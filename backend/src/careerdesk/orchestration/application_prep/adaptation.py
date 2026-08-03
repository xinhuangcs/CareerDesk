"""Pure deterministic core for resume adaptation.

This module intentionally has no database, HTTP, Tool, Skill, or provider
side effects.  The later workflow may compose these primitives around frozen
feature snapshots without weakening their validation rules.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Literal

from ...platform.ai.structured_tasks import (
    STRUCTURED_CONTEXT_GUARD_TOKENS,
    STRUCTURED_RETRY_INSTRUCTION,
    StructuredTaskCapacityError,
    StructuredTaskValidationError,
    desired_output_tokens,
    effective_context_window,
    structured_input_tokens,
)
from .adaptation_contracts import (
    ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
    ADAPTATION_PROMPT_VERSION,
    ADAPTATION_RUBRIC_VERSION,
    ADAPTATION_SCHEMA_VERSION,
    ADAPTATION_SEGMENT_VERSION,
    ADAPTATION_TASK_OUTPUT_TOKENS,
    ResumeAdaptationReport,
)


ADAPTATION_SEGMENT_MAX_CHARS = 1_200
EXTRACTION_MIN_ALNUM_CHARS = 4
EXTRACTION_REPLACEMENT_MIN_COUNT = 4
EXTRACTION_REPLACEMENT_MAX_RATIO = 0.05
EXTRACTION_CONTROL_MIN_COUNT = 4
EXTRACTION_CONTROL_MAX_RATIO = 0.02

ResumeInputForm = Literal["full_text", "summarized"]
SegmentNamespace = Literal["J", "R"]

_NUMERIC_FACT_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)*(?:[%％])?")
_FACT_PLACEHOLDER_RE = re.compile(r"(?:\[[^\[\]\r\n]+\]|【[^【】\r\n]+】)")
_HAN_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_FACT_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;\r\n]+")
_LATIN_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z][A-Za-z0-9]*(?:(?:\+\+|#)|[./_-][A-Za-z0-9+#]+)*"
    r"(?![A-Za-z0-9])",
)

# These lists deliberately stay narrow.  The host gate is meant to reject
# high-signal factual escalation, not to act as a general-purpose tokenizer or
# decide whether two pieces of prose are semantically equivalent.
_LATIN_TECH_TERMS = frozenset(
    {
        "angular",
        "aws",
        "azure",
        "docker",
        "elasticsearch",
        "flink",
        "gcp",
        "git",
        "golang",
        "graphql",
        "grpc",
        "hadoop",
        "java",
        "javascript",
        "kafka",
        "kubernetes",
        "linux",
        "mongodb",
        "mysql",
        "node.js",
        "nodejs",
        "nosql",
        "postgres",
        "postgresql",
        "python",
        "react",
        "redis",
        "rust",
        "spark",
        "sql",
        "terraform",
        "typescript",
        "vue",
    }
)
_LATIN_NON_FACT_WORDS = frozenset(
    {
        "ability",
        "and",
        "business",
        "candidate",
        "company",
        "customer",
        "develop",
        "development",
        "experience",
        "for",
        "from",
        "good",
        "have",
        "include",
        "including",
        "job",
        "knowledge",
        "must",
        "our",
        "platform",
        "preferred",
        "product",
        "project",
        "required",
        "requirement",
        "responsibilities",
        "responsibility",
        "role",
        "should",
        "skill",
        "strong",
        "system",
        "team",
        "their",
        "the",
        "use",
        "using",
        "with",
        "work",
        "working",
        "years",
    }
)
_HAN_HIGH_SIGNAL_FACT_PARTS = (
    "优化",
    "分割",
    "加载",
    "监控",
    "系统",
    "平台",
    "流程",
    "整改",
    "反洗钱",
    "识别",
    "合规",
    "审计",
    "风控",
    "算法",
    "模型",
    "行业",
    "架构",
    "缓存",
    "队列",
    "数据库",
    "微服务",
    "容器",
    "部署",
    "集成",
    "迁移",
    "治理",
)
_HAN_GENERIC_PHRASES = (
    "具备良好沟通能力",
    "良好的沟通能力",
    "较强的沟通能力",
    "良好的团队协作",
    "沟通能力",
    "团队协作",
    "相关工作经验",
    "以上工作经验",
    "相关经验",
    "工作经验",
    "项目经验",
    "项目成果",
    "工作成果",
    "项目交付",
    "岗位职责",
    "任职要求",
    "工作内容",
    "技能要求",
    "核心职责",
    "核心要求",
    "核心能力",
    "关键成果",
    "优先考虑",
)
_SHORT_SCOPE_FACT_MARKERS = ("定期", "regularly")
_ROLE_ESCALATION_MARKERS = ("主导", "牵头", "负责", "统筹", "带领", "主责")
_ENGLISH_ROLE_ESCALATION_RE = re.compile(
    r"\b(?:led|owned|spearheaded|headed|took\s+ownership|responsible\s+for)\b",
    re.IGNORECASE,
)
_FACT_ADDITION_RE = re.compile(
    r"(?:补充|补写|写明|写入|写上|写出|写成|写|加入|增加|新增|突出|强调|"
    r"体现|展示|说明|描述|提及|陈述|声称|改写|准备)"
    r"|\b(?:add|include|state|mention|claim|highlight|emphasi[sz]e|describe|write|"
    r"prepar(?:e|ed|ing))\b",
    re.IGNORECASE,
)
_NEGATED_FACT_ADDITION_RE = re.compile(
    r"(?:不要|不可|无需|禁止|避免|切勿|不得).{0,6}"
    r"(?:补充|补写|写明|写入|写上|写出|写成|写|加入|增加|新增|突出|强调|"
    r"体现|展示|说明|描述|提及|陈述|声称|改写|准备)"
    r"|\b(?:do\s+not|don't|never|avoid)\s+"
    r"(?:add|include|state|mention|claim|highlight|emphasi[sz]e|describe|write|"
    r"prepar(?:e|ed|ing))\b",
    re.IGNORECASE,
)
_FACT_CONDITION_RE = re.compile(
    r"(?:若|如果|如有|如确有|确实|核实|确认|属实|真实|前提下)"
    r"|\b(?:if|only\s+if|provided\s+that|assuming|verify|verified|"
    r"confirm|confirmed|actually|genuinely|where\s+applicable)\b",
    re.IGNORECASE,
)
_RESEARCH_AS_REQUIREMENT_MARKERS = (
    "调研提及",
    "研究提及",
    "调研要求",
    "研究要求",
)

_CONDITIONAL_ADVICE_PREFIX_ZH = "若你确实有可核实的相关经历，"
_CONDITIONAL_ADVICE_PREFIX_EN = "If you have relevant verifiable experience, "
_SCRIPT_DOMINANCE_MIN_CHARS = 8
_SCRIPT_DOMINANCE_RATIO = 0.8


class AdaptationHostValidationError(StructuredTaskValidationError):
    """A model report failed deterministic host validation and is unpublished."""


@dataclass(frozen=True, slots=True)
class TextSegment:
    segment_id: str
    char_start: int
    char_end: int
    text: str

    def model_payload(self) -> dict:
        return {"segment_id": self.segment_id, "text": self.text}

    def host_evidence(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExtractionQualityReceipt:
    status: Literal["usable", "reupload_required"]
    char_count: int
    non_whitespace_count: int
    alnum_count: int
    replacement_char_count: int
    replacement_ratio: float
    control_char_count: int
    control_ratio: float
    reason_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return self.status == "usable"

    def model_dump(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdaptationCapacityReceipt:
    fits: bool
    reason: Literal[
        "ok",
        "jd_parsed_dropped",
        "resume_only_overflow",
        "insufficient_model_capacity",
        "missing_model_capacity",
        "invalid_model_capacity",
    ]
    include_jd_parsed: bool
    summarization_available: bool
    estimated_input_tokens: int | None
    available_output_tokens: int | None
    required_output_tokens: int
    context_window: int | None


def _unicode_safe_cut(text: str, proposed: int) -> int:
    """Avoid splitting common combining/emoji clusters at a bounded hard cut."""

    def extends_previous(character: str) -> bool:
        codepoint = ord(character)
        return (
            unicodedata.category(character).startswith("M")
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0xE0020 <= codepoint <= 0xE007F
        )

    def is_regional_indicator(character: str) -> bool:
        return 0x1F1E6 <= ord(character) <= 0x1F1FF

    def unsafe_boundary(cut: int) -> bool:
        if cut <= 0 or cut >= len(text):
            return False
        following = text[cut]
        previous = text[cut - 1]
        if extends_previous(following) or following == "\u200d" or previous == "\u200d":
            return True
        if is_regional_indicator(previous) and is_regional_indicator(following):
            preceding_run = 0
            cursor = cut - 1
            while cursor >= 0 and is_regional_indicator(text[cursor]):
                preceding_run += 1
                cursor -= 1
            if preceding_run % 2 == 1:
                return True
        return False

    cut = proposed
    while cut > 0 and unsafe_boundary(cut):
        cut -= 1
    if cut > 0:
        return cut

    # A single hostile/valid grapheme cluster can itself exceed the normal
    # segment bound.  Preserve Unicode integrity by advancing to its end; this
    # is the sole case in which a segment may exceed ``max_chars``.
    cut = proposed
    while cut < len(text) and unsafe_boundary(cut):
        cut += 1
    return cut


def _segment_id(
    namespace: SegmentNamespace,
    version: int,
    ordinal: int,
    text: str,
) -> str:
    digest = hashlib.sha256(
        f"{namespace}{version}\0{ordinal}\0{text}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{namespace}{version}-{ordinal:04d}-{digest}"


def exact_text_segments(
    text: str,
    *,
    namespace: SegmentNamespace,
    version: int = ADAPTATION_SEGMENT_VERSION,
    max_chars: int = ADAPTATION_SEGMENT_MAX_CHARS,
) -> list[TextSegment]:
    """Split text without deleting or normalizing a single character.

    Short physical lines are grouped to keep JSON overhead bounded.  Long
    physical lines are cut only at a Unicode-safe hard boundary.  Newline
    sequences stay in segment text, so concatenation is byte-for-byte stable
    at the Python string level, including CRLF and a final newline.
    """

    if not isinstance(text, str):
        raise TypeError("segment source must be a string")
    if namespace not in {"J", "R"}:
        raise ValueError("segment namespace must be J or R")
    if type(version) is not int or version <= 0:
        raise ValueError("segment version must be a positive integer")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("segment max_chars must be a positive integer")
    if not text:
        return []

    chunks: list[str] = []
    buffered = ""

    def flush() -> None:
        nonlocal buffered
        if buffered:
            chunks.append(buffered)
            buffered = ""

    for physical_line in text.splitlines(keepends=True):
        if len(physical_line) <= max_chars:
            if buffered and len(buffered) + len(physical_line) > max_chars:
                flush()
            buffered += physical_line
            continue

        flush()
        remaining = physical_line
        while len(remaining) > max_chars:
            cut = _unicode_safe_cut(remaining, max_chars)
            chunks.append(remaining[:cut])
            remaining = remaining[cut:]
        buffered = remaining
    flush()

    # ``splitlines`` is total for non-empty strings, but retain a fail-closed
    # invariant in case Python ever changes handling of exotic separators.
    if "".join(chunks) != text:  # pragma: no cover - runtime invariant
        raise AssertionError("exact segment round-trip failed")

    segments: list[TextSegment] = []
    cursor = 0
    for ordinal, chunk in enumerate(chunks, start=1):
        end = cursor + len(chunk)
        segments.append(
            TextSegment(
                segment_id=_segment_id(namespace, version, ordinal, chunk),
                char_start=cursor,
                char_end=end,
                text=chunk,
            )
        )
        cursor = end
    return segments


def segment_is_analyzable(segment: TextSegment | str) -> bool:
    text = segment.text if isinstance(segment, TextSegment) else segment
    return any(character.isalpha() or character.isdigit() for character in text)


def jd_has_meaningful_content(
    jd_text: str | None,
    *,
    company: str,
    position: str,
) -> bool:
    """Reject blank/metadata-only JD text while accepting all Unicode scripts."""

    if not isinstance(jd_text, str) or not jd_text.strip():
        return False
    metadata = {
        value.strip().casefold()
        for value in (company, position)
        if isinstance(value, str) and value.strip()
    }
    for physical_line in jd_text.splitlines():
        candidate = physical_line.strip()
        if not candidate or candidate.casefold() in metadata:
            continue
        if any(character.isalpha() or character.isdigit() for character in candidate):
            return True
    return False


def assess_resume_extraction(
    content_text: str | None,
    *,
    source_suffix: str | None = None,
    parser_failed: bool = False,
) -> ExtractionQualityReceipt:
    """Return a high-confidence extraction gate plus auditable local metrics."""

    text = content_text if isinstance(content_text, str) else ""
    char_count = len(text)
    non_whitespace_count = sum(not character.isspace() for character in text)
    alnum_count = sum(character.isalpha() or character.isdigit() for character in text)
    replacement_count = text.count("\ufffd")
    control_count = sum(
        unicodedata.category(character) == "Cc" and character not in "\t\r\n"
        for character in text
    )
    denominator = max(1, char_count)
    replacement_ratio = replacement_count / denominator
    control_ratio = control_count / denominator
    reasons: list[str] = []
    warnings: list[str] = []

    suffix = (source_suffix or "").strip().casefold()
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix
    if parser_failed:
        reasons.append("parser_failed")
    if not text.strip():
        reasons.append("missing_content_text")
        if suffix == ".pdf":
            reasons.append("scanned_pdf_without_text_layer")
    elif alnum_count < EXTRACTION_MIN_ALNUM_CHARS:
        reasons.append("insufficient_meaningful_text")
    if (
        replacement_count >= EXTRACTION_REPLACEMENT_MIN_COUNT
        and replacement_ratio >= EXTRACTION_REPLACEMENT_MAX_RATIO
    ):
        reasons.append("abnormal_replacement_characters")
    elif replacement_count:
        warnings.append("replacement_characters_present")
    if control_count >= EXTRACTION_CONTROL_MIN_COUNT and control_ratio >= EXTRACTION_CONTROL_MAX_RATIO:
        reasons.append("abnormal_control_characters")
    elif control_count:
        warnings.append("control_characters_present")

    return ExtractionQualityReceipt(
        status="reupload_required" if reasons else "usable",
        char_count=char_count,
        non_whitespace_count=non_whitespace_count,
        alnum_count=alnum_count,
        replacement_char_count=replacement_count,
        replacement_ratio=replacement_ratio,
        control_char_count=control_count,
        control_ratio=control_ratio,
        reason_codes=tuple(dict.fromkeys(reasons)),
        warning_codes=tuple(dict.fromkeys(warnings)),
    )


_RESEARCH_PAYLOAD_FIELDS = frozenset(
    {
        "company_report",
        "position_report",
        "company_source_refs",
        "position_source_refs",
        "coverage_quality",
        "coverage_limitations",
        "source_conflicts",
    }
)
_HOST_ONLY_KEYS = frozenset(
    {
        "application_id",
        "resume_id",
        "snapshot_id",
        "semantic_claim_hash",
        "input_hash",
        "generation",
        "file_path",
        "url",
        "query",
        "generated_time",
        "fresh_until",
    }
)


def _validate_no_host_metadata(value, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string JSON key")
            normalized = key.casefold()
            if normalized in _HOST_ONLY_KEYS or normalized.endswith("_url"):
                raise ValueError(f"{path}.{key} is host-only metadata")
            _validate_no_host_metadata(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_no_host_metadata(nested, path=f"{path}[{index}]")


def _json_copy(value):
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("adaptation payload must contain only JSON data") from error
    return json.loads(encoded)


def build_resume_adaptation_payload(
    *,
    company: str,
    position: str,
    department: str | None,
    jd_segments: list[TextSegment],
    resume_input_form: ResumeInputForm,
    resume_segments: list[TextSegment] | None = None,
    resume_summary_text: str | None = None,
    jd_parsed: dict | None = None,
    research: dict | None = None,
) -> dict:
    """Build the exact untrusted model data plane, excluding host metadata."""

    if not jd_segments:
        raise ValueError("adaptation payload requires JD segments")
    target = {
        "company": company,
        "position": position,
        "department": department,
        "jd_segments": [segment.model_payload() for segment in jd_segments],
    }
    if jd_parsed is not None:
        target["jd_parsed"] = _json_copy(jd_parsed)

    if resume_input_form == "full_text":
        if not resume_segments or resume_summary_text is not None:
            raise ValueError("full_text input requires segments and forbids summary_text")
        resume = {
            "resume_input_form": "full_text",
            "segments": [segment.model_payload() for segment in resume_segments],
        }
    elif resume_input_form == "summarized":
        if not isinstance(resume_summary_text, str) or not resume_summary_text.strip():
            raise ValueError("summarized input requires summary_text")
        if resume_segments:
            raise ValueError("summarized model payload cannot include resume segments")
        resume = {
            "resume_input_form": "summarized",
            "summary_text": resume_summary_text,
        }
    else:  # pragma: no cover - Literal callers; runtime fail-closed
        raise ValueError("unknown resume_input_form")

    payload = {
        "kind": "careerdesk_untrusted_resume_adaptation_input_v1",
        "target": target,
        "resume": resume,
    }
    if research is not None:
        unknown = set(research) - _RESEARCH_PAYLOAD_FIELDS
        if unknown:
            raise ValueError(f"research payload contains unsupported fields: {sorted(unknown)}")
        payload["research"] = _json_copy(research)
    _validate_no_host_metadata(payload)
    return payload


def render_untrusted_json(label: str, data: dict) -> str:
    return (
        f"{label}（不可信 JSON 数据）：\n"
        + json.dumps(
            data,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def adaptation_input_hash(
    model_payload: dict,
    *,
    resume_id: int,
    resume_content_text: str,
    research_fingerprint: str | dict,
    summary_receipt: dict | None = None,
    segment_version: int = ADAPTATION_SEGMENT_VERSION,
    prompt_version: int = ADAPTATION_PROMPT_VERSION,
    rubric_version: int = ADAPTATION_RUBRIC_VERSION,
    schema_version: int = ADAPTATION_SCHEMA_VERSION,
    output_locale: str = "zh-CN",
) -> str:
    """Hash every semantic/model input while keeping raw text out of the receipt."""

    if type(resume_id) is not int or resume_id <= 0:
        raise ValueError("resume_id must be a positive integer")
    if not isinstance(resume_content_text, str):
        raise TypeError("resume_content_text must be a string")
    payload_json = json.dumps(
        model_payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    receipt = {
        "kind": "resume_adaptation_input_fingerprint_v1",
        "payload_hash": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        "resume_id": resume_id,
        "resume_content_hash": hashlib.sha256(
            resume_content_text.encode("utf-8")
        ).hexdigest(),
        "research_fingerprint": research_fingerprint,
        "summary_receipt": summary_receipt,
        "output_locale": output_locale,
        "versions": {
            "segment": segment_version,
            "prompt": prompt_version,
            "rubric": rubric_version,
            "schema": schema_version,
        },
    }
    encoded = json.dumps(
        receipt,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _segment_maps(
    segments: list[TextSegment],
    *,
    namespace: SegmentNamespace,
) -> tuple[dict[str, TextSegment], dict[str, int]]:
    by_id: dict[str, TextSegment] = {}
    indexes: dict[str, int] = {}
    cursor = 0
    prefix = f"{namespace}{ADAPTATION_SEGMENT_VERSION}-"
    for index, segment in enumerate(segments):
        if segment.segment_id in by_id:
            raise AdaptationHostValidationError("segment id 重复")
        if not segment.segment_id.startswith(prefix):
            raise AdaptationHostValidationError("segment id 版本或命名空间无效")
        expected_id = _segment_id(
            namespace,
            ADAPTATION_SEGMENT_VERSION,
            index + 1,
            segment.text,
        )
        if segment.segment_id != expected_id:
            raise AdaptationHostValidationError("segment id ordinal 或内容摘要无效")
        if segment.char_start != cursor or segment.char_end != cursor + len(segment.text):
            raise AdaptationHostValidationError("segment offset 不连续")
        by_id[segment.segment_id] = segment
        indexes[segment.segment_id] = index
        cursor = segment.char_end
    return by_id, indexes


def _require_refs(
    refs: list[str],
    allowed: dict[str, TextSegment],
    *,
    field: str,
) -> None:
    if len(refs) != len(set(refs)):
        raise AdaptationHostValidationError(f"{field} 包含重复引用")
    missing = [ref for ref in refs if ref not in allowed]
    if missing:
        raise AdaptationHostValidationError(f"{field} 包含不存在的引用")


def _evidence(refs: list[str], allowed: dict[str, TextSegment]) -> list[dict]:
    return [allowed[ref].host_evidence() for ref in refs]


def _numeric_facts(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value)
    return {match.group(0).replace(",", "") for match in _NUMERIC_FACT_RE.finditer(normalized)}


def _normalized_fact_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _latin_key_terms(value: str) -> set[str]:
    """Return only high-confidence Latin proper/technical terms.

    Capitalization, acronym punctuation, digits, and a deliberately small
    technology vocabulary are useful deterministic signals.  Ordinary prose
    is excluded so phrases such as "work with the team" do not become facts.
    """

    terms: set[str] = set()
    for match in _LATIN_TERM_RE.finditer(unicodedata.normalize("NFKC", value)):
        raw = match.group(0)
        normalized = raw.casefold()
        if normalized in _LATIN_NON_FACT_WORDS:
            continue
        letters = [character for character in raw if character.isalpha()]
        if len(letters) < 2:
            continue
        is_signaled = (
            normalized in _LATIN_TECH_TERMS
            or any(character.isdigit() for character in raw)
            or any(not character.isalnum() for character in raw)
            or raw.isupper()
            or any(character.isupper() for character in raw)
        )
        if is_signaled:
            terms.add(normalized)
    return terms


def _latin_terms(value: str) -> set[str]:
    """Return normalized Latin tokens for exact source-segment membership."""

    return {
        match.group(0).casefold()
        for match in _LATIN_TERM_RE.finditer(unicodedata.normalize("NFKC", value))
    }


def _is_sentence_initial_token(value: str, start: int) -> bool:
    prefix = value[:start].rstrip()
    return not prefix or prefix[-1] in ".!?。！？;；:\r\n-–—*•"


def _latin_rewrite_fact_terms(value: str) -> set[str]:
    """Find high-confidence technical/proper terms asserted by a rewrite.

    Unlike :func:`_latin_key_terms`, this gate does not require a term to also
    occur in the JD.  That distinction matters because a model can invent an
    unrelated technology, or move one from another resume segment.  Ordinary
    sentence-initial capitalization remains prose and is deliberately ignored.
    """

    normalized_value = unicodedata.normalize("NFKC", value)
    terms: set[str] = set()
    for match in _LATIN_TERM_RE.finditer(normalized_value):
        raw = match.group(0)
        normalized = raw.casefold()
        if normalized in _LATIN_NON_FACT_WORDS:
            continue
        letters = [character for character in raw if character.isalpha()]
        if len(letters) < 2:
            continue
        is_signaled = (
            normalized in _LATIN_TECH_TERMS
            or any(character.isdigit() for character in raw)
            or any(not character.isalnum() for character in raw)
            or raw.isupper()
            or any(character.isupper() for character in raw[1:])
            or (
                raw[0].isupper()
                and not _is_sentence_initial_token(normalized_value, match.start())
            )
        )
        if is_signaled:
            terms.add(normalized)
    return terms


def _novel_rewrite_fact_markers(value: str, *, source_text: str) -> set[str]:
    """Return high-signal facts absent from the rewrite's cited segment."""

    normalized_value = _normalized_fact_text(value)
    normalized_source = _normalized_fact_text(source_text)
    markers = {
        term
        for term in _latin_rewrite_fact_terms(value)
        if term not in _latin_terms(source_text)
    }
    markers.update(
        signal
        for signal in _HAN_HIGH_SIGNAL_FACT_PARTS
        if signal in normalized_value and signal not in normalized_source
    )
    return markers


def _is_generic_han_marker(value: str) -> bool:
    return any(
        value in generic or generic in value
        for generic in _HAN_GENERIC_PHRASES
    )


def _han_key_markers(value: str, *, jd_text: str, resume_text: str) -> set[str]:
    """Find conservative Chinese phrases copied from the JD but not resume.

    Six-character overlap is treated as sufficiently specific on its own.
    Three/four-character overlaps need a technical/compliance signal.  This
    catches compact technical terms without turning common connective
    prose into a factual assertion.
    """

    normalized_jd = _normalized_fact_text(jd_text)
    normalized_resume = _normalized_fact_text(resume_text)
    markers: set[str] = set()
    for run_match in _HAN_RUN_RE.finditer(_normalized_fact_text(value)):
        run = run_match.group(0)
        for width in (6, 4, 3):
            if len(run) < width:
                continue
            for start in range(len(run) - width + 1):
                candidate = run[start : start + width]
                if candidate not in normalized_jd or candidate in normalized_resume:
                    continue
                if _is_generic_han_marker(candidate):
                    continue
                if width < 6 and not any(
                    signal in candidate for signal in _HAN_HIGH_SIGNAL_FACT_PARTS
                ):
                    continue
                markers.add(candidate)
    return markers


def _novel_jd_fact_markers(
    value: str,
    *,
    jd_text: str,
    resume_text: str,
) -> set[str]:
    normalized_value = _normalized_fact_text(value)
    normalized_jd = _normalized_fact_text(jd_text)
    normalized_resume = _normalized_fact_text(resume_text)
    markers = _han_key_markers(value, jd_text=jd_text, resume_text=resume_text)

    jd_latin_terms = _latin_key_terms(jd_text)
    resume_latin_terms = _latin_key_terms(resume_text)
    markers.update(
        term
        for term in _latin_key_terms(value)
        if term in jd_latin_terms and term not in resume_latin_terms
    )
    markers.update(
        marker
        for marker in _SHORT_SCOPE_FACT_MARKERS
        if marker in normalized_value
        and marker in normalized_jd
        and marker not in normalized_resume
    )
    return markers


def _validate_rewrite_role_strengthening(suggestion: str, *, resume_text: str) -> None:
    normalized_suggestion = _normalized_fact_text(suggestion)
    normalized_resume = _normalized_fact_text(resume_text)
    if any(
        marker in normalized_suggestion and marker not in normalized_resume
        for marker in _ROLE_ESCALATION_MARKERS
    ):
        raise AdaptationHostValidationError(
            "rewrite suggestion 引入了简历中不存在的角色强化表述",
        )

    if (
        _ENGLISH_ROLE_ESCALATION_RE.search(normalized_suggestion)
        and not _ENGLISH_ROLE_ESCALATION_RE.search(normalized_resume)
    ):
        raise AdaptationHostValidationError(
            "rewrite suggestion 引入了简历中不存在的角色强化表述",
        )


def _requires_conditional_fact_advice(
    value: str,
    *,
    jd_text: str,
    resume_text: str,
) -> bool:
    """Return whether advice asks to add a high-confidence JD-only fact."""

    # Conditions and negations have scope.  A trailing sentence such as
    # A later formatting caveat cannot make an earlier instruction to invent a JD fact
    # safe, and a generic “avoid unrelated details” clause cannot negate another addition.
    # each factual clause and each addition verb independently.
    for clause in _FACT_CLAUSE_SPLIT_RE.split(value):
        if not clause or not _novel_jd_fact_markers(
            clause,
            jd_text=jd_text,
            resume_text=resume_text,
        ):
            continue
        negated_additions = list(_NEGATED_FACT_ADDITION_RE.finditer(clause))
        conditions = list(_FACT_CONDITION_RE.finditer(clause))
        for addition in _FACT_ADDITION_RE.finditer(clause):
            if any(
                negated.start() <= addition.start()
                and negated.end() >= addition.end()
                for negated in negated_additions
            ):
                continue
            if any(condition.start() <= addition.start() for condition in conditions):
                continue
            return True
    return False


def _validate_conditional_fact_advice(
    value: str,
    *,
    field: str,
    jd_text: str,
    resume_text: str,
) -> None:
    """Require a verification condition before asking to add a JD-only fact."""

    if _requires_conditional_fact_advice(
        value,
        jd_text=jd_text,
        resume_text=resume_text,
    ):
        raise AdaptationHostValidationError(
            f"{field} 补写 JD 独有事实时必须使用条件或核实措辞",
        )


def _script_counts(value: str) -> tuple[int, int]:
    han = sum("\u3400" <= character <= "\u9fff" for character in value)
    latin = sum(
        "a" <= character.casefold() <= "z"
        for character in value
        if len(character.casefold()) == 1
    )
    return han, latin


def _dominant_script(value: str, *, require_minimum: bool) -> Literal["han", "latin"] | None:
    han, latin = _script_counts(value)
    total = han + latin
    if total == 0:
        return None
    minimum = _SCRIPT_DOMINANCE_MIN_CHARS if require_minimum else 1
    if han >= minimum and han / total >= _SCRIPT_DOMINANCE_RATIO:
        return "han"
    if latin >= minimum and latin / total >= _SCRIPT_DOMINANCE_RATIO:
        return "latin"
    return None


def _validate_rewrite_language(suggestion: str, *, source_text: str) -> None:
    """Keep rewrites in the clearly dominant language of their source segment."""

    source_script = _dominant_script(source_text, require_minimum=True)
    if source_script is None:
        return
    suggestion_script = _dominant_script(suggestion, require_minimum=False)
    if suggestion_script != source_script:
        language = "中文" if source_script == "han" else "英文"
        raise AdaptationHostValidationError(
            f"rewrite suggestion 必须保持被引用简历 segment 的明显{language}主语言",
        )


def _conditional_advice_prefix(value: str) -> str:
    han, latin = _script_counts(value)
    if latin > han:
        return _CONDITIONAL_ADVICE_PREFIX_EN
    return _CONDITIONAL_ADVICE_PREFIX_ZH


def _normalize_conditional_advice_fields(
    report: ResumeAdaptationReport,
    *,
    jd_text: str,
    resume_text: str,
) -> ResumeAdaptationReport:
    """Condition unsafe advice deterministically without changing hard facts.

    Only advice prose is eligible for repair here.  Evidence, references,
    numbers, roles, and every other provider field continue through the strict
    host gates unchanged.  Re-validating the complete provider contract makes
    an over-limit repair fail closed instead of silently truncating text.
    """

    payload = report.model_dump(mode="json")
    changed = False
    for item in payload["overall_advice"]:
        for field in ("action", "reason"):
            value = item[field]
            if _requires_conditional_fact_advice(
                value,
                jd_text=jd_text,
                resume_text=resume_text,
            ):
                item[field] = f"{_conditional_advice_prefix(value)}{value}"
                changed = True
    for section in payload["section_reviews"]:
        normalized_improvements = []
        for improvement in section["improvements"]:
            if _requires_conditional_fact_advice(
                improvement,
                jd_text=jd_text,
                resume_text=resume_text,
            ):
                improvement = f"{_conditional_advice_prefix(improvement)}{improvement}"
                changed = True
            normalized_improvements.append(improvement)
        section["improvements"] = normalized_improvements
    normalized_next_steps = []
    for next_step in payload["next_steps"]:
        if _requires_conditional_fact_advice(
            next_step,
            jd_text=jd_text,
            resume_text=resume_text,
        ):
            next_step = f"{_conditional_advice_prefix(next_step)}{next_step}"
            changed = True
        normalized_next_steps.append(next_step)
    payload["next_steps"] = normalized_next_steps

    if not changed:
        return report
    try:
        return ResumeAdaptationReport.model_validate(payload)
    except ValueError as error:
        raise AdaptationHostValidationError(
            "建议条件化归一后超出报告长度或契约限制",
        ) from error


def _validate_rewrite_facts(
    suggestion: str,
    *,
    verification_needed: bool,
    jd_text: str,
    source_text: str,
) -> None:
    """Reject deterministic high-signal fabrication in rewrite suggestions.

    Code cannot prove every semantic claim, but it can prove that a newly
    introduced number or high-signal term never appeared in the cited resume
    segment.  Missing facts must be represented as an explicit placeholder
    paired with the schema flag so the UI cannot mistake them for candidate
    facts.  Segment-local grounding also prevents facts from one resume line
    being silently moved into another line that does not support them.
    """

    novel_numbers = _numeric_facts(suggestion) - _numeric_facts(source_text)
    if novel_numbers:
        raise AdaptationHostValidationError(
            "rewrite suggestion 引入了简历中不存在的数字",
        )
    has_placeholder = _FACT_PLACEHOLDER_RE.search(suggestion) is not None
    if verification_needed and not has_placeholder:
        raise AdaptationHostValidationError(
            "verification_needed=true 的 rewrite 必须使用显式事实占位符",
        )
    if has_placeholder and not verification_needed:
        raise AdaptationHostValidationError(
            "包含事实占位符的 rewrite 必须标记 verification_needed=true",
        )

    # A verified placeholder is the only place where a JD-only term may
    # appear.  Text outside it is rendered by the UI as a proposed resume line
    # and therefore must remain grounded in the frozen resume.
    asserted_text = _FACT_PLACEHOLDER_RE.sub("", suggestion)
    if _novel_jd_fact_markers(
        asserted_text,
        jd_text=jd_text,
        resume_text=source_text,
    ):
        raise AdaptationHostValidationError(
            "rewrite suggestion 在事实占位符外复制了简历中不存在的 JD 事实",
        )
    _validate_rewrite_role_strengthening(asserted_text, resume_text=source_text)
    if _novel_rewrite_fact_markers(asserted_text, source_text=source_text):
        raise AdaptationHostValidationError(
            "rewrite suggestion 引入了引用 segment 未支撑的技术或专有术语",
        )


def _drop_unsafe_optional_rewrites(
    report: ResumeAdaptationReport,
    *,
    jd_text: str,
    resume_by_id: dict[str, TextSegment],
) -> ResumeAdaptationReport:
    """Drop only unsafe optional rewrites while preserving the valid report.

    Rewrites are an optional convenience layer.  A model can produce sound
    requirement analysis and section guidance yet overreach in one proposed
    sentence.  Publishing that sentence is unsafe, but rejecting and paying
    for the entire report again is unnecessary.  Unknown references and other
    structural defects are deliberately left untouched for the normal strict
    validator and fresh retry.
    """

    payload = report.model_dump(mode="json")
    changed = False
    for section in payload["section_reviews"]:
        safe_rewrites = []
        for rewrite in section["rewrites"]:
            source = resume_by_id.get(rewrite["resume_segment_ref"])
            if source is None:
                safe_rewrites.append(rewrite)
                continue
            try:
                _validate_rewrite_language(
                    rewrite["suggestion"],
                    source_text=source.text,
                )
                _validate_rewrite_facts(
                    rewrite["suggestion"],
                    verification_needed=rewrite["verification_needed"],
                    jd_text=jd_text,
                    source_text=source.text,
                )
            except AdaptationHostValidationError:
                changed = True
                continue
            safe_rewrites.append(rewrite)
        section["rewrites"] = safe_rewrites
    if not changed:
        return report
    return ResumeAdaptationReport.model_validate(payload)


def validate_and_materialize_report(
    report: ResumeAdaptationReport,
    *,
    jd_segments: list[TextSegment],
    resume_segments: list[TextSegment],
    resume_input_form: ResumeInputForm,
    output_locale: str = "zh-CN",
) -> dict:
    """Validate every reference/range and refill only host-trusted source text."""

    if not isinstance(report, ResumeAdaptationReport):
        raise TypeError("report must be a ResumeAdaptationReport")
    jd_by_id, _jd_indexes = _segment_maps(jd_segments, namespace="J")
    resume_by_id, resume_indexes = _segment_maps(resume_segments, namespace="R")
    if not jd_by_id:
        raise AdaptationHostValidationError("JD segments 不能为空")
    jd_text = "".join(segment.text for segment in jd_segments)
    resume_text = "".join(segment.text for segment in resume_segments)
    report = _normalize_conditional_advice_fields(
        report,
        jd_text=jd_text,
        resume_text=resume_text,
    )
    report = _drop_unsafe_optional_rewrites(
        report,
        jd_text=jd_text,
        resume_by_id=resume_by_id,
    )

    for item in (*report.requirement_assessments, *report.major_gaps):
        if any(
            marker in item.requirement_summary
            for marker in _RESEARCH_AS_REQUIREMENT_MARKERS
        ):
            raise AdaptationHostValidationError(
                "requirement_summary 不能把调研或研究来源升级为岗位要求",
            )

    for index, requirement in enumerate(report.requirement_assessments):
        _require_refs(
            requirement.jd_segment_refs,
            jd_by_id,
            field=f"requirement_assessments[{index}].jd_segment_refs",
        )
        _require_refs(
            requirement.resume_segment_refs,
            resume_by_id,
            field=f"requirement_assessments[{index}].resume_segment_refs",
        )
    for index, gap in enumerate(report.major_gaps):
        _require_refs(gap.jd_segment_refs, jd_by_id, field=f"major_gaps[{index}].jd_segment_refs")
        _require_refs(
            gap.resume_segment_refs,
            resume_by_id,
            field=f"major_gaps[{index}].resume_segment_refs",
        )

    if resume_input_form == "summarized":
        missing_evidence_marker = (
            "Evidence was not found in the compressed summary"
            if output_locale == "en"
            else "压缩摘要中未见"
        )
        if report.section_reviews:
            raise AdaptationHostValidationError("摘要形态不能包含 section_reviews")
        if any(item.resume_segment_refs for item in report.requirement_assessments):
            raise AdaptationHostValidationError("摘要形态不能引用简历原文 segment")
        if any(item.resume_segment_refs for item in report.major_gaps):
            raise AdaptationHostValidationError("摘要形态不能引用简历原文 segment")
        for item in report.requirement_assessments:
            if (
                item.evidence_state in {"partial", "absent", "uncertain"}
                and missing_evidence_marker not in item.limitation
            ):
                raise AdaptationHostValidationError(
                    "摘要形态的非完整证据 limitation 必须包含“压缩摘要中未见”",
                )
        if any(missing_evidence_marker not in item.basis for item in report.major_gaps):
            raise AdaptationHostValidationError(
                "摘要形态的 major_gap basis 必须包含“压缩摘要中未见”",
            )
    elif resume_input_form != "full_text":  # pragma: no cover - Literal callers
        raise AdaptationHostValidationError("未知 resume_input_form")
    elif any(
        marker in caveat
        for caveat in report.analysis_caveats
        for marker in (
            "压缩摘要", "简历摘要", "摘要形态", "摘要中",
            "compressed summary", "resume summary", "summarized input",
        )
    ):
        raise AdaptationHostValidationError(
            "full_text 形态不能声称使用压缩或简历摘要",
        )

    covered_analyzable: dict[str, int] = {
        segment.segment_id: 0
        for segment in resume_segments
        if segment_is_analyzable(segment)
    }
    for advice_index, advice in enumerate(report.overall_advice):
        _validate_conditional_fact_advice(
            f"{advice.action}\n{advice.reason}",
            field=f"overall_advice[{advice_index}]",
            jd_text=jd_text,
            resume_text=resume_text,
        )
    for next_step_index, next_step in enumerate(report.next_steps):
        _validate_conditional_fact_advice(
            next_step,
            field=f"next_steps[{next_step_index}]",
            jd_text=jd_text,
            resume_text=resume_text,
        )

    previous_end = -1
    section_ranges: list[tuple[int, int]] = []
    for section_index, section in enumerate(report.section_reviews):
        # A one-segment logical section naturally has equal start/end refs; it
        # is not a duplicate citation.
        _require_refs(
            [section.resume_segment_start_ref],
            resume_by_id,
            field=f"section_reviews[{section_index}].range.start",
        )
        _require_refs(
            [section.resume_segment_end_ref],
            resume_by_id,
            field=f"section_reviews[{section_index}].range.end",
        )
        start = resume_indexes[section.resume_segment_start_ref]
        end = resume_indexes[section.resume_segment_end_ref]
        if start > end:
            raise AdaptationHostValidationError("section range 起点晚于终点")
        if start <= previous_end:
            raise AdaptationHostValidationError("section ranges 重叠或顺序错误")
        previous_end = end
        section_ranges.append((start, end))
        for segment in resume_segments[start : end + 1]:
            if segment.segment_id in covered_analyzable:
                covered_analyzable[segment.segment_id] += 1
        for improvement_index, improvement in enumerate(section.improvements):
            _validate_conditional_fact_advice(
                improvement,
                field=(
                    f"section_reviews[{section_index}]."
                    f"improvements[{improvement_index}]"
                ),
                jd_text=jd_text,
                resume_text=resume_text,
            )
        for rewrite_index, rewrite in enumerate(section.rewrites):
            _require_refs(
                [rewrite.resume_segment_ref],
                resume_by_id,
                field=f"section_reviews[{section_index}].rewrites[{rewrite_index}]",
            )
            rewrite_position = resume_indexes[rewrite.resume_segment_ref]
            if not start <= rewrite_position <= end:
                raise AdaptationHostValidationError("rewrite 引用不在所属 section range 内")
            _validate_rewrite_language(
                rewrite.suggestion,
                source_text=resume_by_id[rewrite.resume_segment_ref].text,
            )
            _validate_rewrite_facts(
                rewrite.suggestion,
                verification_needed=rewrite.verification_needed,
                jd_text=jd_text,
                source_text=resume_by_id[rewrite.resume_segment_ref].text,
            )

    if resume_input_form == "full_text" and report.mode == "full":
        if covered_analyzable and not report.section_reviews:
            raise AdaptationHostValidationError("full 模式缺少 section_reviews")
        if any(count != 1 for count in covered_analyzable.values()):
            raise AdaptationHostValidationError("analyzable resume segments 未恰好覆盖一次")

    materialized = report.model_dump(mode="json")
    for item, source in zip(
        materialized["requirement_assessments"],
        report.requirement_assessments,
        strict=True,
    ):
        item["jd_evidence"] = _evidence(source.jd_segment_refs, jd_by_id)
        item["resume_evidence"] = _evidence(source.resume_segment_refs, resume_by_id)
    for item, source in zip(materialized["major_gaps"], report.major_gaps, strict=True):
        item["jd_evidence"] = _evidence(source.jd_segment_refs, jd_by_id)
        item["resume_evidence"] = _evidence(source.resume_segment_refs, resume_by_id)
    for ordinal, (item, source, (start, end)) in enumerate(
        zip(materialized["section_reviews"], report.section_reviews, section_ranges, strict=True),
        start=1,
    ):
        digest = hashlib.sha256(
            f"{ordinal}\0{source.section_name}\0{source.resume_segment_start_ref}\0"
            f"{source.resume_segment_end_ref}".encode("utf-8")
        ).hexdigest()[:8]
        item["section_id"] = f"S1-{ordinal:04d}-{digest}"
        item["resume_segment_range"] = {
            "start": resume_segments[start].host_evidence(),
            "end": resume_segments[end].host_evidence(),
        }
        for rewrite in item["rewrites"]:
            rewrite["original_text"] = resume_by_id[
                rewrite["resume_segment_ref"]
            ].text
    return materialized


def provider_report_from_materialized(value) -> ResumeAdaptationReport:
    """Strictly project one host-enriched report back to its provider contract.

    Unknown fields are deliberately retained so ``extra=forbid`` rejects them;
    only the exact, host-owned enrichment keys are removed.  Generation and
    cache validation share this one projection, preventing the provider model
    returned to evaluators from drifting from user-visible materialized text.
    """

    if not isinstance(value, dict):
        raise AdaptationHostValidationError("materialized report 必须是 object")

    provider_payload = dict(value)

    requirements = value.get("requirement_assessments")
    if isinstance(requirements, list):
        provider_payload["requirement_assessments"] = [
            {
                key: nested
                for key, nested in item.items()
                if key not in {"jd_evidence", "resume_evidence"}
            }
            if isinstance(item, dict)
            else item
            for item in requirements
        ]

    gaps = value.get("major_gaps")
    if isinstance(gaps, list):
        provider_payload["major_gaps"] = [
            {
                key: nested
                for key, nested in item.items()
                if key not in {"jd_evidence", "resume_evidence"}
            }
            if isinstance(item, dict)
            else item
            for item in gaps
        ]

    sections = value.get("section_reviews")
    if isinstance(sections, list):
        provider_sections = []
        for item in sections:
            if not isinstance(item, dict):
                provider_sections.append(item)
                continue
            provider_item = {
                key: nested
                for key, nested in item.items()
                if key not in {"section_id", "resume_segment_range"}
            }
            rewrites = item.get("rewrites")
            if isinstance(rewrites, list):
                provider_item["rewrites"] = [
                    {
                        key: nested
                        for key, nested in rewrite.items()
                        if key != "original_text"
                    }
                    if isinstance(rewrite, dict)
                    else rewrite
                    for rewrite in rewrites
                ]
            provider_sections.append(provider_item)
        provider_payload["section_reviews"] = provider_sections

    try:
        return ResumeAdaptationReport.model_validate(provider_payload)
    except (TypeError, ValueError) as error:
        raise AdaptationHostValidationError(
            "materialized report 无法投影为 provider 契约",
        ) from error


def validate_cached_materialized_report(
    value,
    *,
    jd_segments: list[TextSegment],
    resume_segments: list[TextSegment],
    resume_input_form: ResumeInputForm,
    output_locale: str = "zh-CN",
) -> dict:
    """Revalidate a persisted host-enriched report against current source text.

    A cache entry is not trusted merely because its input fingerprint matches:
    older builds, manual database edits, or partial writes can leave a report
    with an obsolete shape or forged host evidence.  Project only the exact
    enrichment fields produced by :func:`validate_and_materialize_report`,
    validate the remaining provider contract, rematerialize it from the
    current segments, and require byte-for-byte JSON equality.  The equality
    check also rejects unknown nested fields that would otherwise survive a
    ``dict``-typed response boundary.  Legacy unsafe advice is normalized only
    during rematerialization and therefore fails this equality check closed.
    """

    try:
        report = provider_report_from_materialized(value)
        rematerialized = validate_and_materialize_report(
            report,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form=resume_input_form,
            output_locale=output_locale,
        )
    except (TypeError, ValueError) as error:
        raise AdaptationHostValidationError("cached report 契约或引用无效") from error
    if rematerialized != value:
        raise AdaptationHostValidationError("cached report 宿主证据或字段形状无效")
    return rematerialized


def _capacity_for_payload(
    llm,
    *,
    system_prompt: str,
    payload: str,
    schema_model,
) -> tuple[int, int, int]:
    context_window = effective_context_window(llm)
    budget_payload = f"{payload}\n\n{STRUCTURED_RETRY_INSTRUCTION}"
    input_tokens = structured_input_tokens(system_prompt, budget_payload, schema_model)
    provider_reserve = desired_output_tokens(llm, ADAPTATION_TASK_OUTPUT_TOKENS)
    context_reserve = context_window - input_tokens - STRUCTURED_CONTEXT_GUARD_TOKENS
    available = max(0, min(provider_reserve, context_reserve))
    return context_window, input_tokens, available


def preflight_adaptation_capacity(
    llm,
    *,
    system_prompt: str,
    payload_with_jd_parsed: str,
    payload_without_jd_parsed: str | None = None,
    payload_without_resume: str | None = None,
    schema_model=ResumeAdaptationReport,
    required_output_tokens: int = ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
) -> AdaptationCapacityReceipt:
    """Reserve the full 16,384-token output before any adaptation model call.

    The helper deterministically retries the ledger without ``jd_parsed`` first.
    If that still fails, an optional no-resume ledger distinguishes a resume-only
    overflow (eligible for explicit summary confirmation) from general capacity
    failure.  It performs no provider call.
    """

    if type(required_output_tokens) is not int or required_output_tokens <= 0:
        raise ValueError("required_output_tokens must be positive")

    try:
        context, input_tokens, available = _capacity_for_payload(
            llm,
            system_prompt=system_prompt,
            payload=payload_with_jd_parsed,
            schema_model=schema_model,
        )
    except StructuredTaskCapacityError as error:
        reason = (
            "missing_model_capacity"
            if error.reason == "missing_model_capacity"
            else "invalid_model_capacity"
        )
        return AdaptationCapacityReceipt(
            fits=False,
            reason=reason,
            include_jd_parsed=True,
            summarization_available=False,
            estimated_input_tokens=None,
            available_output_tokens=None,
            required_output_tokens=required_output_tokens,
            context_window=None,
        )
    if available >= required_output_tokens:
        return AdaptationCapacityReceipt(
            fits=True,
            reason="ok",
            include_jd_parsed=True,
            summarization_available=False,
            estimated_input_tokens=input_tokens,
            available_output_tokens=available,
            required_output_tokens=required_output_tokens,
            context_window=context,
        )

    selected_input = input_tokens
    selected_available = available
    include_jd_parsed = True
    if payload_without_jd_parsed is not None:
        context, selected_input, selected_available = _capacity_for_payload(
            llm,
            system_prompt=system_prompt,
            payload=payload_without_jd_parsed,
            schema_model=schema_model,
        )
        include_jd_parsed = False
        if selected_available >= required_output_tokens:
            return AdaptationCapacityReceipt(
                fits=True,
                reason="jd_parsed_dropped",
                include_jd_parsed=False,
                summarization_available=False,
                estimated_input_tokens=selected_input,
                available_output_tokens=selected_available,
                required_output_tokens=required_output_tokens,
                context_window=context,
            )

    summary_available = False
    if payload_without_resume is not None:
        _context, _input, no_resume_available = _capacity_for_payload(
            llm,
            system_prompt=system_prompt,
            payload=payload_without_resume,
            schema_model=schema_model,
        )
        summary_available = no_resume_available >= required_output_tokens

    return AdaptationCapacityReceipt(
        fits=False,
        reason=("resume_only_overflow" if summary_available else "insufficient_model_capacity"),
        include_jd_parsed=include_jd_parsed,
        summarization_available=summary_available,
        estimated_input_tokens=selected_input,
        available_output_tokens=selected_available,
        required_output_tokens=required_output_tokens,
        context_window=context,
    )


__all__ = [
    "ADAPTATION_SEGMENT_MAX_CHARS",
    "AdaptationCapacityReceipt",
    "AdaptationHostValidationError",
    "ExtractionQualityReceipt",
    "TextSegment",
    "adaptation_input_hash",
    "assess_resume_extraction",
    "build_resume_adaptation_payload",
    "exact_text_segments",
    "jd_has_meaningful_content",
    "preflight_adaptation_capacity",
    "provider_report_from_materialized",
    "render_untrusted_json",
    "segment_is_analyzable",
    "validate_cached_materialized_report",
    "validate_and_materialize_report",
]
