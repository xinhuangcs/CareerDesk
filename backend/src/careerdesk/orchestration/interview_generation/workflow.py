"""Deterministic workflow for basic/custom material-driven question sets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

from ...features.applications import public as applications
from ...features.grill import public as grill
from ...features.questions import public as questions
from ...features.questions.public import GeneratedQuestionSet
from ...features.resumes import public as resumes
from ...platform.ai.structured_tasks import (
    STRUCTURED_CONTEXT_GUARD_TOKENS,
    conservative_tokens,
    desired_output_tokens,
    structured_input_tokens,
)
from ...platform.ai.providers import resolve_model_capabilities
from ...platform.database import read_connection, transaction
from ...platform.locale import DEFAULT_OUTPUT_LOCALE, OutputLocale
from . import ai_tasks

POLICY_VERSION = "interview-policy-v2"
SCHEMA_VERSION = "question-set-v4"
RUBRIC_VERSION = "rubric-v1"
SEGMENTATION_VERSION = "material-segments-v2"
MAX_QUESTIONS = 30
_QUESTION_TOKEN_RESERVE = 700
_EVIDENCE_SEGMENT_CHARS = 800
_BASIC_CATEGORIES = (
    "hr_motivation", "resume_deep_dive", "behavioral_situational",
    "professional_domain", "case_work_sample",
)
_CUSTOM_CATEGORIES = (*_BASIC_CATEGORIES, "business_company")

_EMAIL = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_ACCOUNT = re.compile(r"(?i)\b(?:wechat|weixin|linkedin|github|qq|id|account)\s*[:=]\s*\S+")
_SENSITIVE = re.compile(
    r"(?i)(age|gender|sexual orientation|marital|pregnan|religion|politic|disab|medical history|"
    r"年龄|性别|婚育|孕产|民族|国籍|宗教|政治立场|残障|病史|照片)"
)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def current_policy_fingerprint() -> str:
    """Return the policy identity that controls whether a generated set is startable."""
    return _hash({"policy": POLICY_VERSION, "prompt": ai_tasks.GENERATION_PROMPT_VERSION,
                  "schema": SCHEMA_VERSION, "rubric": RUBRIC_VERSION,
                  "segments": SEGMENTATION_VERSION,
                  "summary": ai_tasks.SUMMARY_POLICY_VERSION, "redaction": "pii-pattern-v1"})


def _redact(text: str) -> str:
    result = _EMAIL.sub("[邮箱已隐去]", text)
    result = _URL.sub("[链接已隐去]", result)
    result = _PHONE.sub("[电话已隐去]", result)
    return _ACCOUNT.sub("[账号已隐去]", result)


def _bounded_segments(segments: list[dict]) -> list[dict]:
    """Give the model bounded refs while the host retains excerpt ownership."""
    result = []
    for segment in segments:
        text = segment.get("text")
        ref_id = segment.get("id")
        if not isinstance(text, str) or not isinstance(ref_id, str):
            continue
        parts = [text[start:start + _EVIDENCE_SEGMENT_CHARS]
                 for start in range(0, len(text), _EVIDENCE_SEGMENT_CHARS)] or [""]
        for index, part in enumerate(parts, start=1):
            bounded = {"id": ref_id, "text": part}
            if isinstance(segment.get("basis_kind"), str):
                bounded["basis_kind"] = segment["basis_kind"]
            if len(parts) > 1:
                suffix = f".{index}"
                if len(ref_id) + len(suffix) <= 80:
                    bounded["id"] = ref_id + suffix
                else:
                    digest = hashlib.sha256(ref_id.encode("utf-8")).hexdigest()[:12]
                    bounded["id"] = f"{ref_id[:64]}.{digest}.{index}"[:80]
            bounded["text"] = part
            result.append(bounded)
    return result


def _generic_segments(text: str, prefix: str, *, size: int = _EVIDENCE_SEGMENT_CHARS) -> list[dict]:
    result = []
    for start in range(0, len(text), size):
        end = min(len(text), start + size)
        result.append({"id": f"{prefix}{len(result) + 1}", "text": text[start:end]})
    return result


@dataclass(frozen=True)
class FrozenInput:
    edition: str
    resume_id: int
    application_id: int | None
    context_label: str
    materials: tuple[dict, ...]
    material_claim: dict

    @property
    def material_fingerprint(self) -> str:
        return _hash(self.material_claim)


def freeze_input(db_path: str, user_id: str, *, edition: str, resume_id: int | None = None,
                 application_id: int | None = None, conn=None) -> FrozenInput:
    if edition == "basic":
        if resume_id is None:
            raise ValueError("resume_selection_required")
        if conn is None:
            with read_connection(db_path) as owned:
                owned.execute("BEGIN")
                snapshot = resumes.resume_generation_snapshot_in_transaction(owned, user_id, resume_id)
        else:
            snapshot = resumes.resume_generation_snapshot_in_transaction(conn, user_id, resume_id)
        if snapshot is None:
            raise ValueError("no_resume")
        if snapshot["archived"] or not snapshot["content_text"] or not snapshot["segments"]:
            raise ValueError("resume_reupload_required")
        material = {"kind": "resume", "hash": snapshot["content_hash"],
                    "segments": _bounded_segments(snapshot["segments"])}
        claim = {"user_id": user_id, "edition": "basic", "resume_id": resume_id,
                 "resume_hash": snapshot["content_hash"],
                 "extraction_receipt": snapshot["extraction_receipt"]}
        return FrozenInput("basic", resume_id, None, snapshot["name"], (material,), claim)

    if edition != "custom" or application_id is None:
        raise ValueError("application_selection_required")
    frozen = (
        applications.freeze_resume_adaptation_input_in_transaction(conn, user_id, application_id)
        if conn is not None
        else applications.freeze_resume_adaptation_input(db_path, user_id, application_id)
    )
    if frozen.get("status") != "ok":
        raise ValueError("application_selection_required")
    bound = frozen.get("bound_resume")
    if not isinstance(bound, dict):
        raise ValueError("no_resume")
    if not bound.get("content_text") or not bound.get("segments"):
        raise ValueError("resume_reupload_required")
    jd_text = frozen.get("jd_text")
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise ValueError("missing_jd")
    actual_jd_hash = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()
    resume_material = {"kind": "resume", "hash": bound["content_hash"],
                       "segments": _bounded_segments(bound["segments"])}
    jd_segments = _generic_segments(jd_text, "J")
    jd_material = {"kind": "jd", "hash": actual_jd_hash, "segments": jd_segments}
    materials = [resume_material, jd_material]
    claim = {"user_id": user_id, "edition": "custom", "application_id": application_id,
             "company": frozen["company"], "position": frozen["position"],
             "department": frozen.get("department"), "resume_id": bound["id"],
             "resume_hash": bound["content_hash"], "jd_hash": actual_jd_hash}
    return FrozenInput("custom", bound["id"], application_id,
                       f"{frozen['company']} · {frozen['position']}", tuple(materials), claim)


def start_current_session(db_path: str, user_id: str, *, question_set_id: int,
                          question_count: int) -> tuple[int, dict, int]:
    """Atomically reject stale packs and create a thin Grill session."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pack = questions.question_set_start_snapshot_in_transaction(conn, user_id, question_set_id)
        if pack is None or pack["state"] != "ready" or pack["archived_at"] is not None:
            raise ValueError("题集不存在、未就绪或已归档")
        if pack["kind"] != "generated":
            raise ValueError("历史题集仅供查看，不能开始新的练习")
        if pack["policy_fingerprint"] != current_policy_fingerprint():
            raise ValueError("题集策略版本已过期，请重新生成")
        try:
            current = freeze_input(
                db_path, user_id, edition=pack["edition"], resume_id=pack["resume_id"],
                application_id=pack["application_id"], conn=conn,
            )
        except ValueError as exc:
            raise ValueError("题集材料已变化，请重新生成") from exc
        if current.material_fingerprint != pack["material_fingerprint"]:
            raise ValueError("题集材料已变化，请重新生成")
        return grill.create_session_in_transaction(
            conn, user_id, question_set_id=question_set_id, question_count=question_count,
        )


def capacity_for(materials: tuple[dict, ...], *, context_window: int | None,
                 max_output_tokens: int | None) -> dict:
    if not context_window or not max_output_tokens:
        return {"state": "blocked", "code": "model_capacity_required", "effective_question_limit": 0,
                "compressed_materials": [], "extra_calls": 0}
    llm = SimpleNamespace(context_window=context_window, max_output_tokens=max_output_tokens)
    schema_cost = structured_input_tokens(ai_tasks.GENERATION_PROMPT, "{}", GeneratedQuestionSet)
    output = desired_output_tokens(llm, ai_tasks.QUESTION_OUTPUT_TOKENS)
    effective_limit = min(MAX_QUESTIONS, max(1, output // _QUESTION_TOKEN_RESERVE))
    input_budget = context_window - schema_cost - output - STRUCTURED_CONTEXT_GUARD_TOKENS
    costs = {item["kind"]: conservative_tokens(json.dumps(item, ensure_ascii=False)) for item in materials}
    if any(cost >= input_budget for cost in costs.values()):
        return {"state": "blocked", "code": "insufficient_model_capacity",
                "effective_question_limit": effective_limit, "compressed_materials": [], "extra_calls": 0}
    if sum(costs.values()) <= input_budget:
        return {"state": "direct", "effective_question_limit": effective_limit,
                "compressed_materials": [], "extra_calls": 0}
    if len(materials) == 1:
        return {"state": "blocked", "code": "insufficient_model_capacity",
                "effective_question_limit": effective_limit, "compressed_materials": [], "extra_calls": 0}
    total = sum(costs.values())
    compressed = []
    for kind, cost in sorted(costs.items(), key=lambda pair: pair[1], reverse=True):
        compressed.append(kind)
        total -= cost
        total += min(cost // 5, 12_000)
        if total <= input_budget:
            break
    return {"state": "compressed", "effective_question_limit": effective_limit,
            "compressed_materials": compressed, "extra_calls": len(compressed)}


def readiness(db_path: str, user_id: str, *, edition: str, resume_id: int | None,
              application_id: int | None, context_window: int | None,
              max_output_tokens: int | None) -> dict:
    requirements: dict = {}
    if edition == "basic" and resume_id is not None:
        snapshot = next(
            (item for item in resumes.list_resume_summaries(db_path, user_id)
             if item["id"] == resume_id), None,
        )
        requirements["resume"] = {
            "ready": bool(snapshot and snapshot.get("content_hash")),
            "label": snapshot.get("name") if snapshot else None,
            "character_count": snapshot.get("character_count") if snapshot else None,
        }
    elif edition == "custom" and application_id is not None:
        detail = applications.freeze_resume_adaptation_input(db_path, user_id, application_id)
        if detail.get("status") == "ok":
            bound = detail.get("bound_resume")
            jd_text = detail.get("jd_text")
            requirements["resume"] = {
                "ready": isinstance(bound, dict) and bool(bound.get("content_text")),
                "label": bound.get("name") if isinstance(bound, dict) else None,
            }
            requirements["jd"] = {
                "present": bool(isinstance(jd_text, str) and jd_text.strip()),
            }
    messages = {
        "resume_selection_required": "请选择一份简历",
        "no_resume": "当前选择没有可用简历",
        "resume_reupload_required": "简历正文不可用，请重新上传",
        "application_selection_required": "请选择一个岗位",
        "missing_jd": "当前岗位缺少岗位描述，请先前往岗位详情补充",
    }
    try:
        frozen = freeze_input(db_path, user_id, edition=edition, resume_id=resume_id,
                              application_id=application_id)
    except ValueError as exc:
        code = str(exc)
        return {"ready": False, "code": code, "message": messages.get(code, code),
                "capacity": None, "requirements": requirements}
    cap = capacity_for(frozen.materials, context_window=context_window,
                       max_output_tokens=max_output_tokens)
    return {"ready": cap["state"] != "blocked", "code": cap.get("code"), "capacity": cap,
            "context_label": frozen.context_label, "requirements": requirements}


def _materialize(result: GeneratedQuestionSet, frozen: FrozenInput, limit: int) -> tuple[list[dict], dict]:
    if len(result.questions) > limit:
        raise ValueError("question_limit_exceeded")
    ref_map = {}
    for material in frozen.materials:
        basis = {"resume": "resume", "jd": "jd", "research": None}[material["kind"]]
        for segment in material["segments"]:
            ref_basis = segment.get("basis_kind") or basis
            ref_map[segment["id"]] = (ref_basis, material["hash"], segment["text"])
    items = []
    seen = set()
    allowed_categories = (
        _CUSTOM_CATEGORIES if frozen.edition == "custom" else _BASIC_CATEGORIES
    )
    policy_omissions = 0
    for question in result.questions:
        raw = question.model_dump(mode="json")
        # A category outside this edition is not unsafe content and must not
        # poison otherwise valid questions.  Drop it before evidence handling;
        # the prompt and payload also constrain the model, while this remains
        # the deterministic publication boundary.
        if raw["category"] not in allowed_categories:
            policy_omissions += 1
            continue
        if _SENSITIVE.search(raw["text"] + " " + raw["answer_guide"]):
            continue
        normalized = re.sub(r"\s+", "", raw["text"]).casefold()
        if normalized in seen:
            continue
        evidence = []
        invalid = False
        for ref in raw["evidence_refs"]:
            actual = ref_map.get(ref["ref_id"])
            if actual is None or actual[0] != ref["basis_kind"]:
                invalid = True
                break
            source_text = actual[2]
            if not source_text or len(source_text) > _EVIDENCE_SEGMENT_CHARS:
                invalid = True
                break
            evidence.append({"basis_kind": actual[0], "source_hash": actual[1],
                             "ref_id": ref["ref_id"], "excerpt": _redact(source_text),
                             "source_start": 0, "source_end": len(source_text),
                             "redaction_version": "pii-pattern-v1", "limitations": raw["limitations"]})
        if invalid:
            raise ValueError("invalid_evidence_ref")
        raw["text"] = _redact(raw["text"])
        raw["answer_guide"] = {"kind": "coaching_guide", "text": _redact(raw["answer_guide"])}
        raw["evidence"] = evidence
        raw["answer_authority"] = "model_generated_unverified"
        raw["repeat_scope"] = ("application" if "jd" in raw["basis_kinds"]
                               else "resume" if "resume" in raw["basis_kinds"] else "global")
        raw.pop("evidence_refs")
        raw.pop("basis_kinds")
        raw.pop("limitations")
        seen.add(normalized)
        items.append(raw)
    coverage = result.coverage.model_dump(mode="json")
    covered_categories = {
        item["category"] for item in items
    }
    coverage["covered_categories"] = [
        category for category in allowed_categories if category in covered_categories
    ]
    coverage["omitted_categories"] = [
        category for category in allowed_categories if category not in covered_categories
    ]
    coverage["published_question_count"] = len(items)
    coverage["policy_omissions"] = policy_omissions
    coverage["safety_omissions"] = (
        len(result.questions) - len(items) - policy_omissions
    )
    return items, coverage


_SAFE_GENERATION_FAILURE_CODES = frozenset({
    "invalid_summary_ref",
    "invalid_evidence_ref",
    "no_supported_questions",
    "publication_conflict",
    "question_limit_exceeded",
})


def _safe_generation_failure_code(error: ValueError) -> str:
    """Keep programming/provider detail out of persisted user-visible status."""
    code = str(error)
    return code if code in _SAFE_GENERATION_FAILURE_CODES else "unexpected_generation_error"


class InterviewGenerationWorkflow:
    def __init__(self, db_path: str, llm, *, model_label: str | None,
                 context_window: int | None, max_output_tokens: int | None):
        context_window, max_output_tokens = resolve_model_capabilities(
            model_label,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
        self.db_path = db_path
        self.llm = llm
        self.model_label = model_label
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self._pending: dict | None = None

    async def generate(self, user_id: str, *, edition: str, resume_id: int | None,
                       application_id: int | None, client_command_id: str,
                       refresh: bool, output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
                       enqueue_only: bool = False) -> dict:
        if self.llm is None:
            return {"status": "error", "code": "model_required", "message": "请先配置模型"}
        try:
            frozen = freeze_input(self.db_path, user_id, edition=edition, resume_id=resume_id,
                                  application_id=application_id)
        except ValueError as exc:
            return {"status": "error", "code": str(exc), "message": str(exc)}
        cap = capacity_for(frozen.materials, context_window=self.context_window,
                           max_output_tokens=self.max_output_tokens)
        if cap["state"] == "blocked":
            return {"status": "error", "code": cap["code"], "message": cap["code"]}
        policy_fingerprint = current_policy_fingerprint()
        model_fingerprint = _hash({"model": self.model_label, "context": self.context_window,
                                   "output": self.max_output_tokens})
        generation_fingerprint = _hash({"material": frozen.material_fingerprint,
                                        "policy": policy_fingerprint, "capacity": cap,
                                        "model": model_fingerprint,
                                        "output_locale": output_locale})
        request_digest = _hash({"edition": edition, "resume_id": resume_id,
                                "application_id": application_id, "refresh": refresh,
                                "output_locale": output_locale})
        generation = uuid4().hex
        metadata = {"edition": edition, "resume_id": frozen.resume_id,
                    "application_id": frozen.application_id,
                    "generation": generation, "material_fingerprint": frozen.material_fingerprint,
                    "policy_fingerprint": policy_fingerprint,
                    "generation_fingerprint": generation_fingerprint,
                    "prompt_version": ai_tasks.GENERATION_PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
                    "rubric_version": RUBRIC_VERSION, "segmentation_version": SEGMENTATION_VERSION,
                    "summary_policy_version": ai_tasks.SUMMARY_POLICY_VERSION, "model_label": self.model_label,
                    "context_label": frozen.context_label,
                    "content_locale": output_locale,
                    "input_receipt": {"materials": frozen.material_claim, "capacity": cap,
                                      "effective_question_limit": cap["effective_question_limit"],
                                      "model_fingerprint": model_fingerprint,
                                      "content_locale": output_locale}}
        try:
            claim = questions.claim_generation(
                self.db_path, user_id, client_command_id=client_command_id,
                request_digest=request_digest, refresh=refresh, metadata=metadata,
            )
        except ValueError as exc:
            return {"status": "error", "code": "command_conflict", "message": str(exc)}
        if claim.generation is None:
            if claim.status == "completed":
                return {"status": "ready", "question_set_id": claim.question_set_id}
            if claim.status == "failed":
                return {"status": "error", "question_set_id": claim.question_set_id,
                        "code": claim.safe_error_code or "generation_failed",
                        "message": claim.safe_error_code or "题集生成失败，请重试"}
            return {"status": "processing", "question_set_id": claim.question_set_id}
        set_id = claim.question_set_id
        execution = {
            "user_id": user_id, "set_id": set_id, "generation": generation,
            "frozen": frozen, "cap": cap, "edition": edition, "resume_id": resume_id,
            "application_id": application_id, "output_locale": output_locale,
        }
        if enqueue_only:
            self._pending = execution
            return {"status": "processing", "question_set_id": set_id}
        return await self._execute(**execution)

    async def run_pending(self) -> dict:
        """Run the already persisted claim owned by this request-local workflow."""
        if self._pending is None:
            return {"status": "error", "code": "no_pending_generation"}
        execution, self._pending = self._pending, None
        return await self._execute(**execution)

    async def _execute(self, *, user_id: str, set_id: int, generation: str,
                       frozen: FrozenInput, cap: dict, edition: str,
                       resume_id: int | None, application_id: int | None,
                       output_locale: OutputLocale) -> dict:
        try:
            prepared = []
            compressed = set(cap["compressed_materials"])
            if compressed:
                questions.update_generation_stage(self.db_path, user_id, set_id, generation, "summarizing")
            for material in frozen.materials:
                if material["kind"] in compressed:
                    summary = await ai_tasks.summarize_material(self.llm, material)
                    valid = {segment["id"]: segment for segment in material["segments"]}
                    if any(
                        ref.ref_id not in valid
                        or ref.basis_kind != (valid[ref.ref_id].get("basis_kind") or material["kind"])
                        for point in summary.points for ref in point.refs
                    ):
                        raise ValueError("invalid_summary_ref")
                    prepared.append({"kind": material["kind"], "hash": material["hash"],
                                     "summary": summary.model_dump(mode="json")})
                else:
                    prepared.append(material)
            questions.update_generation_stage(self.db_path, user_id, set_id, generation, "generating")
            result = await ai_tasks.generate_question_set(self.llm, {
                "kind": "careerdesk_untrusted_question_set_input_v1", "edition": edition,
                "allowed_categories": list(
                    _CUSTOM_CATEGORIES if edition == "custom" else _BASIC_CATEGORIES
                ),
                "effective_question_limit": cap["effective_question_limit"],
                "capacity_mode": cap["state"], "materials": prepared,
                "output_locale": output_locale,
            }, output_locale=output_locale)
            items, coverage = _materialize(result, frozen, cap["effective_question_limit"])
            if not items:
                raise ValueError("no_supported_questions")
            current = freeze_input(self.db_path, user_id, edition=edition, resume_id=resume_id,
                                   application_id=application_id)
            if current.material_fingerprint != frozen.material_fingerprint:
                questions.fail_generation(self.db_path, user_id, set_id, generation, "input_changed")
                return {"status": "error", "question_set_id": set_id, "code": "input_changed"}
            if not questions.publish_generation(
                self.db_path, user_id, set_id, generation,
                expected_material_fingerprint=frozen.material_fingerprint,
                coverage=coverage, items=items,
            ):
                raise ValueError("publication_conflict")
            return {"status": "ready", "question_set_id": set_id}
        except ai_tasks.InterviewAITaskError as exc:
            questions.fail_generation(self.db_path, user_id, set_id, generation, str(exc))
            return {"status": "error", "question_set_id": set_id, "code": str(exc), "message": str(exc)}
        except ValueError as exc:
            code = _safe_generation_failure_code(exc)
            questions.fail_generation(self.db_path, user_id, set_id, generation, code)
            return {"status": "error", "question_set_id": set_id, "code": code,
                    "message": "题集生成意外失败，请重试" if code == "unexpected_generation_error" else code}
        except asyncio.CancelledError:
            questions.fail_generation(self.db_path, user_id, set_id, generation, "outcome_unknown")
            raise
        except Exception:
            code = "unexpected_generation_error"
            questions.fail_generation(self.db_path, user_id, set_id, generation, code)
            return {"status": "error", "question_set_id": set_id, "code": code,
                    "message": "题集生成意外失败，请重试"}


__all__ = ["InterviewGenerationWorkflow", "capacity_for", "current_policy_fingerprint",
           "freeze_input", "readiness", "start_current_session"]
