"""Deterministic resume-adaptation workflow.

GET paths only freeze local state and run deterministic validation/capacity
accounting.  Provider clients are constructed and owned only by the shared
POST task after every prerequisite and explicit downgrade confirmation passes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Literal

from ...features.applications import public as applications
from ...features.research.public import (
    ResearchAttempt,
    derive_search_profile,
    derive_research_artifact_state,
    research_semantic_claim,
)
from ...platform.locale import OutputLocale
from ...platform.ai.client import close_llm_client, model_uses_local_provider
from ...platform.ai.providers import provider_spec, resolve_model_capabilities
from ...platform.ai.structured_tasks import (
    STRUCTURED_CONTEXT_GUARD_TOKENS,
    desired_output_tokens,
    effective_context_window,
    structured_input_tokens,
)
from .adaptation import (
    AdaptationCapacityReceipt,
    AdaptationHostValidationError,
    TextSegment,
    adaptation_input_hash,
    assess_resume_extraction,
    build_resume_adaptation_payload,
    exact_text_segments,
    jd_has_meaningful_content,
    preflight_adaptation_capacity,
    render_untrusted_json,
    validate_cached_materialized_report,
)
from .adaptation_contracts import (
    ADAPTATION_PROMPT_VERSION,
    ADAPTATION_SCHEMA_VERSION,
    ADAPTATION_TASK_OUTPUT_TOKENS,
    MAX_RESUME_SUMMARY_CHARS,
    ResumeSummaryResult,
)
from .ai_tasks import (
    RESUME_SUMMARY_PROMPT,
    PrepAITaskError,
    compose_resume_summary,
    compose_validated_resume_adaptation,
    adaptation_prompt,
)
ADAPTATION_ARTIFACT_VERSION = 1
logger = logging.getLogger(__name__)
ADAPTATION_JOB_TIMEOUT_SECONDS = 180
RESUME_SUMMARY_POLICY_VERSION = 1
_VISUAL_LIMITATION = (
    "仅基于文件抽取文本分析内容、事实、叙事、顺序和文本结构；"
    "不评价字体、颜色、留白、图标或双栏视觉顺序。"
)
_VISUAL_LIMITATION_EN = (
    "The analysis uses extracted text only for content, facts, narrative, order, and "
    "textual structure; it does not assess typography, colour, spacing, icons, or "
    "multi-column visual order."
)
_SUMMARY_CACHE_ENTRY_KEYS = frozenset({
    "resume_content_hash",
    "summary_policy_version",
    "target_chars",
    "summary_text",
    "summary_hash",
    "chunk_count",
    "generated_time",
})
_SUMMARY_CACHE_CONTAINER_KEYS = frozenset({
    "cache_version",
    "resume_content_hash",
    "entries",
})
_SUMMARY_CACHE_VERSION = 1
_SUMMARY_CACHE_MAX_ENTRIES = 4
_SUMMARY_CACHE_MERGE_ATTEMPTS = 4
_SUMMARY_RECEIPT_KEYS = (
    "resume_content_hash",
    "summary_policy_version",
    "target_chars",
    "summary_hash",
    "chunk_count",
)
_ADAPTATION_ARTIFACT_KEYS = frozenset({
    "artifact_version",
    "input_hash",
    "resume_id",
    "resume_name",
    "resume_selection",
    "research_mode",
    "research_snapshot_id",
    "resume_input_form",
    "summary_receipt",
    "jd_parsed_included",
    "generated_time",
    "model_metadata",
    "extraction_receipt",
    "analysis_flags",
    "report",
    "content_locale",
})

_ADAPTATION_TASKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tasks_for_current_loop() -> dict:
    loop = asyncio.get_running_loop()
    return _ADAPTATION_TASKS.setdefault(loop, {})


def _application_has_running_task(
    db_path: str,
    user_id: str,
    application_id: int,
) -> bool:
    """Expose the loop-local single-flight state to async read routes."""
    try:
        tasks = _tasks_for_current_loop()
    except RuntimeError:
        # Pure synchronous callers have no owning event loop and therefore
        # cannot observe a live shared task.
        return False
    prefix = (str(Path(db_path).resolve()), user_id, application_id)
    return any(
        key[:3] == prefix and not task.done()
        for key, task in tasks.items()
    )


def _canonical_hash(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_aware_iso_timestamp(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _attempt(prep: dict) -> dict:
    value = prep.get("research_attempt")
    if isinstance(value, dict):
        try:
            return ResearchAttempt.model_validate(value).model_dump(mode="json")
        except (TypeError, ValueError):
            pass
    # Legacy/malformed attempt metadata must not reflect arbitrary persisted
    # error text through the adaptation response.
    return {
        "attempt_state": "idle",
        "generation": None,
        "updated_time": None,
        "error_code": None,
    }


def _research_action(artifact_state: str, attempt_state: str) -> str | None:
    if attempt_state in {"pending", "running", "disabled", "unavailable"}:
        return None
    if attempt_state == "failed":
        return "retry"
    if artifact_state == "stale":
        return "refresh"
    if artifact_state == "legacy":
        return "restart"
    if artifact_state == "missing":
        # A succeeded attempt without a usable snapshot is a recovery case, not
        # a first run.  Asking for a restart makes the Prep API reopen a broad
        # ``prep_status=ready`` claim instead of returning the old ready result.
        if attempt_state == "succeeded":
            return "restart"
        return "start"
    return None


def _model_disclosure(model_string: str | None) -> dict | None:
    if not model_string:
        return None
    provider, separator, explicit = model_string.partition(":")
    provider = provider.strip()
    spec = provider_spec(provider)
    model = explicit.strip() if separator and explicit.strip() else (
        spec.default_model if spec is not None else ""
    )
    if not provider or not model:
        return None
    return {
        "provider": provider,
        "model": model,
        # Deliberately exclude endpoint, credentials and environment metadata.
        "label": f"{provider} · {model}",
    }


@dataclass(frozen=True, slots=True)
class _PreparedInput:
    snapshot: dict
    research_mode: Literal["snapshot", "no_research"]
    resume_input_form: Literal["full_text", "summarized"]
    resume_segments: list[TextSegment]
    jd_segments: list[TextSegment]
    model_payload: dict
    input_hash: str
    capacity: AdaptationCapacityReceipt
    summary_receipt: dict | None


class ResumeAdaptationWorkflow:
    """Freeze, validate, generate, host-validate and CAS-publish one report."""

    def __init__(
        self,
        db_path: str,
        *,
        model_string: str | None,
        strict_offline: bool,
        context_window: int | None,
        max_output_tokens: int | None,
        llm_factory: Callable[[], object] | None = None,
        output_locale: OutputLocale = "zh-CN",
    ) -> None:
        self._db_path = db_path
        self._model_string = model_string
        self._strict_offline = strict_offline
        self._explicit_context_window = context_window
        self._explicit_max_output_tokens = max_output_tokens
        self._llm_factory = llm_factory
        self._output_locale = output_locale

    def _capability_probe(self):
        context, output = resolve_model_capabilities(
            self._model_string,
            context_window=self._explicit_context_window,
            max_output_tokens=self._explicit_max_output_tokens,
        )
        if context is None or output is None:
            return SimpleNamespace(context_window=None, max_output_tokens=None)
        return SimpleNamespace(context_window=context, max_output_tokens=output)

    def _model_available(self) -> bool:
        if not self._model_string or self._llm_factory is None:
            return False
        if self._strict_offline and not model_uses_local_provider(self._model_string):
            return False
        return True

    def _l(self, zh: str, en: str) -> str:
        return en if self._output_locale == "en" else zh

    def _derive_snapshot(self, frozen: dict) -> dict:
        """Attach Research policy state to one feature-owned aggregate snapshot."""
        if frozen.get("status") != "ok":
            return {"status": "missing"}
        root_prep = frozen["prep"]
        localized = root_prep.get("localized") if isinstance(root_prep, dict) else None
        localized_entry = (
            localized.get(self._output_locale)
            if isinstance(localized, dict)
            and isinstance(localized.get(self._output_locale), dict)
            else {}
        )
        legacy = root_prep if self._output_locale == "zh-CN" else {}
        prep = {**root_prep, **legacy, **localized_entry}
        semantic_claim = research_semantic_claim(
            company=frozen["company"],
            aliases=frozen["company_aliases"],
            notes=frozen["company_notes"],
            department=frozen["department"],
            position=frozen["position"],
            jd_text=frozen["jd_text"],
            output_locale=self._output_locale,
            search_profile=derive_search_profile(
                frozen["company"], frozen["position"], frozen["jd_text"]
            ),
        )
        semantic_claim_hash = _canonical_hash(semantic_claim)
        return {
            **frozen,
            "prep": prep,
            "semantic_claim": semantic_claim,
            "semantic_claim_hash": semantic_claim_hash,
            "research_artifact": derive_research_artifact_state(
                root_prep,
                current_semantic_claim_hash=semantic_claim_hash,
                content_locale=self._output_locale,
            ),
            "research_attempt": _attempt(prep),
        }

    def _read_snapshot(self, user_id: str, application_id: int) -> dict:
        return self._derive_snapshot(
            applications.freeze_resume_adaptation_input(
                self._db_path,
                user_id,
                application_id,
            )
        )

    @staticmethod
    def _resume_dto(resume: dict, *, include_receipt: bool = False) -> dict:
        receipt = None
        if include_receipt:
            receipt = assess_resume_extraction(
                resume.get("content_text"),
                source_suffix=Path(resume.get("file_path") or "").suffix,
            ).model_dump()
        return {
            "id": resume["id"],
            "name": resume["name"],
            "updated_time": resume.get("updated_time"),
            "extraction_receipt": receipt,
        }

    def _research_dto(self, snapshot: dict) -> dict:
        artifact = snapshot["research_artifact"]
        attempt = snapshot["research_attempt"]
        published = artifact.get("snapshot") if isinstance(artifact, dict) else None
        return {
            "artifact_state": artifact.get("artifact_state", "missing"),
            "attempt_state": attempt["attempt_state"],
            "coverage_quality": (
                artifact.get("coverage_quality")
                if artifact.get("artifact_state") == "ready"
                else None
            ),
            "fresh_until": (
                published.get("fresh_until") if isinstance(published, dict) else None
            ),
            "error_code": attempt.get("error_code"),
            "action": _research_action(
                artifact.get("artifact_state", "missing"),
                attempt["attempt_state"],
            ),
        }

    def _base_response(self, snapshot: dict, state: str, *, message: str | None = None) -> dict:
        bound = snapshot.get("bound_resume")
        return {
            "state": state,
            "message": message,
            "cached": False,
            "bound_resume": self._resume_dto(bound, include_receipt=True) if bound else None,
            "resume_options": [self._resume_dto(item) for item in snapshot.get("resumes", [])],
            "recommended_resume_id": (
                snapshot["resumes"][0]["id"]
                if snapshot.get("bound_resume") is None and snapshot.get("resumes")
                else None
            ),
            "research": self._research_dto(snapshot),
            "report": None,
            "envelope": None,
            "host_limitations": [self._l(
                _VISUAL_LIMITATION,
                _VISUAL_LIMITATION_EN,
            )] if bound else [],
            "analysis_flags": [],
            "estimated_input_tokens": None,
            "model_disclosure": _model_disclosure(self._model_string),
            "summarization_available": False,
            "no_research_fallback_available": False,
            "model_input_preview_available": bool(bound),
        }

    @staticmethod
    def _research_payload(snapshot_value: dict) -> dict:
        def safe_sources(items) -> list[dict]:
            if not isinstance(items, list):
                return []
            result = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("source_id"), str):
                    continue
                result.append({
                    "source_id": item["source_id"],
                    "title": item.get("title") if isinstance(item.get("title"), str) else "",
                    "date": item.get("date") if isinstance(item.get("date"), str) else None,
                })
            return result

        return {
            "company_report": snapshot_value.get("company_report") or {},
            "position_report": snapshot_value.get("position_report") or {},
            "company_source_refs": safe_sources(snapshot_value.get("company_sources")),
            "position_source_refs": safe_sources(snapshot_value.get("position_sources")),
            "coverage_quality": snapshot_value.get("coverage_quality"),
            "coverage_limitations": snapshot_value.get("coverage_limitations") or [],
            "source_conflicts": snapshot_value.get("source_conflicts") or [],
        }

    @staticmethod
    def _research_fingerprint(
        research_mode: Literal["snapshot", "no_research"],
        snapshot: dict,
    ) -> dict:
        if research_mode == "no_research":
            return {
                "mode": "no_research",
                "attempt_state": snapshot["research_attempt"]["attempt_state"],
            }
        value = snapshot["research_artifact"]["snapshot"]
        return {
            "mode": "snapshot",
            "snapshot_id": value["snapshot_id"],
            "semantic_claim_hash": value["semantic_claim_hash"],
            "company_report_hash": value["company_report_hash"],
            "position_report_hash": value["position_report_hash"],
            "fresh_until": value["fresh_until"],
        }

    def _payloads(
        self,
        snapshot: dict,
        *,
        research_mode: Literal["snapshot", "no_research"],
        resume_input_form: Literal["full_text", "summarized"],
        summary_text: str | None = None,
    ) -> tuple[list[TextSegment], list[TextSegment], dict, dict, str]:
        resume = snapshot["bound_resume"]
        resume_segments = exact_text_segments(resume["content_text"], namespace="R")
        jd_segments = exact_text_segments(snapshot["jd_text"], namespace="J")
        research = (
            self._research_payload(snapshot["research_artifact"]["snapshot"])
            if research_mode == "snapshot"
            else None
        )
        common = {
            "company": snapshot["company"],
            "position": snapshot["position"],
            "department": snapshot["department"],
            "jd_segments": jd_segments,
            "resume_input_form": resume_input_form,
            "resume_segments": resume_segments if resume_input_form == "full_text" else None,
            "resume_summary_text": summary_text,
            "research": research,
        }
        with_parsed = build_resume_adaptation_payload(
            **common,
            jd_parsed=snapshot["jd_parsed"],
        )
        without_parsed = build_resume_adaptation_payload(**common, jd_parsed=None)
        without_resume = json.loads(json.dumps(without_parsed, ensure_ascii=False))
        without_resume["resume"] = {
            "resume_input_form": "full_text",
            "segments": [],
        }
        return resume_segments, jd_segments, with_parsed, without_parsed, render_untrusted_json(
            "resume_adaptation_input", without_resume,
        )

    def _capacity(
        self,
        with_parsed: dict,
        without_parsed: dict,
        without_resume_payload: str,
    ) -> AdaptationCapacityReceipt:
        return preflight_adaptation_capacity(
            self._capability_probe(),
            system_prompt=adaptation_prompt(self._output_locale),
            payload_with_jd_parsed=render_untrusted_json(
                "resume_adaptation_input", with_parsed,
            ),
            payload_without_jd_parsed=render_untrusted_json(
                "resume_adaptation_input", without_parsed,
            ),
            payload_without_resume=without_resume_payload,
        )

    def _input_for_form(
        self,
        snapshot: dict,
        *,
        research_mode: Literal["snapshot", "no_research"],
        resume_input_form: Literal["full_text", "summarized"],
        summary_cache: dict | None = None,
        include_jd_parsed: bool | None = None,
    ) -> _PreparedInput:
        summary_text = (
            summary_cache.get("summary_text")
            if resume_input_form == "summarized" and isinstance(summary_cache, dict)
            else None
        )
        resume_segments, jd_segments, with_parsed, without_parsed, no_resume = self._payloads(
            snapshot,
            research_mode=research_mode,
            resume_input_form=resume_input_form,
            summary_text=summary_text,
        )
        capacity = self._capacity(with_parsed, without_parsed, no_resume)
        if include_jd_parsed is None:
            include_jd_parsed = capacity.include_jd_parsed
        payload = with_parsed if include_jd_parsed else without_parsed
        summary_receipt = (
            {
                key: summary_cache.get(key)
                for key in (
                    "resume_content_hash",
                    "summary_policy_version",
                    "target_chars",
                    "summary_hash",
                    "chunk_count",
                )
            }
            if resume_input_form == "summarized" and isinstance(summary_cache, dict)
            else None
        )
        input_hash = adaptation_input_hash(
            payload,
            resume_id=snapshot["bound_resume"]["id"],
            resume_content_text=snapshot["bound_resume"]["content_text"],
            research_fingerprint=self._research_fingerprint(research_mode, snapshot),
            summary_receipt=summary_receipt,
            output_locale=self._output_locale,
        )
        return _PreparedInput(
            snapshot=snapshot,
            research_mode=research_mode,
            resume_input_form=resume_input_form,
            resume_segments=resume_segments,
            jd_segments=jd_segments,
            model_payload=payload,
            input_hash=input_hash,
            capacity=capacity,
            summary_receipt=summary_receipt,
        )

    @staticmethod
    def _summary_receipt(cached: dict) -> dict:
        return {key: cached[key] for key in _SUMMARY_RECEIPT_KEYS}

    @staticmethod
    def _summary_cache_key(cached: dict) -> tuple[str, int, int]:
        return (
            cached["resume_content_hash"],
            cached["summary_policy_version"],
            cached["target_chars"],
        )

    def _valid_summary_cache_entries(self, snapshot: dict) -> list[dict]:
        """Read the current bounded cache, accepting the legacy single-entry shape."""

        cached = snapshot["prep"].get("resume_adaptation_summary")
        if not isinstance(cached, dict):
            return []
        if set(cached) == _SUMMARY_CACHE_ENTRY_KEYS:
            candidates = [cached]
            container_resume_hash = None
        elif (
            set(cached) == _SUMMARY_CACHE_CONTAINER_KEYS
            and type(cached.get("cache_version")) is int
            and cached.get("cache_version") == _SUMMARY_CACHE_VERSION
            and isinstance(cached.get("entries"), list)
            and 1 <= len(cached["entries"]) <= _SUMMARY_CACHE_MAX_ENTRIES
        ):
            candidates = cached["entries"]
            container_resume_hash = cached.get("resume_content_hash")
        else:
            return []

        resume_hash = hashlib.sha256(
            snapshot["bound_resume"]["content_text"].encode("utf-8")
        ).hexdigest()
        if container_resume_hash is not None and container_resume_hash != resume_hash:
            return []
        valid: list[dict] = []
        cache_keys: set[tuple[str, int, int]] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != _SUMMARY_CACHE_ENTRY_KEYS:
                return []
            cached_target = candidate.get("target_chars")
            chunk_count = candidate.get("chunk_count")
            if (
                candidate.get("resume_content_hash") != resume_hash
                or type(candidate.get("summary_policy_version")) is not int
                or candidate.get("summary_policy_version") != RESUME_SUMMARY_POLICY_VERSION
                or type(cached_target) is not int
                or not 1 <= cached_target <= MAX_RESUME_SUMMARY_CHARS
                or not isinstance(candidate.get("summary_text"), str)
                or not candidate["summary_text"].strip()
                or len(candidate["summary_text"]) > cached_target
                or candidate.get("summary_hash") != hashlib.sha256(
                    candidate["summary_text"].encode("utf-8")
                ).hexdigest()
                or type(chunk_count) is not int
                or not 1 <= chunk_count <= max(
                    1,
                    len(snapshot["bound_resume"]["content_text"]),
                )
                or 2 * chunk_count - 1 > cached_target
                or not _is_aware_iso_timestamp(candidate.get("generated_time"))
            ):
                return []
            cache_key = self._summary_cache_key(candidate)
            if cache_key in cache_keys:
                return []
            cache_keys.add(cache_key)
            valid.append(candidate)
        return valid

    def _valid_summary_cache(
        self,
        snapshot: dict,
        *,
        target_chars: int | None = None,
        expected_receipt: dict | None = None,
    ) -> dict | None:
        for cached in self._valid_summary_cache_entries(snapshot):
            if target_chars is not None and cached["target_chars"] != target_chars:
                continue
            if (
                expected_receipt is not None
                and self._summary_receipt(cached) != expected_receipt
            ):
                continue
            return cached
        return None

    def _cached_prepared(self, snapshot: dict) -> tuple[dict, _PreparedInput] | None:
        artifact = snapshot["prep"].get("resume_adaptation")
        if (
            not isinstance(artifact, dict)
            or set(artifact) != _ADAPTATION_ARTIFACT_KEYS
            or artifact.get("artifact_version") != ADAPTATION_ARTIFACT_VERSION
            or artifact.get("content_locale") != self._output_locale
            or artifact.get("resume_id") != snapshot["bound_resume"]["id"]
            or artifact.get("resume_name") != snapshot["bound_resume"]["name"]
            or artifact.get("resume_selection") != "bound"
            or not _is_aware_iso_timestamp(artifact.get("generated_time"))
            or type(artifact.get("jd_parsed_included")) is not bool
            or artifact.get("analysis_flags") != []
            or not isinstance(artifact.get("report"), dict)
        ):
            return None
        model_metadata = artifact.get("model_metadata")
        if not isinstance(model_metadata, dict):
            return None
        if model_metadata:
            if set(model_metadata) != {"provider", "model", "label"} or any(
                not isinstance(model_metadata.get(key), str)
                or not model_metadata[key]
                or len(model_metadata[key]) > 500
                for key in ("provider", "model", "label")
            ):
                return None
        current_extraction = json.loads(json.dumps(assess_resume_extraction(
            snapshot["bound_resume"]["content_text"],
            source_suffix=Path(snapshot["bound_resume"].get("file_path") or "").suffix,
        ).model_dump()))
        if artifact.get("extraction_receipt") != current_extraction:
            return None
        research_mode = artifact.get("research_mode")
        if research_mode == "snapshot":
            research_value = snapshot["research_artifact"]
            if research_value.get("artifact_state") != "ready":
                return None
            published = research_value.get("snapshot")
            if not isinstance(published, dict) or artifact.get("research_snapshot_id") != published.get("snapshot_id"):
                return None
        elif research_mode == "no_research":
            if snapshot["research_artifact"].get("artifact_state") == "ready":
                return None
            if snapshot["research_attempt"]["attempt_state"] not in {"disabled", "unavailable"}:
                return None
        else:
            return None
        form = artifact.get("resume_input_form")
        if form == "summarized":
            summary_cache = self._valid_summary_cache(
                snapshot,
                expected_receipt=artifact.get("summary_receipt"),
            )
            if summary_cache is None:
                return None
        elif form == "full_text":
            summary_cache = None
        else:
            return None
        try:
            prepared = self._input_for_form(
                snapshot,
                research_mode=research_mode,
                resume_input_form=form,
                summary_cache=summary_cache,
                include_jd_parsed=artifact["jd_parsed_included"],
            )
            materialized = validate_cached_materialized_report(
                artifact["report"],
                jd_segments=prepared.jd_segments,
                resume_segments=prepared.resume_segments,
                resume_input_form=prepared.resume_input_form,
                output_locale=self._output_locale,
            )
        except (TypeError, ValueError):
            return None
        if (
            artifact.get("input_hash") != prepared.input_hash
            or artifact.get("summary_receipt") != prepared.summary_receipt
        ):
            return None
        return {**artifact, "report": materialized}, prepared

    def _host_limitations(self, prepared: _PreparedInput) -> list[str]:
        limitations = [
            self._l(
                _VISUAL_LIMITATION,
                _VISUAL_LIMITATION_EN,
            )
        ]
        receipt = assess_resume_extraction(
            prepared.snapshot["bound_resume"]["content_text"],
            source_suffix=Path(prepared.snapshot["bound_resume"].get("file_path") or "").suffix,
        )
        if receipt.warning_codes:
            limitations.append(self._l("抽取文本含少量异常字符；请通过“查看模型读取文本”核对原文。", "The extracted text contains a few unusual characters. Review the source under “View model input”."))
        if prepared.research_mode == "no_research":
            limitations.append(self._l("未结合公司调研；本次仅依据 JD 与简历生成。", "Company research was not included; this report uses only the job description and résumé."))
        else:
            research = prepared.snapshot["research_artifact"]["snapshot"]
            if research.get("coverage_quality") != "complete":
                missing = research.get("missing_sections") or []
                suffix = ((f" (missing: {', '.join(missing)})" if self._output_locale == "en" else f"（缺失：{'、'.join(missing)}）") if missing else "")
                limitations.append(self._l(f"公司调研公开信息覆盖有限{suffix}，未知信息未由模型补全。", f"Public research coverage is limited{suffix}; the model did not invent missing information."))
            for conflict in research.get("source_conflicts") or []:
                if not isinstance(conflict, dict):
                    continue
                summary = conflict.get("summary")
                sources = conflict.get("sources")
                if not isinstance(summary, str) or not isinstance(sources, list):
                    continue
                source_label = (", " if self._output_locale == "en" else "、").join(
                    source for source in sources if isinstance(source, str)
                )
                limitations.append(self._l(f"公司调研来源存在冲突（{source_label}）：{summary}", f"Company research sources conflict ({source_label}): {summary}"))
            attempt_state = prepared.snapshot["research_attempt"]["attempt_state"]
            if attempt_state in {"failed", "disabled", "unavailable"}:
                limitations.append(self._l("最近一次调研刷新未成功；本报告使用仍在有效期内的上次成功快照。", "The latest research refresh failed; this report uses the previous snapshot while it remains valid."))
        if prepared.resume_input_form == "summarized":
            limitations.append(self._l("基于压缩摘要、非全文分析；不包含逐段点评或原文对照改写。", "This is based on a compressed summary rather than the full text, so it excludes section-by-section review and source-aligned rewrites."))
        return limitations

    def _ok_response(self, artifact: dict, prepared: _PreparedInput, *, cached: bool) -> dict:
        response = self._base_response(prepared.snapshot, "ok")
        response.update({
            "cached": cached,
            "report": artifact["report"],
            "envelope": {
                "artifact_version": artifact["artifact_version"],
                "resume_id": artifact["resume_id"],
                "resume_name": artifact["resume_name"],
                "resume_selection": artifact.get("resume_selection", "bound"),
                "research_mode": artifact["research_mode"],
                "research_snapshot_id": artifact.get("research_snapshot_id"),
                "resume_input_form": artifact["resume_input_form"],
                "generated_time": artifact["generated_time"],
                "content_locale": artifact["content_locale"],
            },
            "host_limitations": self._host_limitations(prepared),
            "analysis_flags": list(artifact.get("analysis_flags") or []),
            "estimated_input_tokens": prepared.capacity.estimated_input_tokens,
            "model_input_preview_available": True,
        })
        return response

    def _inspect_snapshot(
        self,
        snapshot: dict,
    ) -> dict | None:
        """Evaluate one already-frozen local snapshot without provider work."""
        if snapshot["status"] == "missing":
            return None
        if not snapshot["resumes"]:
            return self._base_response(snapshot, "no_resume", message=self._l("尚未上传可用简历", "No usable résumé has been uploaded yet."))
        if snapshot["bound_resume"] is None:
            return self._base_response(
                snapshot,
                "resume_selection_required",
                message=self._l("请确认并绑定本次投递使用的简历版本", "Confirm which résumé version is used for this application."),
            )
        receipt = assess_resume_extraction(
            snapshot["bound_resume"]["content_text"],
            source_suffix=Path(snapshot["bound_resume"].get("file_path") or "").suffix,
        )
        if not receipt.usable:
            return self._base_response(
                snapshot,
                "resume_reupload_required",
                message=self._l("当前简历无法提取出可靠文本，请上传可复制文本的 PDF 或 DOCX", "Reliable text could not be extracted from this résumé. Upload a text-based PDF or DOCX."),
            )
        if not jd_has_meaningful_content(
            snapshot["jd_text"],
            company=snapshot["company"],
            position=snapshot["position"],
        ):
            return self._base_response(snapshot, "missing_jd", message=self._l("请先补充完整岗位 JD", "Add the complete job description first."))
        attempt_state = snapshot["research_attempt"]["attempt_state"]
        if attempt_state in {"pending", "running"}:
            return self._base_response(snapshot, "research_running", message=self._l("公司调研正在生成", "Company research is being generated."))

        cached = self._cached_prepared(snapshot)
        if cached is not None:
            artifact, prepared = cached
            return self._ok_response(artifact, prepared, cached=True)

        artifact_state = snapshot["research_artifact"].get("artifact_state")
        if artifact_state != "ready":
            state = {
                "failed": "research_failed",
                "disabled": "research_disabled",
                "unavailable": "research_unavailable",
            }.get(attempt_state, "research_required")
            response = self._base_response(snapshot, state)
            response["no_research_fallback_available"] = attempt_state in {
                "disabled", "unavailable",
            }
            if response["no_research_fallback_available"] and self._model_available():
                try:
                    prepared = self._input_for_form(
                        snapshot,
                        research_mode="no_research",
                        resume_input_form="full_text",
                    )
                    response["estimated_input_tokens"] = prepared.capacity.estimated_input_tokens
                    response["summarization_available"] = prepared.capacity.summarization_available
                except (TypeError, ValueError):
                    pass
            return response

        if not self._model_available():
            return self._base_response(
                snapshot,
                "model_required",
                message=self._l("请先在“模型与隐私”配置当前可用的模型", "Configure an available model under Model & Privacy first."),
            )
        prepared = self._input_for_form(
            snapshot,
            research_mode="snapshot",
            resume_input_form="full_text",
        )
        if not prepared.capacity.fits:
            response = self._base_response(
                snapshot,
                "insufficient_model_capacity",
                message=self._l("当前模型无法同时容纳完整材料和安全输出预算", "The current model cannot fit the complete materials and a safe output budget."),
            )
            response["estimated_input_tokens"] = prepared.capacity.estimated_input_tokens
            response["summarization_available"] = prepared.capacity.summarization_available
            return response
        response = self._base_response(snapshot, "ready")
        response["estimated_input_tokens"] = prepared.capacity.estimated_input_tokens
        return response

    def inspect(self, user_id: str, application_id: int) -> dict | None:
        """Return the exact local state; never constructs a provider client."""
        snapshot = self._read_snapshot(user_id, application_id)
        if snapshot["status"] == "missing":
            return None
        if _application_has_running_task(self._db_path, user_id, application_id):
            return self._base_response(
                snapshot,
                "generation_running",
                message=self._l("简历优化建议正在后台生成；离开当前页面不会中断任务", "Résumé recommendations are being generated in the background. Leaving this page will not interrupt the task."),
            )
        return self._inspect_snapshot(snapshot)

    def _choose_summary_target(
        self,
        snapshot: dict,
        *,
        research_mode: Literal["snapshot", "no_research"],
    ) -> int | None:
        low, high = 1, MAX_RESUME_SUMMARY_CHARS
        best = None
        while low <= high:
            target = (low + high) // 2
            placeholder = "简" * target
            try:
                prepared = self._input_for_form(
                    snapshot,
                    research_mode=research_mode,
                    resume_input_form="summarized",
                    summary_cache={
                        "summary_text": placeholder,
                        "resume_content_hash": hashlib.sha256(
                            snapshot["bound_resume"]["content_text"].encode("utf-8")
                        ).hexdigest(),
                        "summary_policy_version": RESUME_SUMMARY_POLICY_VERSION,
                        "target_chars": target,
                        "summary_hash": hashlib.sha256(placeholder.encode("utf-8")).hexdigest(),
                        "chunk_count": 1,
                    },
                )
            except (TypeError, ValueError):
                return None
            if prepared.capacity.fits:
                best = target
                low = target + 1
            else:
                high = target - 1
        return best

    @staticmethod
    def _summary_payload(text: str, *, ordinal: int, count: int) -> dict:
        return {
            "kind": "careerdesk_untrusted_resume_summary_input_v1",
            "chunk_ordinal": ordinal,
            "chunk_count": count,
            "resume_text": text,
        }

    def _summary_call_fits(self, llm, text: str, target_chars: int) -> bool:
        payload = {
            **self._summary_payload(text, ordinal=1, count=1),
            "target_chars": target_chars,
        }
        rendered = render_untrusted_json("resume_summary_input", payload)
        try:
            context = effective_context_window(llm)
            input_tokens = structured_input_tokens(
                RESUME_SUMMARY_PROMPT,
                rendered,
                ResumeSummaryResult,
            )
            provider_output = desired_output_tokens(llm, ADAPTATION_TASK_OUTPUT_TOKENS)
        except Exception:  # model capacity was already checked by adaptation preflight
            return False
        required = min(provider_output, max(512, target_chars * 2 + 1_000))
        return context - input_tokens - STRUCTURED_CONTEXT_GUARD_TOKENS >= required

    def _summary_chunks(self, llm, text: str, target_chars: int) -> list[str]:
        if self._summary_call_fits(llm, text, target_chars):
            return [text]
        chunks: list[str] = []
        cursor = 0
        while cursor < len(text):
            low, high, best = 1, len(text) - cursor, 0
            while low <= high:
                size = (low + high) // 2
                if self._summary_call_fits(llm, text[cursor : cursor + size], max(1, target_chars)):
                    best = size
                    low = size + 1
                else:
                    high = size - 1
            if best <= 0:
                raise PrepAITaskError("当前模型无法安全容纳简历摘要分块，请换用更大上下文模型")
            chunks.append(text[cursor : cursor + best])
            cursor += best
        return chunks

    async def _build_summary(self, llm, snapshot: dict, target_chars: int) -> dict:
        text = snapshot["bound_resume"]["content_text"]
        chunks = self._summary_chunks(llm, text, target_chars)
        # Every non-empty chunk needs at least one summary character and the
        # persisted combined summary also spends one newline between chunks.
        # Reject before any paid summary call when that minimum cannot fit.
        if 2 * len(chunks) - 1 > target_chars:
            raise PrepAITaskError("当前模型需要过多摘要分块，无法形成可靠压缩结果")
        separator_budget = max(0, len(chunks) - 1)
        distributable = target_chars - separator_budget
        lengths = [len(item) for item in chunks]
        total_length = max(1, sum(lengths))
        allocations = [max(1, distributable * length // total_length) for length in lengths]
        while sum(allocations) > distributable:
            index = max(range(len(allocations)), key=allocations.__getitem__)
            if allocations[index] <= 1:
                break
            allocations[index] -= 1
        while sum(allocations) < distributable:
            index = max(
                range(len(allocations)),
                key=lambda item: lengths[item] / allocations[item],
            )
            allocations[index] += 1

        summaries: list[str] = []
        for ordinal, (chunk, allocation) in enumerate(zip(chunks, allocations, strict=True), start=1):
            result = await compose_resume_summary(
                llm,
                self._summary_payload(chunk, ordinal=ordinal, count=len(chunks)),
                target_chars=allocation,
            )
            summaries.append(result.summary_text)
        summary_text = "\n".join(summaries)
        if len(summary_text) > target_chars:  # pragma: no cover - allocation invariant
            raise PrepAITaskError("简历摘要超过宿主目标长度")
        return {
            "resume_content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "summary_policy_version": RESUME_SUMMARY_POLICY_VERSION,
            "target_chars": target_chars,
            "summary_text": summary_text,
            "summary_hash": hashlib.sha256(summary_text.encode("utf-8")).hexdigest(),
            "chunk_count": len(chunks),
            "generated_time": _now_iso(),
        }

    def _persist_summary_if_current(
        self,
        user_id: str,
        application_id: int,
        summary: dict,
    ) -> dict | None:
        """Optimistically merge one keyed summary without evicting the live report.

        The Applications seam owns the write transaction but treats the cache as
        opaque JSON.  Comparing the observed container inside its CAS callback
        lets concurrent target-specific summaries retry and merge instead of
        replacing each other.  If another worker already won the same key, its
        validated entry becomes the canonical summary for this task.
        """

        for _attempt in range(_SUMMARY_CACHE_MERGE_ATTEMPTS):
            latest = self._read_snapshot(user_id, application_id)
            if latest.get("status") != "ok" or latest.get("bound_resume") is None:
                return None
            current_hash = hashlib.sha256(
                latest["bound_resume"]["content_text"].encode("utf-8")
            ).hexdigest()
            if summary.get("resume_content_hash") != current_hash:
                return None

            candidate_snapshot = {
                **latest,
                "prep": {
                    **latest["prep"],
                    "resume_adaptation_summary": summary,
                },
            }
            validated_new = self._valid_summary_cache(
                candidate_snapshot,
                target_chars=summary.get("target_chars"),
            )
            if validated_new is None or validated_new != summary:
                raise PrepAITaskError("简历摘要未通过严格缓存校验")

            existing_entries = self._valid_summary_cache_entries(latest)
            new_key = self._summary_cache_key(summary)
            existing_same_key = next(
                (
                    entry
                    for entry in existing_entries
                    if self._summary_cache_key(entry) == new_key
                ),
                None,
            )
            if existing_same_key is not None:
                return existing_same_key

            published = latest["prep"].get("resume_adaptation")
            published_receipt = (
                published.get("summary_receipt")
                if isinstance(published, dict)
                and published.get("resume_input_form") == "summarized"
                else None
            )
            pinned = next(
                (
                    entry
                    for entry in existing_entries
                    if self._summary_receipt(entry) == published_receipt
                ),
                None,
            )
            ordered = [summary]
            if pinned is not None:
                ordered.append(pinned)
            ordered.extend(existing_entries)
            merged_entries: list[dict] = []
            merged_keys: set[tuple[str, int, int]] = set()
            for entry in ordered:
                cache_key = self._summary_cache_key(entry)
                if cache_key in merged_keys:
                    continue
                merged_entries.append(entry)
                merged_keys.add(cache_key)
                if len(merged_entries) == _SUMMARY_CACHE_MAX_ENTRIES:
                    break
            container = {
                "cache_version": _SUMMARY_CACHE_VERSION,
                "resume_content_hash": summary["resume_content_hash"],
                "entries": merged_entries,
            }
            observed_cache = latest["prep"].get("resume_adaptation_summary")

            def current_validator(
                frozen_input: dict,
                expected_input_hash: str,
                *,
                expected_cache=observed_cache,
            ) -> bool:
                current = self._derive_snapshot(frozen_input)
                if current.get("status") != "ok" or current.get("bound_resume") is None:
                    return False
                frozen_hash = hashlib.sha256(
                    current["bound_resume"]["content_text"].encode("utf-8")
                ).hexdigest()
                return (
                    frozen_hash == expected_input_hash
                    and current["prep"].get("resume_adaptation_summary")
                    == expected_cache
                )

            if applications.merge_resume_adaptation_key_if_current(
                self._db_path,
                user_id,
                application_id,
                key="resume_adaptation_summary",
                value=container,
                expected_input_hash=summary["resume_content_hash"],
                current_validator=current_validator,
            ):
                return summary
        return None

    def _publish_if_current(
        self,
        user_id: str,
        application_id: int,
        prepared: _PreparedInput,
        artifact: dict,
    ) -> bool:
        def current_validator(frozen_input: dict, expected_input_hash: str) -> bool:
            current = self._derive_snapshot(frozen_input)
            if current.get("status") != "ok" or current.get("bound_resume") is None:
                return False
            # A research refresh deliberately blocks adaptation even while the
            # previous success snapshot is still dynamically eligible.  This
            # prevents a report from being published against the old revision
            # after the refresh has already claimed its generation.
            if current["research_attempt"]["attempt_state"] in {"pending", "running"}:
                return False
            if prepared.research_mode == "snapshot":
                if current["research_artifact"].get("artifact_state") != "ready":
                    return False
            else:
                if (
                    current["research_artifact"].get("artifact_state") == "ready"
                    or current["research_attempt"]["attempt_state"] not in {"disabled", "unavailable"}
                ):
                    return False
            summary_cache = (
                self._valid_summary_cache(
                    current,
                    expected_receipt=prepared.summary_receipt,
                )
                if prepared.resume_input_form == "summarized"
                else None
            )
            try:
                current_input = self._input_for_form(
                    current,
                    research_mode=prepared.research_mode,
                    resume_input_form=prepared.resume_input_form,
                    summary_cache=summary_cache,
                    include_jd_parsed="jd_parsed" in prepared.model_payload["target"],
                )
            except (TypeError, ValueError):
                return False
            return current_input.input_hash == expected_input_hash

        return applications.merge_resume_adaptation_key_if_current(
            self._db_path,
            user_id,
            application_id,
            key="resume_adaptation",
            value=artifact,
            expected_input_hash=prepared.input_hash,
            current_validator=current_validator,
            content_locale=self._output_locale,
        )

    async def _run_shared(
        self,
        user_id: str,
        application_id: int,
        snapshot: dict,
        *,
        research_mode: Literal["snapshot", "no_research"],
        use_summary: bool,
        target_chars: int | None,
        expected_full_input_hash: str,
    ) -> dict:
        llm = None
        try:
            if self._llm_factory is None:  # pragma: no cover - guarded before task creation
                raise RuntimeError("model factory unavailable")
            llm = self._llm_factory()
            summary_cache = None
            if use_summary:
                if target_chars is None:
                    raise RuntimeError("summary target missing")
                summary_cache = self._valid_summary_cache(snapshot, target_chars=target_chars)
                if summary_cache is None:
                    summary_cache = await self._build_summary(llm, snapshot, target_chars)
                    persisted_summary = self._persist_summary_if_current(
                        user_id,
                        application_id,
                        summary_cache,
                    )
                    if persisted_summary is None:
                        return self._base_response(
                            snapshot,
                            "stale",
                            message=self._l(
                                "摘要生成期间简历已变化，请重新生成",
                                "The résumé changed while its summary was being generated. Generate it again.",
                            ),
                        )
                    summary_cache = persisted_summary
                    current = self._read_snapshot(user_id, application_id)
                    current_attempt = current.get("research_attempt", {})
                    current_artifact = current.get("research_artifact", {})
                    research_is_current = (
                        current_attempt.get("attempt_state") not in {"pending", "running"}
                        and (
                            (
                                research_mode == "snapshot"
                                and current_artifact.get("artifact_state") == "ready"
                            )
                            or (
                                research_mode == "no_research"
                                and current_artifact.get("artifact_state") != "ready"
                                and current_attempt.get("attempt_state")
                                in {"disabled", "unavailable"}
                            )
                        )
                    )
                    try:
                        current_full = (
                            self._input_for_form(
                                current,
                                research_mode=research_mode,
                                resume_input_form="full_text",
                            )
                            if current.get("status") == "ok"
                            and current.get("bound_resume") is not None
                            and research_is_current
                            else None
                        )
                    except (KeyError, TypeError, ValueError):
                        current_full = None
                    if (
                        current_full is None
                        or current_full.input_hash != expected_full_input_hash
                    ):
                        response_snapshot = (
                            current if current.get("status") == "ok" else snapshot
                        )
                        return self._base_response(
                            response_snapshot,
                            "stale",
                            message=(
                                "摘要生成期间岗位、简历或调研材料已变化；"
                                "摘要已安全保存，请核对当前材料后重新生成"
                            ),
                        )
                    snapshot = current
            prepared = self._input_for_form(
                snapshot,
                research_mode=research_mode,
                resume_input_form="summarized" if use_summary else "full_text",
                summary_cache=summary_cache,
            )
            if not prepared.capacity.fits:
                response = self._base_response(
                    snapshot,
                    "insufficient_model_capacity",
                    message=self._l("当前模型无法同时容纳完整材料和安全输出预算", "The current model cannot fit the complete materials and a safe output budget."),
                )
                response["summarization_available"] = prepared.capacity.summarization_available
                response["estimated_input_tokens"] = prepared.capacity.estimated_input_tokens
                return response
            _report, materialized = await compose_validated_resume_adaptation(
                llm,
                prepared.model_payload,
                jd_segments=prepared.jd_segments,
                resume_segments=prepared.resume_segments,
                resume_input_form=prepared.resume_input_form,
                output_locale=self._output_locale,
            )
            generated_time = _now_iso()
            research_snapshot = snapshot["research_artifact"].get("snapshot")
            artifact = {
                "artifact_version": ADAPTATION_ARTIFACT_VERSION,
                "content_locale": self._output_locale,
                "input_hash": prepared.input_hash,
                "resume_id": snapshot["bound_resume"]["id"],
                "resume_name": snapshot["bound_resume"]["name"],
                "resume_selection": "bound",
                "research_mode": research_mode,
                "research_snapshot_id": (
                    research_snapshot.get("snapshot_id")
                    if research_mode == "snapshot" and isinstance(research_snapshot, dict)
                    else None
                ),
                "resume_input_form": prepared.resume_input_form,
                "summary_receipt": prepared.summary_receipt,
                "jd_parsed_included": "jd_parsed" in prepared.model_payload["target"],
                "generated_time": generated_time,
                "model_metadata": _model_disclosure(self._model_string) or {},
                "extraction_receipt": assess_resume_extraction(
                    snapshot["bound_resume"]["content_text"],
                    source_suffix=Path(snapshot["bound_resume"].get("file_path") or "").suffix,
                ).model_dump(),
                "analysis_flags": [],
                "report": materialized,
            }
            if not self._publish_if_current(
                user_id, application_id, prepared, artifact,
            ):
                return self._base_response(
                    snapshot,
                    "stale",
                    message=self._l("生成期间岗位、简历或调研材料已变化，旧结果未发布", "The role, résumé, or research changed during generation, so the outdated result was not published."),
                )
            return self._ok_response(artifact, prepared, cached=False)
        except AdaptationHostValidationError:
            return self._base_response(
                snapshot,
                "invalid_model_output",
                message=self._l("模型输出未通过引用或覆盖校验，报告未发布", "The model output failed evidence or coverage validation and was not published."),
            )
        except PrepAITaskError as error:
            state = (
                "invalid_model_output"
                if "合规" in str(error) or "valid" in str(error).casefold()
                else "provider_error"
            )
            return self._base_response(snapshot, state, message=str(error))
        except TimeoutError:
            return self._base_response(
                snapshot,
                "provider_error",
                message=self._l("简历适配生成超过 180 秒，报告未发布", "Résumé adaptation exceeded 180 seconds and was not published."),
            )
        except Exception as error:
            # Provider/SDK details can contain request bodies; never reflect or
            # log the raw exception from this full-resume path.
            logger.exception("resume adaptation failed: %s", type(error).__name__)
            return self._base_response(
                snapshot,
                "provider_error",
                message=self._l("模型服务暂不可用，报告未发布；请检查配置后重试", "The model service is unavailable and the report was not published. Check the configuration and try again."),
            )
        finally:
            await close_llm_client(llm)

    async def _run_with_hard_timeout(
        self,
        user_id: str,
        application_id: int,
        snapshot: dict,
        *,
        research_mode: Literal["snapshot", "no_research"],
        use_summary: bool,
        target_chars: int | None,
        expected_full_input_hash: str,
    ) -> dict:
        """Put the deadline inside the shared task so waiter cancellation is isolated."""
        try:
            async with asyncio.timeout(ADAPTATION_JOB_TIMEOUT_SECONDS):
                return await self._run_shared(
                    user_id,
                    application_id,
                    snapshot,
                    research_mode=research_mode,
                    use_summary=use_summary,
                    target_chars=target_chars,
                    expected_full_input_hash=expected_full_input_hash,
                )
        except TimeoutError:
            return self._base_response(
                snapshot,
                "provider_error",
                message=self._l("简历适配生成超过 180 秒，报告未发布", "Résumé adaptation exceeded 180 seconds and was not published."),
            )

    async def generate(
        self,
        user_id: str,
        application_id: int,
        *,
        refresh: bool,
        expected_resume_id: int | None,
        accept_no_research: bool,
        accept_summarized: bool,
    ) -> dict | None:
        """Run POST semantics; all confirmations are request-scoped."""
        snapshot = self._read_snapshot(user_id, application_id)
        if snapshot["status"] == "missing":
            return None
        # Reuse the exact read-state implementation for every prerequisite that
        # cannot be bypassed by an explicit request-scoped downgrade.  Evaluate
        # the same frozen snapshot used below so a concurrent edit cannot mix
        # prerequisites from one revision with model input from another.
        current = self._inspect_snapshot(snapshot)
        if current is None:
            return None
        bound = snapshot["bound_resume"]
        if expected_resume_id is not None and (
            bound is None or expected_resume_id != bound["id"]
        ):
            return self._base_response(
                snapshot,
                "stale",
                message=self._l("简历选择已变化，请核对当前绑定后重试", "The résumé selection changed. Check the current binding and try again."),
            )
        if current["state"] == "ok" and not refresh:
            return current
        if current["state"] in {
            "no_resume",
            "resume_selection_required",
            "resume_reupload_required",
        }:
            return current
        if bound is None:
            return current
        if current["state"] in {
            "missing_jd",
            "research_running",
            "research_failed",
            "research_required",
            "model_required",
        }:
            return current

        artifact_state = snapshot["research_artifact"].get("artifact_state")
        attempt_state = snapshot["research_attempt"]["attempt_state"]
        if artifact_state == "ready":
            research_mode: Literal["snapshot", "no_research"] = "snapshot"
        elif attempt_state in {"disabled", "unavailable"} and accept_no_research:
            research_mode = "no_research"
        else:
            return current
        if not self._model_available():
            return self._base_response(snapshot, "model_required", message=self._l("请先配置当前可用模型", "Configure an available model first."))

        full = self._input_for_form(
            snapshot,
            research_mode=research_mode,
            resume_input_form="full_text",
        )
        use_summary = False
        target_chars = None
        if not full.capacity.fits:
            if not full.capacity.summarization_available or not accept_summarized:
                response = self._base_response(
                    snapshot,
                    "insufficient_model_capacity",
                    message=self._l("当前模型无法同时容纳完整材料和安全输出预算", "The current model cannot fit the complete materials and a safe output budget."),
                )
                response["summarization_available"] = full.capacity.summarization_available
                response["estimated_input_tokens"] = full.capacity.estimated_input_tokens
                return response
            target_chars = self._choose_summary_target(
                snapshot,
                research_mode=research_mode,
            )
            if target_chars is None:
                return self._base_response(
                    snapshot,
                    "insufficient_model_capacity",
                    message=self._l("即使压缩简历，当前模型也无法保留安全输出预算", "Even with résumé summarization, the current model cannot preserve a safe output budget."),
                )
            use_summary = True

        intent = {
            "application_id": application_id,
            "resume_id": bound["id"],
            "resume_hash": hashlib.sha256(bound["content_text"].encode("utf-8")).hexdigest(),
            "research": self._research_fingerprint(research_mode, snapshot),
            # Full-form input hash also freezes whether jd_parsed survived the
            # deterministic ledger.  Two requests that would send different
            # model payloads must never share a merely coarse semantic task key.
            "full_input_hash": full.input_hash,
            "resume_input_form": "summarized" if use_summary else "full_text",
            "summary_target": target_chars,
            "schema_version": ADAPTATION_SCHEMA_VERSION,
            "prompt_version": ADAPTATION_PROMPT_VERSION,
            "output_locale": self._output_locale,
        }
        intent_hash = _canonical_hash(intent)
        key = (str(Path(self._db_path).resolve()), user_id, application_id, intent_hash)
        tasks = _tasks_for_current_loop()
        task = tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._run_with_hard_timeout(
                    user_id,
                    application_id,
                    snapshot,
                    research_mode=research_mode,
                    use_summary=use_summary,
                    target_chars=target_chars,
                    expected_full_input_hash=full.input_hash,
                )
            )
            tasks[key] = task

            def cleanup(done, task_key=key, registry=tasks):
                if registry.get(task_key) is done:
                    registry.pop(task_key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(cleanup)
        try:
            async with asyncio.timeout(ADAPTATION_JOB_TIMEOUT_SECONDS + 5):
                result = await asyncio.shield(task)
        except TimeoutError:
            result = self._base_response(
                snapshot,
                "provider_error",
                message=self._l("已停止等待；共享任务仍会在硬超时内安全收口", "Waiting has stopped; the shared task will still end safely within its hard timeout."),
            )
        return result

    def input_preview(self, user_id: str, application_id: int) -> dict | None:
        """Return only the actual resume text form a current report would read."""
        snapshot = self._read_snapshot(user_id, application_id)
        if snapshot["status"] == "missing":
            return None
        resume = snapshot.get("bound_resume")
        if resume is None:
            return {"status": "unavailable"}
        cached = self._cached_prepared(snapshot)
        if cached is not None and cached[1].resume_input_form == "summarized":
            summary = self._valid_summary_cache(
                snapshot,
                expected_receipt=cached[1].summary_receipt,
            )
            if summary is not None:
                return {
                    "status": "ok",
                    "resume_id": resume["id"],
                    "resume_name": resume["name"],
                    "input_form": "summarized",
                    "text": summary["summary_text"],
                    "host_limitations": self._host_limitations(cached[1]),
                }
        return {
            "status": "ok",
            "resume_id": resume["id"],
            "resume_name": resume["name"],
            "input_form": "full_text",
            "text": resume["content_text"],
            "host_limitations": [self._l(
                _VISUAL_LIMITATION,
                _VISUAL_LIMITATION_EN,
            )],
        }


__all__ = [
    "ADAPTATION_ARTIFACT_VERSION",
    "ADAPTATION_JOB_TIMEOUT_SECONDS",
    "RESUME_SUMMARY_POLICY_VERSION",
    "ResumeAdaptationWorkflow",
]
