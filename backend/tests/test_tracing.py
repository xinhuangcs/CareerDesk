
import json
import os
import stat

import pytest

from careerdesk.platform.ai import tracing
from careerdesk.platform.storage.private import UnsafeManagedPath


def test_shared_tracers_are_reused_and_closed(monkeypatch, tmp_path):
    tracing.close_shared_tracers()
    created = []

    class FakeTracer:
        def __init__(self, *, exporters):
            self.exporters = exporters
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(tracing, "Tracer", FakeTracer)
    monkeypatch.setattr(tracing, "MetadataJsonlExporter", lambda path: path)
    path = tmp_path / "traces.jsonl"

    first = tracing.shared_tracer(str(path))
    assert tracing.shared_tracer(str(path)) is first
    assert len(created) == 1

    tracing.close_shared_tracers()
    assert first.closed
    second = tracing.shared_tracer(str(path))
    assert second is not first
    tracing.close_shared_tracers()


def test_metadata_exporter_excludes_all_user_content_and_is_private(tmp_path):
    path = tmp_path / "data" / "traces.jsonl"
    exporter = tracing.MetadataJsonlExporter(path)
    exporter.export({
        "type": "tool_call",
        "tool": "record_review",
        "status": "ok",
        "params": {"text": "PRIVATE-INTERVIEW"},
        "result": "PRIVATE-RESULT",
        "query": "PRIVATE-QUERY",
        "content": "PRIVATE-CONTENT",
        "usage": {"input_tokens": 12, "provider_note": "PRIVATE-USAGE"},
        "latency_ms": 17,
    })
    exporter.close()

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event == {
        "type": "tool_call",
        "tool": "record_review",
        "status": "ok",
        "usage": {"input_tokens": 12},
        "latency_ms": 17,
    }
    serialized = path.read_text(encoding="utf-8")
    assert "PRIVATE" not in serialized
    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_opening_trace_sanitizes_active_file_and_archives(tmp_path):
    path = tmp_path / "traces.jsonl"
    archive = tmp_path / "traces.jsonl.1"
    raw = json.dumps({
        "type": "context_block", "query": "PRIVATE-QUERY",
        "params": {"content": "PRIVATE-PARAM"}, "block_chars": 42,
    }) + "\nnot-json\n"
    path.write_text(raw, encoding="utf-8")
    archive.write_text(raw, encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o644)
        os.chmod(archive, 0o644)

    exporter = tracing.MetadataJsonlExporter(path)
    exporter.close()

    for candidate in (path, archive):
        content = candidate.read_text(encoding="utf-8")
        assert "PRIVATE" not in content and "not-json" not in content
        assert json.loads(content) == {"type": "context_block", "block_chars": 42}
        if os.name == "posix":
            assert stat.S_IMODE(candidate.stat().st_mode) == 0o600


def test_trace_rotation_is_size_bounded_and_keeps_at_most_four_files(tmp_path):
    path = tmp_path / "traces.jsonl"
    exporter = tracing.MetadataJsonlExporter(
        path, max_bytes=220, ttl_seconds=3600, max_files=4,
    )
    for index in range(30):
        exporter.export({
            "type": "tool_call", "tool": "preferences", "status": "ok",
            "run_id": f"run-{index:03d}", "latency_ms": index,
            "params": {"value": f"PRIVATE-{index}"},
        })
    exporter.close()

    family = sorted(tmp_path.glob("traces.jsonl*"))
    assert len(family) == 4
    assert {item.name for item in family} == {
        "traces.jsonl", "traces.jsonl.1", "traces.jsonl.2", "traces.jsonl.3",
    }
    assert all("PRIVATE" not in item.read_text(encoding="utf-8") for item in family)


def test_trace_ttl_removes_expired_history_before_open(tmp_path):
    path = tmp_path / "traces.jsonl"
    archive = tmp_path / "traces.jsonl.1"
    path.write_text('{"type":"old","params":{"text":"PRIVATE"}}\n', encoding="utf-8")
    archive.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(path, (1, 1))
    os.utime(archive, (1, 1))

    exporter = tracing.MetadataJsonlExporter(path, ttl_seconds=1)
    exporter.close()

    assert path.exists() and path.read_text(encoding="utf-8") == ""
    assert not archive.exists()


def test_trace_final_symlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "outside"
    target.write_text("KEEP", encoding="utf-8")
    path = tmp_path / "traces.jsonl"
    try:
        path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(UnsafeManagedPath, match="符号链接"):
        tracing.MetadataJsonlExporter(path)

    assert path.is_symlink() and target.read_text(encoding="utf-8") == "KEEP"
