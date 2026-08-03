"""Bounded, metadata-only Agent traces with process-level reuse."""

from __future__ import annotations

import atexit
import json
import logging
import os
from pathlib import Path
from threading import Lock
from time import time
from uuid import uuid4

from agentmaker import Tracer

from ..storage.private import (canonical_private_file, open_private_binary_exclusive,
                               open_private_text_append, prepare_private_file)

logger = logging.getLogger(__name__)

TRACE_MAX_BYTES = 5 * 1024 * 1024
TRACE_TTL_SECONDS = 30 * 24 * 60 * 60
# Includes the active file: traces.jsonl + .1 + .2 + .3.
TRACE_MAX_FILES = 4

_TEXT_FIELDS = {
    "type", "model", "finish_reason", "run_id", "tool", "status", "paradigm",
}
_NUMBER_FIELDS = {
    "latency_ms", "step_index", "before", "after", "block_chars", "sources",
}
_BOOLEAN_FIELDS = {"has_tool_calls", "streamed"}
_USAGE_FIELDS = {
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "prompt_token_count",
    "candidates_token_count", "total_token_count", "cached_content_token_count",
    "thoughts_token_count", "tool_use_prompt_token_count", "prompt_tokens_details",
    "completion_tokens_details", "audio_tokens", "cached_tokens", "reasoning_tokens",
    "accepted_prediction_tokens", "rejected_prediction_tokens",
}
_MAX_METADATA_TEXT = 160

_TRACERS: dict[str, Tracer] = {}
_TRACERS_LOCK = Lock()


def _bounded_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:_MAX_METADATA_TEXT]


def _numeric_metadata(value):
    """Keep only numeric usage trees; arbitrary strings can contain user data."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        projected = {}
        for key, item in value.items():
            if key not in _USAGE_FIELDS:
                continue
            safe = _numeric_metadata(item)
            if safe is not None:
                projected[key] = safe
        return projected
    return None


def project_trace_metadata(event: dict) -> dict:
    """Project one trace event onto the strict metadata allowlist."""
    if not isinstance(event, dict):
        return {"type": "unknown"}
    projected: dict = {}
    for key in _TEXT_FIELDS:
        value = _bounded_text(event.get(key))
        if value is not None:
            projected[key] = value
    for key in _NUMBER_FIELDS:
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            projected[key] = value
    for key in _BOOLEAN_FIELDS:
        value = event.get(key)
        if isinstance(value, bool):
            projected[key] = value
    usage = _numeric_metadata(event.get("usage"))
    if isinstance(usage, dict) and usage:
        projected["usage"] = usage
    projected.setdefault("type", "unknown")
    return projected


def _json_line(event: dict) -> bytes:
    return (json.dumps(project_trace_metadata(event), ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


class MetadataJsonlExporter:
    """Append bounded metadata JSONL and sanitize existing files before reuse."""

    def __init__(self, path: str | Path, *, max_bytes: int = TRACE_MAX_BYTES,
                 ttl_seconds: int = TRACE_TTL_SECONDS,
                 max_files: int = TRACE_MAX_FILES):
        if max_bytes <= 0 or ttl_seconds < 0 or max_files <= 0:
            raise ValueError("trace retention 参数必须为正数")
        self.path = canonical_private_file(path)
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.max_files = max_files
        self._lock = Lock()
        self._file = None
        self._maintain_existing_files()
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            self._rotate()
        self._file = open_private_text_append(self.path)

    def _archive(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _numeric_archives(self) -> list[tuple[int, Path]]:
        prefix = f"{self.path.name}."
        archives = []
        for candidate in self.path.parent.iterdir():
            if not candidate.name.startswith(prefix):
                continue
            suffix = candidate.name[len(prefix):]
            if suffix.isdecimal():
                archives.append((int(suffix), candidate))
        return sorted(archives)

    def _sanitize_existing(self, path: Path) -> None:
        prepare_private_file(path, create=False)
        info = path.stat()
        temporary = path.parent / f".{path.name}.sanitize-{uuid4().hex}"
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                with open_private_binary_exclusive(temporary) as output:
                    for line in source:
                        try:
                            event = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        output.write(_json_line(event))
                    output.flush()
                    os.fsync(output.fileno())
            os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _maintain_existing_files(self) -> None:
        existing: list[tuple[int, Path]] = []
        if os.path.lexists(self.path):
            existing.append((0, self.path))
        existing.extend(self._numeric_archives())
        cutoff = time() - self.ttl_seconds
        archive_slots = self.max_files - 1
        kept_archives = 0
        for index, path in existing:
            # Validate before reading timestamps or deleting: a trace-family
            # symlink/hardlink is an unsafe final sensitive file, not cleanup input.
            prepare_private_file(path, create=False)
            is_archive = path != self.path
            canonical_archive = is_archive and index >= 1 and path == self._archive(index)
            over_file_limit = is_archive and (
                not canonical_archive or index > archive_slots
                or kept_archives >= archive_slots
            )
            if path.lstat().st_mtime < cutoff or over_file_limit:
                path.unlink()
                continue
            if is_archive:
                kept_archives += 1
            self._sanitize_existing(path)

    def _rotate(self) -> None:
        archive_slots = self.max_files - 1
        if archive_slots == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self._archive(archive_slots)
        if os.path.lexists(oldest):
            prepare_private_file(oldest, create=False)
            oldest.unlink()
        for index in range(archive_slots - 1, 0, -1):
            source = self._archive(index)
            if os.path.lexists(source):
                prepare_private_file(source, create=False)
                os.replace(source, self._archive(index + 1))
        if os.path.lexists(self.path):
            prepare_private_file(self.path, create=False)
            os.replace(self.path, self._archive(1))

    def export(self, event: dict) -> None:
        encoded = _json_line(event)
        with self._lock:
            if self._file is None:
                return
            current_size = os.fstat(self._file.fileno()).st_size
            if current_size and current_size + len(encoded) > self.max_bytes:
                self._file.close()
                self._file = None
                self._rotate()
                self._file = open_private_text_append(self.path)
            self._file.buffer.write(encoded)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def shared_tracer(trace_path: str) -> Tracer:
    """Reuse one bounded metadata tracer per canonical parent + final filename."""
    normalized = str(canonical_private_file(trace_path))
    with _TRACERS_LOCK:
        tracer = _TRACERS.get(normalized)
        if tracer is None:
            tracer = Tracer(exporters=[MetadataJsonlExporter(normalized)])
            _TRACERS[normalized] = tracer
        return tracer


def maintain_trace_files(trace_path: str | Path) -> None:
    """Sanitize and enforce retention at startup, even before any Agent exists."""
    exporter = MetadataJsonlExporter(trace_path)
    exporter.close()


def close_shared_tracers() -> None:
    """Idempotently close all shared exporters."""
    with _TRACERS_LOCK:
        tracers = tuple(_TRACERS.values())
        _TRACERS.clear()
    for tracer in tracers:
        try:
            tracer.close()
        except Exception:  # noqa: BLE001 -- shutdown cleanup must continue for remaining handles
            logger.exception("failed to close shared agent tracer")


atexit.register(close_shared_tracers)
