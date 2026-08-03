"""Assistant orchestration for durable turn fencing, streaming, attachments, and resources."""

import asyncio
import inspect
import logging
import re
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agentmaker import (
    Guardrail,
    GuardrailResult,
    GuardrailTripwireError,
    Hook,
    LLMConfigError,
    Message,
    content_text,
)

from ...core.config import get_settings
from ...features.applications.public import (
    ApplicationUpdateOperationDTO,
    WORKBOOK_SUFFIXES,
    parse_standard_workbook,
    standard_positions_from_structured_text,
)
from ...platform.ai.client import MODEL_CAPABILITY_MESSAGE, close_llm_client
from ...platform.locale import DEFAULT_OUTPUT_LOCALE, OutputLocale
from ...platform.runtime.recovery_scope import derive_recovery_scope
from ...platform.storage.documents import DOCUMENT_SUFFIXES, extract_document_text
from ...platform.storage.uploads import (
    CHAT_UPLOAD_TTL_SECONDS,
    MAX_CHAT_OR_RESUME_BYTES,
    MAX_CHAT_STORAGE_BYTES,
    UploadTooLarge as PlatformUploadTooLarge,
    cleanup_stale_files,
    save_upload,
    user_upload_root,
)
from . import ledger
from .contracts import (
    CHAT_ATTACHMENT_CHAR_LIMIT,
    IMAGE_SUFFIXES,
    MAX_CHAT_PROPOSAL_OPERATIONS,
    ChatRecoveryScopeResponse,
    ChatStreamEvent,
    ChatTurnStatusResponse,
    ChatUiActionReference,
    TrustedOperationType,
)
from .turn_cancellation import (
    TurnCancellationControl,
    TurnCancellationNotReversible,
    TurnCancellationRequested,
    register_active_turn,
    reject_cancelled_turn_proposals,
    request_active_turn_cancel,
    unregister_active_turn,
)

logger = logging.getLogger(__name__)

_RECOVERY_SCOPE_DOMAIN = b"careerdesk:assistant-recovery-scope:v1\0"

# Tool names map to safe progress labels; unknown internal names are never exposed.
_TOOL_STATUS_LABELS_ZH = {
    "record_review": "整理进展…",
    "parse_jobs": "识别岗位…",
    "query_timeline": "查看求职进展…",
    "query_study": "查看学习记录…",
    "query_library": "查看资料…",
    "query_grill": "查看练习记录…",
    "query_prep": "查看面试准备…",
    "request_application_prep": "准备公司与岗位调研…",
    "query_status": "查看近况…",
    "conversation_search": "查找历史对话…",
    "preferences_read": "读取偏好…",
    "preferences_update": "更新偏好…",
    "update_application": "更新投递记录…",
    "delete_application": "准备删除确认…",
    "manage_review": "修改复盘…",
}
_TOOL_STATUS_LABELS_EN = {
    "record_review": "Organizing job-search updates…",
    "parse_jobs": "Identifying roles…",
    "query_timeline": "Reviewing application progress…",
    "query_study": "Reviewing study records…",
    "query_library": "Reviewing saved materials…",
    "query_grill": "Reviewing practice history…",
    "query_prep": "Reviewing interview preparation…",
    "request_application_prep": "Preparing company and role research…",
    "query_status": "Reviewing recent notes…",
    "conversation_search": "Searching past conversations…",
    "preferences_read": "Reading preferences…",
    "preferences_update": "Updating preferences…",
    "update_application": "Updating application records…",
    "delete_application": "Preparing deletion confirmation…",
    "manage_review": "Editing review notes…",
}
_TOOL_STATUS_LABELS = _TOOL_STATUS_LABELS_ZH


def _l(locale: OutputLocale, zh: str, en: str) -> str:
    return en if locale == "en" else zh


def _tool_status_label(locale: OutputLocale, name: str) -> str | None:
    labels = _TOOL_STATUS_LABELS_ZH if locale == "zh-CN" else _TOOL_STATUS_LABELS_EN
    return labels.get(name)


def _tool_status_variant(name: str, parameters) -> str:
    if name != "preferences" or not isinstance(parameters, dict):
        return name
    return (
        "preferences_update"
        if parameters.get("action") == "apply"
        else "preferences_read"
    )


class ChatUploadTooLarge(ValueError):
    """Transport-neutral assistant upload size or quota failure."""


class ChatTurnRejected(RuntimeError):
    """Turn conflict or uncertainty known before SSE response headers start."""

    def __init__(self, data: dict, *, retry_after: int | None = None):
        super().__init__(data["message"])
        self.data = data
        self.retry_after = retry_after


@dataclass(slots=True)
class PreparedChat:
    """Inspected or claimed turn; execute mode owns the unique attempt token."""

    mode: Literal["execute", "replay", "error"]
    settings: Any
    user_id: str
    session_id: str
    client_turn_id: str
    output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE
    review_supplement_reference: str | None = None
    message_payload: Any = None
    image_paths: tuple[Path, ...] = ()
    attempt_token: str | None = None
    replay_events: tuple[dict, ...] = ()
    error: dict | None = None
    agent_factory: Any = None
    request_llm: Any = None
    direct_standard_intake: dict[str, Any] | None = None
    direct_review_record: bool = False
    execution_started: bool = False
    execution_stopped: bool = False
    cancellation: TurnCancellationControl = field(default_factory=TurnCancellationControl)


class _DeferredSessionStore:
    """Hold the current turn's history until cancellation is no longer admissible."""

    def __init__(self, backing) -> None:
        self._backing = backing
        self._batch: tuple[list[Message], object] | None = None

    async def aload(self, *, scope=None, all_scopes: bool = False):
        return await self._backing.aload(scope=scope, all_scopes=all_scopes)

    async def aappend_many(self, messages: list[Message], *, scope=None) -> None:
        if self._batch is not None:
            raise RuntimeError("assistant turn attempted multiple history commits")
        self._batch = (list(messages), scope)

    async def commit(self) -> None:
        if self._batch is None:
            return
        messages, scope = self._batch
        await self._backing.aappend_many(messages, scope=scope)
        self._batch = None


_STAGE_CORRECTION_CUE = re.compile(r"改回|改成|改为|调整到|调整为|回到|恢复到")
_LOCALE_PREFIX_MAX_CHARS = 96
_COMPLETED_WRITE_CAPABILITIES_ZH = {
    "更新": "update",
    "修改": "change",
    "调整": "adjust",
    "设置": "set",
    "保存": "save",
    "清空": "clear",
    "删除": "delete",
    "撤销": "undo",
    "合并": "merge",
    "导入": "import",
    "记录": "record",
}
_COMPLETED_WRITE_CAPABILITIES_EN = {
    "updated": "update",
    "changed": "change",
    "set": "set",
    "saved": "save",
    "cleared": "clear",
    "deleted": "delete",
    "undone": "undo",
    "merged": "merge",
    "imported": "import",
    "recorded": "record",
}
_COMPLETED_WRITE_VERBS_ZH = tuple(_COMPLETED_WRITE_CAPABILITIES_ZH)
_COMPLETED_WRITE_VERBS_EN = tuple(_COMPLETED_WRITE_CAPABILITIES_EN)
_COMPLETED_WRITE_CLAIM = re.compile(
    r"(?:^|[\n。！？；，,])\s*(?:[-*]\s*)?(?:(?:我|我们)\s*)?(?:已|已经)"
    r"(?:为你|帮你)?(?:成功)?(?P<verb>" + "|".join(_COMPLETED_WRITE_VERBS_ZH) + r")"
    r"(?!的)",
)
_COMPLETED_WRITE_CLAIM_EN = re.compile(
    r"\b(?:I|we)(?:['’]ve| have)?\s+(?:successfully\s+)?"
    r"(?P<verb>" + "|".join(_COMPLETED_WRITE_VERBS_EN) + r")\b",
    re.IGNORECASE,
)
_STREAMING_WRITE_CLAIMS_ZH = tuple(
    (f"{subject}{completed}{actor}{success}{verb}", capability)
    for subject in ("", "我", "我们")
    for completed in ("已", "已经")
    for actor in ("", "为你", "帮你")
    for success in ("", "成功")
    for verb, capability in _COMPLETED_WRITE_CAPABILITIES_ZH.items()
)
_STREAMING_WRITE_CLAIMS_EN = tuple(
    (f"{subject}{success} {verb}", capability)
    for subject in ("i", "i've", "i have", "we", "we've", "we have")
    for success in ("", " successfully")
    for verb, capability in _COMPLETED_WRITE_CAPABILITIES_EN.items()
)
_WRITE_CAPABILITIES_BY_OPERATION = {
    "application_update": frozenset({
        "update", "change", "adjust", "set", "save", "clear", "record",
    }),
    "review_timeline_entry_edit": frozenset({
        "update", "change", "adjust", "set", "save", "clear", "record",
    }),
    "preference_update": frozenset({
        "update", "change", "adjust", "set", "save", "clear", "record",
    }),
}
_WRITE_OPERATION_BY_TOOL = {
    "update_application": "application_update",
    "manage_review": "review_timeline_entry_edit",
    "preferences": "preference_update",
}
_STAGE_ALIASES = (
    ("不再跟进", "withdrawn"),
    ("撤回申请", "withdrawn"),
    ("主动放弃", "withdrawn"),
    ("泡池子", "pooled"),
    ("笔试", "written_test"),
    ("测评", "written_test"),
    ("面试", "interviewing"),
    ("Offer", "offer"),
    ("offer", "offer"),
    ("已投递", "applied"),
    ("待定", "backlog"),
    ("已挂", "rejected"),
    ("被拒", "rejected"),
    ("backlog", "backlog"),
    ("applied", "applied"),
    ("written test", "written_test"),
    ("assessment", "written_test"),
    ("interviewing", "interviewing"),
    ("interview", "interviewing"),
    ("offer", "offer"),
    ("rejected", "rejected"),
    ("withdrawn", "withdrawn"),
    ("pooled", "pooled"),
)
_STAGE_CORRECTION_CUE_EN = re.compile(
    r"\b(?:change|set|move|put|return|switch|update)(?:\s+\w+){0,4}\s+(?:to|back)\b",
    re.IGNORECASE,
)


def _requested_stage_correction(text: str) -> str | None:
    if not (_STAGE_CORRECTION_CUE.search(text) or _STAGE_CORRECTION_CUE_EN.search(text)):
        return None
    lowered = text.lower()
    return next((stage for label, stage in _STAGE_ALIASES if label.lower() in lowered), None)


def _completed_write_capabilities(text: str, output_locale: OutputLocale) -> tuple[str, ...]:
    pattern = _COMPLETED_WRITE_CLAIM_EN if output_locale == "en" else _COMPLETED_WRITE_CLAIM
    capabilities = (
        _COMPLETED_WRITE_CAPABILITIES_EN
        if output_locale == "en"
        else _COMPLETED_WRITE_CAPABILITIES_ZH
    )
    return tuple(capabilities[match.group("verb").lower()] for match in pattern.finditer(text))


def _known_application_identities(db_path: str, user_id: str) -> tuple[tuple[str, str], ...]:
    """Best-effort read used only to bind an explicit correction to the named local target."""
    try:
        from ...features.applications import public as applications

        board = applications.board(db_path, user_id)
        return tuple(
            (item["company"], item["position"])
            for column in board["columns"].values()
            for item in column
            if isinstance(item.get("company"), str)
            and isinstance(item.get("position"), str)
        )
    except Exception:  # noqa: BLE001 -- attestation stays fail-closed without blocking Chat setup
        logger.exception("failed to load application identities for write attestation")
        return ()


def _valid_application_update_batch(
    data: dict,
    expected_client_turn_id: str | None = None,
) -> bool:
    """Validate the bounded receipt envelope before it can attest a write."""
    if set(data) != {
        "operation_type",
        "state",
        "requested_count",
        "changed_count",
        "no_change_count",
        "results",
    } or data.get("operation_type") != "application_update_batch":
        return False
    state = data.get("state")
    requested_count = data.get("requested_count")
    changed_count = data.get("changed_count")
    no_change_count = data.get("no_change_count")
    results = data.get("results")
    if (
        state not in {"completed", "no_change"}
        or type(requested_count) is not int
        or not 1 <= requested_count <= 20
        or type(changed_count) is not int
        or type(no_change_count) is not int
        or changed_count < 0
        or no_change_count < 0
        or not isinstance(results, list)
        or len(results) != requested_count
        or changed_count + no_change_count != requested_count
    ):
        return False
    completed_items = 0
    no_change_items = 0
    indexes: set[int] = set()
    application_ids: set[int] = set()
    operation_ids: set[str] = set()
    client_turn_ids: set[str] = set()
    for item in results:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            return False
        indexes.add(item["index"])
        if item.get("status") == "completed":
            operation = item.get("operation")
            if set(item) != {"index", "status", "operation"} or not isinstance(
                operation, dict,
            ):
                return False
            try:
                validated_operation = ApplicationUpdateOperationDTO.model_validate(operation)
            except (TypeError, ValueError):
                return False
            if validated_operation.state != "completed":
                return False
            application_id = validated_operation.target.application_id
            if (
                application_id in application_ids
                or validated_operation.operation_id in operation_ids
            ):
                return False
            application_ids.add(application_id)
            operation_ids.add(validated_operation.operation_id)
            client_turn_ids.add(validated_operation.client_turn_id)
            completed_items += 1
        elif (
            item.get("status") == "no_change"
            and set(item) == {"index", "status", "application_id", "company", "position"}
            and type(item.get("application_id")) is int
            and item["application_id"] > 0
            and isinstance(item.get("company"), str)
            and bool(item["company"].strip())
            and isinstance(item.get("position"), str)
            and bool(item["position"].strip())
            and item["application_id"] not in application_ids
        ):
            application_ids.add(item["application_id"])
            no_change_items += 1
        else:
            return False
    return (
        indexes == set(range(requested_count))
        and completed_items == changed_count
        and no_change_items == no_change_count
        and len(client_turn_ids) <= 1
        and (
            expected_client_turn_id is None
            or not client_turn_ids
            or client_turn_ids == {expected_client_turn_id}
        )
        and ((state == "completed") == (changed_count > 0))
    )


class _VerifiedWriteAttestation(Guardrail):
    """Reject completion claims that are not backed by this request's trusted Tool receipt."""

    failure_message_zh = (
        "这次没有取得支持该完成声明的可信回执，因此我不能把它当作已经完成。"
        "相关记录可能仍是原状态；请以页面确认卡或可信操作收据为准。"
    )
    failure_message_en = (
        "I did not receive a trusted receipt supporting that completion claim, so I cannot treat "
        "it as completed. The related records may still be unchanged; rely on the page confirmation "
        "or trusted operation receipt."
    )

    def __init__(
        self,
        request_text: str,
        known_applications: tuple[tuple[str, str], ...] = (),
        output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
        expected_client_turn_id: str | None = None,
    ) -> None:
        self.failure_message = _l(
            output_locale,
            self.failure_message_zh,
            self.failure_message_en,
        )
        self._output_locale = output_locale
        self._required_stage = _requested_stage_correction(request_text)
        mentioned = [
            identity for identity in known_applications
            if identity[0] in request_text
        ]
        required_identities: list[tuple[str, str]] = []
        for company in dict.fromkeys(company for company, _position in mentioned):
            company_identities = [
                identity for identity in mentioned if identity[0] == company
            ]
            position_matches = [
                identity for identity in company_identities if identity[1] in request_text
            ]
            position_matches = [
                identity
                for identity in position_matches
                if not any(
                    identity[1] != other[1] and identity[1] in other[1]
                    for other in position_matches
                )
            ]
            required_identities.extend(position_matches or company_identities)
        self._required_identities = tuple(required_identities)
        self._expected_client_turn_id = expected_client_turn_id
        self._verified_operation_types: set[str] = set()
        self._stage_correction_verified = self._required_stage is None

    def observe_tool(self, name: str, parameters: object, result: object) -> None:
        if not isinstance(parameters, dict):
            return
        status = getattr(result, "status", None)
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            return
        operation_type = _WRITE_OPERATION_BY_TOOL.get(name)
        preference_result = data.get("result")
        changed_count = (
            preference_result.get("changed_count")
            if isinstance(preference_result, dict)
            else None
        )
        operation_completed = (
            operation_type == "review_timeline_entry_edit"
            and data.get("operation_type") == operation_type
            and data.get("state") == "completed"
        ) or (
            operation_type == "application_update"
            and _valid_application_update_batch(data, self._expected_client_turn_id)
            and data["changed_count"] > 0
        ) or (
            operation_type == "preference_update"
            and data.get("operation_type") == operation_type
            and isinstance(data.get("effects"), list)
            and isinstance(changed_count, int)
            and changed_count > 0
        )
        if status == "success" and operation_completed and operation_type is not None:
            self._verified_operation_types.add(operation_type)
        if name != "update_application" or self._required_stage is None:
            return
        if data.get("operation_type") == "application_update_batch":
            if not _valid_application_update_batch(data, self._expected_client_turn_id):
                return
            updates = parameters.get("updates")
            results = data.get("results")
            if not isinstance(updates, list) or not isinstance(results, list):
                return
            results_by_index = {
                item["index"]: item
                for item in results
                if isinstance(item, dict) and isinstance(item.get("index"), int)
            }
            candidates = [
                (index, item)
                for index, item in enumerate(updates)
                if isinstance(item, dict) and item.get("new_stage") == self._required_stage
            ]
            verified_identities: set[tuple[str, str]] = set()
            verified_without_identity = False
            for index, _item in candidates:
                batch_item = results_by_index.get(index)
                if not isinstance(batch_item, dict):
                    continue
                operation = (
                    batch_item.get("operation")
                    if isinstance(batch_item.get("operation"), dict)
                    else {}
                )
                before = (
                    operation.get("before")
                    if isinstance(operation.get("before"), dict)
                    else {}
                )
                final = (
                    operation.get("final")
                    if isinstance(operation.get("final"), dict)
                    else {}
                )
                actual_company = before.get("company", batch_item.get("company"))
                actual_position = before.get("position", batch_item.get("position"))
                if not isinstance(actual_company, str) or not isinstance(actual_position, str):
                    continue
                actual_identity = (actual_company, actual_position)
                if self._required_identities and actual_identity not in self._required_identities:
                    continue
                completed = (
                    batch_item.get("status") == "completed"
                    and operation.get("operation_type") == "application_update"
                    and operation.get("state") == "completed"
                    and final.get("stage") == self._required_stage
                )
                already_current = batch_item.get("status") == "no_change"
                if completed or already_current:
                    if self._required_identities:
                        verified_identities.add(actual_identity)
                    else:
                        verified_without_identity = True
            self._stage_correction_verified = (
                set(self._required_identities) <= verified_identities
                if self._required_identities
                else verified_without_identity
            )

    def check(self, text: str) -> GuardrailResult:
        unsupported_claim = any(
            not self.allows_completion_claim(capability)
            for capability in _completed_write_capabilities(text, self._output_locale)
        )
        passed = self._stage_correction_verified and not unsupported_claim
        return GuardrailResult(
            passed=passed,
            message="" if passed else self.failure_message,
        )

    @property
    def required_stage_receipt_pending(self) -> bool:
        """Hold every model delta until an explicit stage correction has a matching receipt."""
        return self._required_stage is not None and not self._stage_correction_verified

    def allows_completion_claim(self, capability: str) -> bool:
        return self._stage_correction_verified and any(
            capability in _WRITE_CAPABILITIES_BY_OPERATION[operation_type]
            for operation_type in self._verified_operation_types
        )


class _OutputLocaleGuardrail(Guardrail):
    """Block only an unmistakably whole-answer language mismatch."""

    def __init__(self, output_locale: OutputLocale) -> None:
        self._output_locale = output_locale

    def check(self, text: str) -> GuardrailResult:
        han_count = sum("\u3400" <= character <= "\u9fff" for character in text)
        latin_words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", text)
        if self._output_locale == "en":
            wrong = han_count >= 20 and len(latin_words) < 12
            message = (
                "I could not produce a reliable answer in the selected language. "
                "Review any operation receipts shown on the page before retrying this message."
            )
        else:
            wrong = len(latin_words) >= 20 and han_count < 8
            message = "本轮没有生成可靠的中文回答；重新发送前，请先核对页面中可能出现的操作收据。"
        return GuardrailResult(passed=not wrong, message=message if wrong else "")


class _StreamingOutputGate:
    """Publish genuine model deltas while withholding only unresolved safety prefixes."""

    def __init__(
        self,
        write_attestation: _VerifiedWriteAttestation,
        output_locale: OutputLocale,
    ) -> None:
        self._write_attestation = write_attestation
        self._locale_guardrail = _OutputLocaleGuardrail(output_locale)
        self._output_locale = output_locale
        self._full_parts: list[str] = []
        self._locale_buffer = ""
        self._locale_safe = False
        self._han_count = 0
        self._latin_word_count = 0
        self._latin_word_active = False
        self._latin_connector_pending = False
        self._claim_candidate = ""
        self._withheld_claim = ""
        self._withheld_claim_capability: str | None = None

    def _observe_locale_evidence(self, piece: str) -> None:
        for character in piece:
            if "\u3400" <= character <= "\u9fff":
                self._han_count += 1
            if character.isascii() and character.isalpha():
                if not self._latin_word_active:
                    self._latin_word_count += 1
                self._latin_word_active = True
                self._latin_connector_pending = False
            elif character in "'’-" and self._latin_word_active and not self._latin_connector_pending:
                self._latin_connector_pending = True
            else:
                self._latin_word_active = False
                self._latin_connector_pending = False

    def _locale_prefix_ready(self) -> bool:
        if len(self._locale_buffer) >= _LOCALE_PREFIX_MAX_CHARS:
            return True
        return self._latin_word_count >= 12 if self._output_locale == "en" else self._han_count >= 8

    def _claim_status(self) -> tuple[Literal["pending", "unsafe", "safe"], str | None]:
        candidate = self._claim_candidate
        if self._output_locale == "zh-CN":
            normalized = candidate
            claims = _STREAMING_WRITE_CLAIMS_ZH

            def is_boundary(character: str) -> bool:
                return character != "的"
        else:
            normalized = re.sub(r"\s+", " ", candidate.lower().replace("’", "'"))
            claims = _STREAMING_WRITE_CLAIMS_EN

            def is_boundary(character: str) -> bool:
                return not (character.isalnum() or character == "_")

        for claim, capability in claims:
            if normalized == claim:
                return "pending", capability
            if normalized.startswith(claim):
                return (
                    ("unsafe", capability)
                    if is_boundary(normalized[len(claim)])
                    else ("safe", None)
                )
        pending = any(claim.startswith(normalized) for claim, _capability in claims)
        return ("pending", None) if pending else ("safe", None)

    def _scan_completion_claims(self, text: str) -> list[str]:
        if self._withheld_claim:
            if (
                self._withheld_claim_capability is not None
                and self._write_attestation.allows_completion_claim(
                    self._withheld_claim_capability,
                )
            ):
                pending = self._withheld_claim + text
                self._withheld_claim = ""
                self._withheld_claim_capability = None
                return self._scan_completion_claims(pending)
            self._withheld_claim += text
            return []

        released: list[str] = []
        remaining = deque(text)
        while remaining:
            character = remaining.popleft()
            if not self._claim_candidate:
                claim_start = (
                    character in {"已", "我"}
                    if self._output_locale == "zh-CN"
                    else character.lower() in {"i", "w"}
                )
                if claim_start:
                    self._claim_candidate = character
                else:
                    released.append(character)
                continue

            self._claim_candidate += character
            status, capability = self._claim_status()
            if status == "unsafe":
                if (
                    capability is None
                    or not self._write_attestation.allows_completion_claim(capability)
                ):
                    self._withheld_claim = self._claim_candidate + "".join(remaining)
                    self._withheld_claim_capability = capability
                    self._claim_candidate = ""
                    text = "".join(released)
                    return [text] if text else []
                released.append(self._claim_candidate)
                self._claim_candidate = ""
                continue
            if status == "safe":
                rejected = self._claim_candidate
                self._claim_candidate = ""
                released.append(rejected[0])
                remaining.extendleft(reversed(rejected[1:]))

        text = "".join(released)
        return [text] if text else []

    def feed(self, piece: str) -> list[str]:
        """Return immediately publishable model text for one upstream delta."""
        self._full_parts.append(piece)
        self._observe_locale_evidence(piece)
        if self._write_attestation.required_stage_receipt_pending:
            self._locale_buffer += piece
            return []

        if not self._locale_safe:
            self._locale_buffer += piece
            if not self._locale_prefix_ready():
                return []
            self._locale_safe = True
            piece, self._locale_buffer = self._locale_buffer, ""

        return self._scan_completion_claims(piece)

    def finish(self) -> list[str]:
        """Validate the terminal text and release any short safe suffix still withheld."""
        full_text = "".join(self._full_parts)
        for guardrail in (self._locale_guardrail, self._write_attestation):
            decision = guardrail.check(full_text)
            if not decision.passed:
                raise GuardrailTripwireError(decision.message)

        released: list[str] = []
        if not self._locale_safe:
            self._locale_safe = True
            released.extend(self._scan_completion_claims(self._locale_buffer))
            self._locale_buffer = ""
        if self._claim_candidate:
            claim_candidate = self._claim_candidate
            _status, capability = self._claim_status()
            self._claim_candidate = ""
            if (
                capability is not None
                and not self._write_attestation.allows_completion_claim(capability)
            ):
                raise GuardrailTripwireError(self._write_attestation.failure_message)
            released.append(claim_candidate)
        if self._withheld_claim:
            withheld_claim = self._withheld_claim
            capability = self._withheld_claim_capability
            self._withheld_claim = ""
            self._withheld_claim_capability = None
            if (
                capability is None
                or not self._write_attestation.allows_completion_claim(capability)
            ):
                raise GuardrailTripwireError(self._write_attestation.failure_message)
            released.append(withheld_claim)
        return released


class _ToolStatusHook(Hook):
    """Push tool lifecycle events into the current request queue."""

    def __init__(self, queue: asyncio.Queue, record_proposal=None, write_attestation=None,
                 cancellation: TurnCancellationControl | None = None,
                 expected_client_turn_id: str | None = None):
        self._queue = queue
        self._record_proposal = record_proposal
        self._write_attestation = write_attestation
        self._cancellation = cancellation
        self._expected_client_turn_id = expected_client_turn_id

    def before_model(self, _messages):
        if self._cancellation is not None:
            self._cancellation.checkpoint()

    def before_tool(self, name, parameters):
        if self._cancellation is not None:
            self._cancellation.begin_tool()
        trusted_operation_type = _trusted_operation_type(name, parameters)
        status_variant = _tool_status_variant(name, parameters)
        self._queue.put_nowait(("tool", name, trusted_operation_type, status_variant))

    def after_tool(self, name, parameters, result):
        try:
            if self._write_attestation is not None:
                self._write_attestation.observe_tool(name, parameters, result)
            for surface, operation_id in _proposal_surfaces_for_tool_result(name, result):
                if self._record_proposal is not None:
                    self._record_proposal(surface, operation_id)
                self._queue.put_nowait(("proposal", surface, operation_id))
            for action in _ui_actions_for_tool_result(name, result):
                self._queue.put_nowait(("ui_action", action))
        finally:
            if self._cancellation is not None:
                self._cancellation.finish_tool(
                    committed=_tool_result_committed(
                        name,
                        result,
                        expected_client_turn_id=self._expected_client_turn_id,
                    ),
                )


_TOOL_UI_ACTIONS = {
    "query_timeline": {"open_application", "open_timeline"},
    "query_prep": {"open_application_research", "open_resume_adaptation"},
    "request_application_prep": {"open_application_research"},
    "query_grill": {"open_grill_session", "open_grill"},
    "query_study": {"open_questions"},
    "query_library": {"open_library", "open_resume"},
}


def _ui_actions_for_tool_result(name: str, result: object) -> list[dict[str, object]]:
    """Accept only allowlisted navigation emitted by a successful local Tool."""
    if getattr(result, "status", None) != "success":
        return []
    allowed = _TOOL_UI_ACTIONS.get(name)
    data = getattr(result, "data", None)
    if allowed is None or not isinstance(data, dict):
        return []
    candidates = data.get("ui_actions")
    if not isinstance(candidates, list) or len(candidates) > 8:
        return []
    actions: list[dict[str, object]] = []
    seen: set[tuple[str, int | None]] = set()
    for candidate in candidates:
        try:
            action = ChatUiActionReference.model_validate(candidate)
        except (TypeError, ValueError):
            return []
        if action.kind not in allowed:
            return []
        identity = (action.kind, action.resource_id)
        if identity in seen:
            continue
        seen.add(identity)
        actions.append(action.model_dump(exclude_none=True))
    return actions


def _canonical_operation_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return value if str(uuid.UUID(value)) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _proposal_surfaces_for_tool_result(
    name: str,
    result: object,
) -> list[tuple[str, str]]:
    """Reveal only exact proposals returned successfully by this Tool invocation."""
    if getattr(result, "status", None) != "success":
        return []
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return []
    if (
        name == "delete_application"
        and data.get("operation_type") == "application_delete_batch"
        and data.get("state") == "pending"
    ):
        operations = data.get("operations")
        if (not isinstance(operations, list)
                or not (1 <= len(operations) <= MAX_CHAT_PROPOSAL_OPERATIONS)):
            return []
        surfaces: list[tuple[str, str]] = []
        seen: set[str] = set()
        for operation in operations:
            if not isinstance(operation, dict):
                return []
            operation_id = _canonical_operation_id(operation.get("operation_id"))
            if (
                operation.get("operation_type") != "application_delete"
                or operation.get("state") != "pending"
                or operation_id is None
                or operation_id in seen
            ):
                return []
            seen.add(operation_id)
            surfaces.append(("application_delete", operation_id))
        return surfaces

    operation_id = _canonical_operation_id(data.get("operation_id"))
    if operation_id is None:
        return []
    if name == "parse_jobs":
        return [("intake", operation_id)] if data.get("status") == "preview" else []
    if data.get("state") != "pending":
        return []
    surface = {
        ("update_application", "application_merge"): "application_merge",
        ("delete_application", "application_delete"): "application_delete",
        ("manage_review", "review_undo"): "review_undo",
    }.get((name, data.get("operation_type")))
    return [(surface, operation_id)] if surface is not None else []


def _proposal_surface_for_tool_result(
    name: str,
    result: object,
) -> tuple[str, str] | None:
    """Backward-compatible singular helper used by focused contract tests."""
    surfaces = _proposal_surfaces_for_tool_result(name, result)
    return surfaces[0] if len(surfaces) == 1 else None


def _trusted_operation_type(
    name: str,
    parameters: object,
) -> TrustedOperationType | None:
    """Mark turn-scoped operation families only from trusted tool names and strict actions."""
    if not isinstance(parameters, dict):
        return None
    if name == "update_application":
        return "application_update"
    if name == "manage_review" and parameters.get("action") == "edit_timeline_entry":
        return "review_timeline_entry_edit"
    if name == "record_review":
        return "review_record"
    if name == "preferences" and parameters.get("action") == "apply":
        return "preference_update"
    return None


def _tool_result_committed(
    name: str,
    result: object,
    *,
    expected_client_turn_id: str | None = None,
) -> bool:
    if getattr(result, "status", None) != "success":
        return False
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return False
    if name == "update_application":
        return (
            _valid_application_update_batch(data, expected_client_turn_id)
            and data["changed_count"] > 0
        )
    if name == "manage_review":
        return (
            data.get("operation_type") == "review_timeline_entry_edit"
            and data.get("state") == "completed"
        )
    if name == "preferences":
        result_data = data.get("result")
        return (
            data.get("operation_type") == "preference_update"
            and isinstance(result_data, dict)
            and isinstance(result_data.get("changed_count"), int)
            and result_data["changed_count"] > 0
        )
    return name == "request_application_prep" and data.get("status") not in {
        "error", "reused", "completed",
    }


def _default_agent_factory(settings, user_id: str, client_turn_id: str,
                           review_supplement_reference: str | None,
                           trusted_review_source: str,
                           resource_closers: list, request_llm: object,
                           proposal_recorder, output_guardrails,
                           output_locale: OutputLocale):
    """Build the production Agent and register request-owned session/retrieval resources."""

    def factory(hooks: list) -> object:
        from ...agentic.agents import build_career_assistant

        agent = build_career_assistant(
            settings.db_path,
            request_llm,
            user_id,
            client_turn_id=client_turn_id,
            review_supplement_reference=review_supplement_reference,
            trusted_review_source=trusted_review_source,
            hooks=hooks,
            proposal_recorder=proposal_recorder,
            conversation_embedding_enabled=getattr(
                settings,
                "conversation_embedding_enabled",
                False,
            ),
            trace_path=getattr(
                settings,
                "trace_path",
                str(Path(settings.db_path).parent / "traces.jsonl"),
            ),
            resource_closers=resource_closers,
            output_locale=output_locale,
        )
        guardrails = getattr(agent, "output_guardrails", None)
        if isinstance(guardrails, list):
            guardrails.extend(output_guardrails)
        return agent

    return factory


def classify_attachment(filename: str | None) -> str | None:
    suffix = Path(filename or "").suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in DOCUMENT_SUFFIXES or suffix in WORKBOOK_SUFFIXES:
        return "document"
    return None


def save_chat_upload(file_obj, filename: str | None, user_id: str) -> Path:
    """Save within limits to the current user's managed directory without partial files."""
    data_dir = Path(get_settings().db_path).parent
    root = user_upload_root(data_dir, "chat", user_id)
    cleanup_stale_files(root, CHAT_UPLOAD_TTL_SECONDS)
    try:
        return save_upload(
            file_obj,
            filename,
            root,
            MAX_CHAT_OR_RESUME_BYTES,
            max_total_bytes=MAX_CHAT_STORAGE_BYTES,
        )
    except PlatformUploadTooLarge as error:
        raise ChatUploadTooLarge(str(error)) from error


def extract_chat_document(destination: Path, filename: str | None) -> dict:
    """Extract a managed document and always delete the temporary source."""
    try:
        if destination.suffix.lower() in WORKBOOK_SUFFIXES:
            try:
                text = parse_standard_workbook(destination).structured_text
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001 -- vendor parsers expose varied failures
                logger.error("spreadsheet parser failed (%s)", type(error).__name__)
                raise ValueError(
                    "表格解析失败：请确认文件未损坏、未加密，并重新保存后重试",
                ) from error
        else:
            text = extract_document_text(str(destination))
    finally:
        destination.unlink(missing_ok=True)
    return {
        "status": "ok",
        "kind": "document",
        "filename": filename,
        "text": text[:CHAT_ATTACHMENT_CHAR_LIMIT],
        "truncated": len(text) > CHAT_ATTACHMENT_CHAR_LIMIT,
    }


def delete_chat_upload(stored: str, user_id: str) -> None:
    """Idempotently delete the current user's unsent temporary images."""
    if len(stored) > 255 or Path(stored).name != stored:
        raise ValueError("非法附件标识")
    root = user_upload_root(Path(get_settings().db_path).parent, "chat", user_id).resolve()
    path = root / stored
    if path.parent != root or path.is_symlink():
        raise ValueError("非法附件标识")
    path.unlink(missing_ok=True)


def maintain_turn_ledger(db_path: str) -> dict[str, int]:
    """Fence interrupted owners at startup, then remove expired completed response copies."""
    recovered = ledger.recover_interrupted_turns(db_path)
    evicted = ledger.evict_expired_completed_replays(db_path)
    return {"recovered": recovered, "evicted": evicted}


def get_chat_turn_status(user_id: str, client_turn_id: str) -> ChatTurnStatusResponse:
    """Return one durable turn state as the read-only seam between HTTP and the ledger."""
    snapshot = ledger.read_turn_status(get_settings().db_path, user_id, client_turn_id)
    return ChatTurnStatusResponse(
        client_turn_id=client_turn_id,
        state=snapshot.state,
        terminal=snapshot.state in {"completed", "unknown", "cancelled"},
        proposal_operations=list(snapshot.proposal_operations),
    )


def cancel_chat_turn_if_absent(user_id: str, client_turn_id: str) -> ChatTurnStatusResponse:
    """Fence an unclaimed turn as cancelled or return its existing durable state."""
    state = ledger.cancel_turn_if_absent(get_settings().db_path, user_id, client_turn_id)
    if state != "cancelled":
        # Preserve proposal references if another owner won the fence.
        snapshot = ledger.read_turn_status(
            get_settings().db_path,
            user_id,
            client_turn_id,
        )
        state = snapshot.state
        proposal_operations = list(snapshot.proposal_operations)
    else:
        proposal_operations = []
    return ChatTurnStatusResponse(
        client_turn_id=client_turn_id,
        state=state,
        terminal=state in {"completed", "unknown", "cancelled"},
        proposal_operations=proposal_operations,
    )


def cancel_chat_turn(user_id: str, client_turn_id: str) -> ChatTurnStatusResponse:
    """Request cancellation for an active turn or seal an unseen turn."""
    settings = get_settings()

    def request_registered_turn() -> ChatTurnStatusResponse | None:
        result = request_active_turn_cancel(
            settings.db_path,
            user_id,
            client_turn_id,
        )
        if result == "accepted":
            return get_chat_turn_status(user_id, client_turn_id)
        if result == "finalizing":
            raise ChatTurnRejected(
                _terminal_error(
                    client_turn_id,
                    "turn_finalizing",
                    "本轮已进入提交阶段，将继续返回最终结果。",
                    retryable=True,
                ),
                retry_after=1,
            )
        return None

    accepted = request_registered_turn()
    if accepted is not None:
        return accepted

    snapshot = get_chat_turn_status(user_id, client_turn_id)
    if snapshot.state == "absent":
        snapshot = cancel_chat_turn_if_absent(user_id, client_turn_id)
    if snapshot.state != "running":
        return snapshot

    accepted = request_registered_turn()
    if accepted is not None:
        return accepted

    snapshot = get_chat_turn_status(user_id, client_turn_id)
    if snapshot.state == "running":
        raise ChatTurnRejected(
            _terminal_error(
                client_turn_id,
                "turn_in_progress",
                "本轮仍由后台安全收口，暂时无法取消；请稍后重试。",
                retryable=True,
            ),
            retry_after=1,
        )
    return snapshot


def get_chat_recovery_scope(user_id: str) -> ChatRecoveryScopeResponse:
    """Derive a stable frontend recovery scope without exposing the raw user ID."""
    if not user_id:
        raise ValueError("user_id must be non-empty")
    return ChatRecoveryScopeResponse(scope=derive_recovery_scope(
        get_settings().db_path,
        user_id,
        domain=_RECOVERY_SCOPE_DOMAIN,
    ))


def _terminal_error(client_turn_id: str, code: str, message: str, *, retryable: bool,
                    history_committed: bool | None = None) -> dict:
    data = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "attachments": "retained",
        "client_turn_id": client_turn_id,
    }
    if history_committed is not None:
        data["history_committed"] = history_committed
    return data


def _turn_rejection(decision, client_turn_id: str) -> ChatTurnRejected:
    if decision.status == "running":
        data = _terminal_error(
            client_turn_id,
            "turn_in_progress",
            "这一轮仍在处理中，请稍等片刻后用同一请求重试。",
            retryable=True,
        )
        return ChatTurnRejected(data, retry_after=2)
    if decision.status == "session_busy":
        data = _terminal_error(
            client_turn_id,
            "session_busy",
            "当前会话已有一轮正在处理，请等待它结束后再发送。",
            retryable=True,
            history_committed=False,
        )
        return ChatTurnRejected(data, retry_after=2)
    if decision.status == "conflict":
        return ChatTurnRejected(_terminal_error(
            client_turn_id,
            "idempotency_key_reused",
            "同一请求编号不能用于不同的消息、会话、附件或复盘补充目标；请刷新后重新发送。",
            retryable=False,
            history_committed=False,
        ))
    if decision.status == "cancelled":
        return ChatTurnRejected(_terminal_error(
            client_turn_id,
            "turn_cancelled",
            "这一轮已被安全取消；请使用新的请求编号重新发送。",
            retryable=False,
            history_committed=False,
        ))
    data = decision.error or _terminal_error(
        client_turn_id,
        "turn_outcome_unknown",
        "这一轮没有得到完整完成确认，部分操作可能已经执行。"
        "请先检查时间线、题库或偏好，再决定是否重新发送。",
        retryable=False,
    )
    retained_data = {
        key: value
        for key, value in data.items()
        if key != "history_committed" or data.get("code") == "assistant_setup_failed"
    }
    return ChatTurnRejected({
        **retained_data,
        "attachments": "retained",
        "client_turn_id": client_turn_id,
    })


def _existing_prepared(decision, settings, user_id: str, session_id: str,
                       client_turn_id: str,
                       review_supplement_reference: str | None) -> PreparedChat | None:
    """Convert non-execute inspect/claim decisions to replay or pre-header rejection."""
    if decision.status in {"absent", "execute"}:
        return None
    if decision.status == "completed":
        return PreparedChat(
            "replay",
            settings,
            user_id,
            session_id,
            client_turn_id,
            review_supplement_reference=review_supplement_reference,
            replay_events=tuple(decision.replay_events or ()),
        )
    raise _turn_rejection(decision, client_turn_id)


def _valid_image_signature(path: Path) -> bool:
    """Reject disguised images by signature before a provider call can become unknown."""
    try:
        with path.open("rb") as source:
            head = source.read(16)
    except OSError:
        return False
    suffix = path.suffix.lower()
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def _prepare_images(settings, user_id: str, images: list[dict], client_turn_id: str):
    if not images:
        return (), None
    from ...platform.ai.client import supports_image_input

    supported, reason = supports_image_input(
        settings.llm_model,
        strict_offline=getattr(settings, "strict_offline", False),
    )
    if not supported:
        return (), _terminal_error(
            client_turn_id,
            "image_unsupported",
            reason,
            retryable=True,
            history_committed=False,
        )
    root = user_upload_root(Path(settings.db_path).parent, "chat", user_id).resolve()
    paths: list[Path] = []
    for item in images:
        stored = item.get("stored") or ""
        if not stored or Path(stored).name != stored:
            return (), _terminal_error(
                client_turn_id,
                "attachment_invalid",
                "图片附件标识无效，请移除后重新上传。",
                retryable=False,
                history_committed=False,
            )
        path = (root / stored).resolve()
        if path.parent != root or not path.is_file() or not _valid_image_signature(path):
            return (), _terminal_error(
                client_turn_id,
                "attachment_invalid",
                f"图片附件「{item.get('filename') or '未知图片'}」已失效或格式不正确，请重新上传。",
                retryable=False,
                history_committed=False,
            )
        paths.append(path)
    return tuple(paths), None


def _direct_standard_intake(
    message: str,
    items: list[dict],
    review_supplement_reference: str | None,
    output_locale: OutputLocale,
) -> dict[str, Any] | None:
    """Recognize one explicit standard workbook import without the main Agent or an LLM."""
    import_request = (
        bool(re.search(r"导入|入库", message))
        if output_locale == "zh-CN"
        else bool(re.search(
            r"\bimport\b|\b(?:add|load)\b.{0,40}"
            r"\b(?:roles?|jobs?|applications?|workbooks?|spreadsheets?|files?)\b",
            message,
            re.IGNORECASE,
        ))
    )
    if review_supplement_reference is not None or not import_request:
        return None
    documents = [item for item in items if item.get("kind") == "document"]
    if len(documents) != 1 or len(items) != 1:
        return None
    document = documents[0]
    text = document.get("text")
    if not isinstance(text, str) or document.get("truncated") is True:
        return None
    payload = standard_positions_from_structured_text(text)
    if payload is None:
        return None
    return {
        "positions": list(payload.positions),
        "source_rows": payload.source_rows,
        "skipped_rows": payload.skipped_rows,
        "filename": document.get("filename") or _l(
            output_locale,
            "标准岗位表格",
            "CareerDesk workbook",
        ),
        "history_message": (
            f"{message}\n📎 "
            f"{document.get('filename') or _l(output_locale, '标准岗位表格', 'CareerDesk workbook')}"
        ),
    }


_ZH_REVIEW_RECORD_COMMAND = re.compile(
    # The record word is both a verb and a noun, so an imperative marker is required.
    # Without one, a noun phrase such as "practice records" routes a question into
    # the progress writer.
    r"(?:帮我|请|麻烦)(?:记录|记下|录入)"
    r"|(?:记录|记下|录入)一下"
    r"|(?:帮我|请|麻烦)?(?:更新|整理)(?:一下)?(?:最近|这些|这次|本轮)?(?:的)?"
    r"(?:求职|秋招|招聘)(?:进展|动态|情况|记录)",
)
_ZH_REVIEW_EVENT = re.compile(
    r"投(?:了|递(?:了)?|申请(?:了)?)"
    r"|(?:面试|笔试|测评)(?:了|完|过)"
    r"|(?:参加|完成)(?:了)?.{0,40}(?:一面|二面|三面|终面|面试|笔试|测评)"
    r"|(?:一面|二面|三面|终面|技术面|HR\s*面|面试|笔试|测评)(?:已)?"
    r"(?:通过|过了|完成|参加|结束|安排|定在|待定)"
    r"|(?:收到|拿到|获得)(?:了)?.{0,24}(?:通知|邀请|offer)"
    r"|(?:通过|过了).{0,12}(?:筛选|初筛|面试|测评|笔试)"
    r"|(?:面试|笔试|测评).{0,16}(?:明天|明晚|后天|下周|安排|定在|时间)"
    r"|(?:被拒|拒信|挂了|淘汰|进人才池|进入人才池|HC\s*冻结|泡池子)",
    re.IGNORECASE,
)
_ZH_REVIEW_QUERY = re.compile(
    r"[?？]|请问|想问|我该|应该|怎么|如何|为什么|能否|可不可以|该不该|要不要|"
    r"建议|分析|查询|查看|列出|哪些|多少|统计|总结|复盘|"
    # Yes/no questions carry a particle rather than a question word, and users
    # routinely omit the question mark.
    r"吗|呢|吧|是不是|有没有|是否|什么|多久|哪个|哪家",
)
# An intention or hypothetical is not a fact that happened yet.
_ZH_REVIEW_INTENT = re.compile(r"打算|准备|计划|考虑|如果|万一|假如")
_EN_REVIEW_RECORD_COMMAND = re.compile(
    r"\b(?:record|log|track|save|update|organize)\b.{0,48}"
    r"\b(?:job search|applications?|interviews?|assessments?|recruiting)\b",
    re.IGNORECASE,
)
_EN_REVIEW_EVENT = re.compile(
    r"\b(?:applied|interviewed|passed|failed|rejected|withdrew)\b"
    r"|\b(?:received|got)\b.{0,32}\b(?:interview|assessment|offer|invitation)\b"
    r"|\b(?:interview|assessment|test)\b.{0,24}\b(?:scheduled|tomorrow|next week)\b",
    re.IGNORECASE,
)
_EN_REVIEW_QUERY = re.compile(
    r"[?]|\b(?:how|why|whether|should|could|would|which|what|advice|analyze|"
    r"summarize|list|show|when|where|who)\b"
    # A leading auxiliary opens a yes/no question; matching it anywhere would
    # instead reject ordinary records such as "the interview is on Tuesday".
    r"|^\s*(?:did|do|does|have|has|had|was|were|is|are|am|can|will)\b",
    re.IGNORECASE,
)
# An intention or hypothetical is not a fact that happened yet.
_EN_REVIEW_INTENT = re.compile(
    r"\b(?:plan(?:ning)? to|going to|thinking about|intend to|hoping to|if|in case)\b",
    re.IGNORECASE,
)


def _is_direct_review_record_request(
    message: str,
    items: list[dict],
    review_supplement_reference: str | None,
    output_locale: OutputLocale,
) -> bool:
    """Recognize a standalone progress write without delegating routing to the model."""
    if review_supplement_reference is not None or items or not message.strip():
        return False
    # Guards are deliberately broad: a missed bypass still reaches the agent, which can
    # call the tool itself, while a false positive feeds a question to the progress writer.
    if output_locale == "en":
        if _EN_REVIEW_QUERY.search(message) or _EN_REVIEW_INTENT.search(message):
            return False
        return bool(
            _EN_REVIEW_EVENT.search(message)
            or _EN_REVIEW_RECORD_COMMAND.search(message)
        )
    if _ZH_REVIEW_QUERY.search(message) or _ZH_REVIEW_INTENT.search(message):
        return False
    return bool(
        _ZH_REVIEW_EVENT.search(message)
        or _ZH_REVIEW_RECORD_COMMAND.search(message)
    )


def _preflight_failure(settings, user_id, session, turn_id, request_hash,
                       review_supplement_reference, error_factory) -> PreparedChat:
    """Resolve pre-claim failures after rechecking durable state.

    Replay a concurrently completed turn; otherwise lazily build the local error only when it is
    actually returned.
    """
    latest = ledger.inspect_turn(settings.db_path, user_id, turn_id, session, request_hash)
    resolved = _existing_prepared(
        latest, settings, user_id, session, turn_id, review_supplement_reference,
    )
    if resolved is not None:
        return resolved
    return PreparedChat(
        "error", settings, user_id, session, turn_id,
        review_supplement_reference=review_supplement_reference,
        error=error_factory(),
    )


def prepare_chat(message: str, session_id: str, client_turn_id: str, user_id: str,
                 *, attachments: list[dict] | None = None,
                 review_supplement_reference: str | None = None,
                 output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
                 agent_factory=None) -> PreparedChat:
    """Check replay/conflicts, run side-effect-free preflight, then claim a new turn atomically."""
    settings = get_settings()
    session = str(session_id)
    turn_id = str(client_turn_id)
    items = attachments or []
    request_hash = ledger.chat_request_hash(
        session,
        message,
        items,
        review_supplement_reference=review_supplement_reference,
        output_locale=output_locale,
    )

    existing = ledger.inspect_turn(
        settings.db_path, user_id, turn_id, session, request_hash,
    )
    resolved = _existing_prepared(
        existing,
        settings,
        user_id,
        session,
        turn_id,
        review_supplement_reference,
    )
    if resolved is not None:
        return resolved

    direct_standard_intake = (
        _direct_standard_intake(
            message,
            items,
            review_supplement_reference,
            output_locale,
        )
        if agent_factory is None
        else None
    )
    direct_review_record = (
        _is_direct_review_record_request(
            message,
            items,
            review_supplement_reference,
            output_locale,
        )
        if agent_factory is None
        else False
    )

    # Preflight failures before Agent entry create no ledger row, so the user can fix input or
    # configuration and reuse the original turn ID.
    if agent_factory is None and direct_standard_intake is None and settings.llm_model is None:
        return _preflight_failure(
            settings, user_id, session, turn_id, request_hash, review_supplement_reference,
            lambda: _terminal_error(
                turn_id,
                "model_not_configured",
                "还没有配置模型，本轮未处理、未写入任何数据；请先完成「模型与隐私」设置。",
                retryable=True,
                history_committed=False,
            ),
        )

    if (agent_factory is None and direct_standard_intake is None
            and getattr(settings, "strict_offline", False)):
        from ...platform.ai.client import (
            STRICT_OFFLINE_MODEL_MESSAGE,
            model_uses_local_provider,
        )

        if not model_uses_local_provider(settings.llm_model):
            # End policy rejection before claim just like missing-model rejection. A security
            # policy must not pollute the durable ledger or become unknown through delayed setup.
            return _preflight_failure(
                settings, user_id, session, turn_id, request_hash, review_supplement_reference,
                lambda: _terminal_error(
                    turn_id,
                    "strict_offline",
                    STRICT_OFFLINE_MODEL_MESSAGE,
                    retryable=True,
                    history_committed=False,
                ),
            )

    request_llm = None
    if agent_factory is None and direct_standard_intake is None:
        from ...platform.ai.client import OutboundAccessDisabled, build_llm

        try:
            # LLMClient construction validates provider/key/adapter and performs no network call.
            # Complete it before durable claim so retryable configuration errors cannot become
            # outcome unknown. The request's default Agent then reuses this instance.
            request_llm = build_llm(
                settings.llm_model,
                strict_offline=getattr(settings, "strict_offline", False),
                context_window=getattr(settings, "llm_context_window", None),
                max_output_tokens=getattr(settings, "llm_max_output_tokens", None),
            )
        except Exception as exception:  # noqa: BLE001 -- all construction drift fails pre-claim
            # A concurrent request may claim or complete the same turn during preflight. Durable
            # replay/running/unknown semantics outrank this request's configuration observation.
            # Save the exception in a normal local because Python deletes the except binding.
            captured = exception

            def _build_llm_error():
                if isinstance(captured, OutboundAccessDisabled):
                    code = "strict_offline"
                    message = str(captured)
                elif (
                    isinstance(captured, LLMConfigError)
                    and str(captured) == MODEL_CAPABILITY_MESSAGE
                ):
                    code = "model_capabilities_missing"
                    message = (
                        f"{MODEL_CAPABILITY_MESSAGE} 本轮未处理、未写入任何数据；"
                        "补齐后可直接重试。"
                    )
                    logger.warning("assistant model capacity preflight failed")
                else:
                    # Reuse the protocol code so the UI retains its Settings recovery link; the
                    # message distinguishes missing configuration from initialization failure.
                    code = "model_not_configured"
                    message = (
                        "当前模型配置无法初始化，本轮未处理、未写入任何数据；"
                        "请检查「模型与隐私」中的供应商、模型名称和 API Key 后重试。"
                    )
                    logger.warning("assistant model preflight failed", exc_info=captured)
                return _terminal_error(
                    turn_id, code, message, retryable=True, history_committed=False,
                )

            return _preflight_failure(
                settings, user_id, session, turn_id, request_hash, review_supplement_reference,
                _build_llm_error,
            )

    images = [item for item in items if item.get("kind") == "image"]
    image_paths, image_error = _prepare_images(settings, user_id, images, turn_id)
    if image_error is not None:
        # A concurrent same-turn request may finish and delete images during preflight. Recheck
        # durable state before replaying.
        return _preflight_failure(
            settings, user_id, session, turn_id, request_hash, review_supplement_reference,
            lambda: image_error,
        )

    prepared_message = message
    for item in items:
        if item.get("kind") == "document" and item.get("text"):
            note = (
                _l(output_locale, "（内容过长已截断）", " (truncated)")
                if item.get("truncated")
                else ""
            )
            prepared_message += (
                f"\n\n---\n[{_l(output_locale, '附件：', 'Attachment: ')}"
                f"{item.get('filename') or _l(output_locale, '文档', 'document')}{note}]\n"
                f"{item['text']}"
            )

    decision = ledger.claim_turn(
        settings.db_path, user_id, turn_id, session, request_hash,
    )
    resolved = _existing_prepared(
        decision, settings, user_id, session, turn_id, review_supplement_reference,
    )
    if resolved is not None:
        return resolved
    if decision.status != "execute":
        raise RuntimeError(f"unexpected assistant turn decision: {decision.status}")

    try:
        message_payload: Any = prepared_message
        if image_paths:
            from agentmaker import image_part_from_file, text_part

            message_payload = [
                *(image_part_from_file(path) for path in image_paths),
                text_part(prepared_message),
            ]
    except Exception:
        error = _terminal_error(
            turn_id,
            "turn_outcome_unknown",
            "本轮在启动时中断，未得到完整完成确认；请检查现有记录后再决定是否重新发送。",
            retryable=False,
        )
        ledger.mark_turn_unknown(
            settings.db_path, user_id, turn_id, decision.attempt_token, error,
        )
        raise ChatTurnRejected(error) from None

    prepared = PreparedChat(
        "execute",
        settings,
        user_id,
        session,
        turn_id,
        output_locale=output_locale,
        review_supplement_reference=review_supplement_reference,
        message_payload=message_payload,
        image_paths=image_paths,
        attempt_token=decision.attempt_token,
        agent_factory=agent_factory,
        request_llm=request_llm,
        direct_standard_intake=direct_standard_intake,
        direct_review_record=direct_review_record,
    )
    register_active_turn(
        settings.db_path,
        user_id,
        turn_id,
        prepared.cancellation,
    )
    return prepared


def _mark_unknown(prepared: PreparedChat, error: dict) -> None:
    if prepared.attempt_token is None:
        return
    try:
        ledger.mark_turn_unknown(
            prepared.settings.db_path,
            prepared.user_id,
            prepared.client_turn_id,
            prepared.attempt_token,
            error,
        )
    except Exception:  # noqa: BLE001 -- original failure/cancellation must still propagate safely
        logger.exception("failed to persist unknown assistant turn")


def abandon_prepared_chat(prepared: PreparedChat, *, trace_id: str | None = None) -> dict:
    """Fail closed for a claimed turn when the HTTP encoding layer fails unexpectedly."""
    if (prepared.mode == "execute"
            and prepared.execution_started
            and not prepared.execution_stopped):
        # Never release the session fence only because the client disconnected: an uncooperative
        # provider or tool may still write in the background. Keep running until execution exits;
        # startup recovery handles process termination.
        error = _terminal_error(
            prepared.client_turn_id,
            "turn_in_progress",
            "连接已中断，但后台仍在安全收口。请稍后用同一请求重试，不要新建一轮。",
            retryable=True,
        )
        if trace_id is not None:
            error["trace_id"] = trace_id
        return error
    error = _terminal_error(
        prepared.client_turn_id,
        "turn_outcome_unknown",
        "这一轮没有得到完整完成确认，部分操作可能已经执行。"
        "请先检查时间线、题库或偏好，再决定是否重新发送。",
        retryable=False,
    )
    if trace_id is not None:
        error["trace_id"] = trace_id
    _mark_unknown(prepared, error)
    if prepared.mode == "execute":
        unregister_active_turn(
            prepared.settings.db_path,
            prepared.user_id,
            prepared.client_turn_id,
            prepared.cancellation,
        )
    return error


def _remove_consumed_images(paths: tuple[Path, ...]) -> None:
    """Treat post-completion disk cleanup as best effort without downgrading committed results."""
    for path in set(paths):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to remove consumed assistant image")


async def _cancel_prepared_turn(prepared: PreparedChat, agent_stream) -> ChatStreamEvent:
    cancellation_uncertain = False
    try:
        if not prepared.execution_stopped:
            await agent_stream.aclose()
    except Exception:  # noqa: BLE001 -- incomplete shutdown cannot be reported as cancelled
        logger.exception("failed to stop cancelled assistant execution")
        cancellation_uncertain = True
    try:
        reject_cancelled_turn_proposals(
            prepared.settings.db_path,
            prepared.user_id,
            prepared.client_turn_id,
        )
    except TurnCancellationNotReversible:
        prepared.cancellation.mark_committed()
    except Exception:  # noqa: BLE001 -- unverified cleanup must retain recovery surfaces
        logger.exception("failed to reject cancelled assistant proposals")
        cancellation_uncertain = True
    if cancellation_uncertain or prepared.cancellation.committed_effects:
        error = _terminal_error(
            prepared.client_turn_id,
            "turn_outcome_unknown",
            "本轮已停止继续处理，但取消前已有操作完成，请核对页面中的可信收据。",
            retryable=False,
        )
        _mark_unknown(prepared, error)
        return ChatStreamEvent(event="error", data=error)
    if prepared.attempt_token is None or not ledger.cancel_running_turn(
        prepared.settings.db_path,
        prepared.user_id,
        prepared.client_turn_id,
        prepared.attempt_token,
    ):
        raise RuntimeError("assistant turn cancellation lost ownership")
    return ChatStreamEvent(
        event="error",
        data=_terminal_error(
            prepared.client_turn_id,
            "turn_cancelled",
            "本轮已取消，未确认方案已丢弃。",
            retryable=False,
            history_committed=False,
        ),
    )


async def stream_prepared_chat(prepared: PreparedChat) -> AsyncIterator[ChatStreamEvent]:
    """Execute or replay a prepared turn; never emit done before durable completion."""
    if prepared.mode == "error":
        yield ChatStreamEvent(event="error", data=prepared.error or {})
        return
    if prepared.mode == "replay":
        for item in prepared.replay_events:
            yield ChatStreamEvent(event=item["event"], data=item["data"])
        return

    text_parts: list[str] = []
    ui_actions: list[dict[str, object]] = []
    ui_action_identities: set[tuple[object, object]] = set()
    direct_review_supplement = (
        prepared.review_supplement_reference is not None
        and prepared.agent_factory is None
        and isinstance(prepared.message_payload, str)
    )
    if prepared.direct_standard_intake is not None:
        agent_stream = _run_direct_standard_intake(prepared)
    elif direct_review_supplement or prepared.direct_review_record:
        agent_stream = _run_direct_review_record(prepared)
    else:
        agent_stream = _run_agent_stream(prepared)
    try:
        async for event in agent_stream:
            if event.event == "message_delta":
                text_parts.append(event.data["text"])
            elif event.event == "message_snapshot":
                text_parts = [event.data["text"]]
            elif event.event == "ui_action":
                identity = (event.data.get("kind"), event.data.get("resource_id"))
                if identity not in ui_action_identities and len(ui_actions) < 8:
                    ui_action_identities.add(identity)
                    ui_actions.append(event.data)
                continue
            yield event

        prepared.cancellation.begin_commit()
        response_text = "".join(text_parts)
        message_id = str(uuid.uuid4())
        replay_events = [
            {"event": "message_snapshot", "data": {"text": response_text}},
            {
                "event": "done",
                "data": {
                    "session": prepared.session_id,
                    "message_id": message_id,
                    "client_turn_id": prepared.client_turn_id,
                    "history_committed": True,
                    "attachments": "consumed",
                    "replayed": True,
                    "ui_actions": ui_actions,
                },
            },
        ]
        proposal_operations = ledger.complete_turn(
            prepared.settings.db_path,
            prepared.user_id,
            prepared.client_turn_id,
            prepared.attempt_token,
            replay_events,
        )
        _remove_consumed_images(prepared.image_paths)
        yield ChatStreamEvent(
            event="done",
            data={
                "session": prepared.session_id,
                "message_id": message_id,
                "client_turn_id": prepared.client_turn_id,
                "history_committed": True,
                "attachments": "consumed",
                "replayed": False,
                "proposal_operations": proposal_operations,
                "ui_actions": ui_actions,
            },
        )
    except asyncio.CancelledError:
        if prepared.cancellation.requested:
            yield await _cancel_prepared_turn(prepared, agent_stream)
            return
        error = _terminal_error(
            prepared.client_turn_id,
            "turn_outcome_unknown",
            "这一轮在完成确认前被中断，部分操作可能已经执行。请先检查现有记录。",
            retryable=False,
        )
        try:
            # The outer generator may be suspended at yield, so async-for will not close the
            # inner stream for us. Release an unknown session fence only after Agent/resources exit.
            if not prepared.execution_stopped:
                await agent_stream.aclose()
        finally:
            if prepared.execution_stopped:
                _mark_unknown(prepared, error)
        raise
    except GeneratorExit:
        error = _terminal_error(
            prepared.client_turn_id,
            "turn_outcome_unknown",
            "这一轮在完成确认前被中断，部分操作可能已经执行。请先检查现有记录。",
            retryable=False,
        )
        try:
            if not prepared.execution_stopped:
                await agent_stream.aclose()
        finally:
            if prepared.execution_stopped:
                _mark_unknown(prepared, error)
        raise
    except TurnCancellationRequested:
        yield await _cancel_prepared_turn(prepared, agent_stream)
    except Exception as exception:  # noqa: BLE001 -- all post-claim failures become durable unknown
        from agentmaker import RunLimitExceeded

        trace_id = uuid.uuid4().hex
        if not prepared.execution_started:
            code = "assistant_setup_failed"
            message = (
                "助手运行组件初始化失败；本轮尚未调用模型或工具，也未修改业务记录。"
                "请完全退出并重新打开 CareerDesk；如仍失败，请使用最新完整安装包后重试。"
            )
            retryable = True
            history_committed = False
            logger.exception("assistant setup failed", extra={"trace_id": trace_id})
        elif isinstance(exception, RunLimitExceeded):
            code = "turn_outcome_unknown"
            message = (
                "这一轮超过安全上限，且部分操作可能已经执行。"
                "请先检查时间线、题库或偏好，再决定是否重新发送。"
            )
            retryable = False
            history_committed = None
        else:
            code = "turn_outcome_unknown"
            message = (
                "这一轮没有得到完整完成确认，部分操作可能已经执行。"
                "请先检查时间线、题库或偏好，再决定是否重新发送。"
            )
            retryable = False
            history_committed = None
            logger.exception("assistant turn failed", extra={"trace_id": trace_id})
        error = {
            **_terminal_error(
                prepared.client_turn_id,
                code,
                message,
                retryable=retryable,
                history_committed=history_committed,
            ),
            "trace_id": trace_id,
        }
        _mark_unknown(prepared, error)
        yield ChatStreamEvent(event="error", data=error)
    finally:
        unregister_active_turn(
            prepared.settings.db_path,
            prepared.user_id,
            prepared.client_turn_id,
            prepared.cancellation,
        )


async def run_chat(message: str, session_id: str, user_id: str, *, client_turn_id: str,
                   attachments: list[dict] | None = None,
                   review_supplement_reference: str | None = None,
                   output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
                   agent_factory=None) -> AsyncIterator[ChatStreamEvent]:
    """Combined entry for tests/non-HTTP callers; HTTP uses prepare plus stream for early 409s."""
    try:
        prepared = prepare_chat(
            message,
            session_id,
            client_turn_id,
            user_id,
            attachments=attachments,
            review_supplement_reference=review_supplement_reference,
            output_locale=output_locale,
            agent_factory=agent_factory,
        )
    except ChatTurnRejected as error:
        yield ChatStreamEvent(event="error", data=error.data)
        return
    async for event in stream_prepared_chat(prepared):
        yield event


async def _close_resources(resource_closers: list) -> None:
    """Close resources best-effort in reverse order without turning done into a second error."""
    for close in reversed(resource_closers):
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 -- continue closing the remaining request resources
            logger.exception("failed to close assistant request resource")


async def _finish_before_reraising_cancellation(awaitable):
    """Keep request-owned writes alive until their worker has exited."""
    task = asyncio.create_task(awaitable)
    interruption: asyncio.CancelledError | GeneratorExit | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, GeneratorExit) as error:
            interruption = error
    result = task.result()
    if interruption is not None:
        raise interruption
    return result


async def _run_direct_review_record(
    prepared: PreparedChat,
) -> AsyncIterator[ChatStreamEvent]:
    """Execute a trusted review write without model-selected tool routing."""
    from agentmaker import Message, Scope

    from ...agentic.memory import build_conversation_memory
    from ...agentic.tools import RecordReviewTool
    from ...features.reviews.public import ReviewService

    resource_closers: list = []
    if prepared.request_llm is not None:
        resource_closers.append(lambda: close_llm_client(prepared.request_llm))
    store, _conversation = build_conversation_memory(
        prepared.settings.db_path,
        embedding_enabled=getattr(
            prepared.settings,
            "conversation_embedding_enabled",
            False,
        ),
        user_id=prepared.user_id,
        resource_closers=resource_closers,
    )
    try:
        prepared.execution_started = True
        prepared.cancellation.checkpoint()
        yield ChatStreamEvent(
            event="tool_status",
            data={
                "tool": "record_review",
                "label": _tool_status_label(prepared.output_locale, "record_review"),
                "trusted_operation_type": "review_record",
            },
        )
        service = ReviewService(
            prepared.settings.db_path,
            prepared.request_llm,
            output_locale=prepared.output_locale,
        )
        batch_record = (
            prepared.direct_review_record
            and prepared.review_supplement_reference is None
        )
        tool = RecordReviewTool(
            service,
            prepared.user_id,
            client_turn_id=prepared.client_turn_id,
            review_supplement_reference=prepared.review_supplement_reference,
            trusted_source_text=(
                prepared.message_payload if batch_record else None
            ),
            allow_batch=batch_record,
            output_locale=prepared.output_locale,
        )
        prepared.cancellation.begin_tool()
        try:
            response = await tool.arun(
                {} if batch_record else {"text": prepared.message_payload},
            )
        finally:
            prepared.cancellation.finish_tool()
        reply = response.text or (
            "The review update was processed. Use the structured receipt shown on the page."
            if prepared.output_locale == "en"
            else "复盘补充已处理，请以页面中的结构化收据为准。"
        )
        scope = Scope(
            user=prepared.user_id,
            app="careerdesk",
            session=prepared.session_id,
        )
        prepared.cancellation.begin_commit()
        store.append_many(
            [
                Message(content=prepared.message_payload, role="user"),
                Message(content=reply, role="assistant"),
            ],
            scope=scope,
        )
        yield ChatStreamEvent(event="message_delta", data={"text": reply})
    finally:
        await _close_resources(resource_closers)
        prepared.execution_stopped = True


async def _run_direct_standard_intake(
    prepared: PreparedChat,
) -> AsyncIterator[ChatStreamEvent]:
    """Generate standard-workbook proposals locally without constructing or calling a model."""
    from agentmaker import Message, Scope

    from ...agentic.memory import build_conversation_memory
    from ...features.applications.public import ApplicationService

    payload = prepared.direct_standard_intake
    if payload is None or prepared.attempt_token is None:  # pragma: no cover - caller invariant
        raise RuntimeError("direct standard intake payload is missing")
    resource_closers: list = []
    store, _conversation = build_conversation_memory(
        prepared.settings.db_path,
        embedding_enabled=False,
        user_id=prepared.user_id,
        resource_closers=resource_closers,
    )
    try:
        prepared.execution_started = True
        prepared.cancellation.checkpoint()
        yield ChatStreamEvent(
            event="tool_status",
            data={
                "tool": "parse_jobs",
                "label": _l(
                    prepared.output_locale,
                    "读取表格…",
                    "Reading workbook…",
                ),
            },
        )

        def record_proposal(conn, surface: str, operation_id: str) -> None:
            ledger.record_proposal_operation_in_transaction(
                conn,
                prepared.user_id,
                prepared.client_turn_id,
                prepared.attempt_token,
                surface,
                operation_id,
            )

        prepared.cancellation.begin_tool()
        try:
            result = ApplicationService(
                prepared.settings.db_path,
                None,
                proposal_recorder=record_proposal,
            ).parse_standard_positions(
                prepared.user_id,
                payload["positions"],
                source_label=_l(
                    prepared.output_locale,
                    f"求职助手标准表格附件：{payload['filename']}",
                    f"Career Agent workbook attachment: {payload['filename']}",
                ),
                source_rows=payload["source_rows"],
                skipped_rows=payload["skipped_rows"],
            )
        finally:
            prepared.cancellation.finish_tool()
        if result["status"] == "preview":
            operation_id = result["operation_id"]
            yield ChatStreamEvent(
                event="tool_status",
                data={
                    "tool": "proposal_ready",
                    "label": _l(
                        prepared.output_locale,
                        "导入预览已准备，等待你确认…",
                        "The import preview is ready for your confirmation…",
                    ),
                    "proposal_surface": "intake",
                    "proposal_operation_id": operation_id,
                },
            )
            reply = _l(
                prepared.output_locale,
                f"已用本地代码读取标准表格，共生成 {len(result['positions'])} 条岗位预览。"
                "请在下方确认卡中核对并保存；当前尚未写入求职进展。",
                f"The workbook was read locally and produced {len(result['positions'])} role previews. "
                "Review and save them in the confirmation card below; no application progress has been written yet.",
            )
        elif result["status"] == "empty":
            reply = _l(
                prepared.output_locale,
                "标准表格中没有可安全导入的岗位；缺少公司或岗位名称的行不会写入。",
                "The workbook contains no roles that can be imported safely. Rows without a company or role name will not be written.",
            )
        else:
            reply = _l(
                prepared.output_locale,
                "这次预览已被更新的导入取代，请重新上传一次。",
                "A newer import replaced this preview. Upload the workbook again.",
            )
        scope = Scope(
            user=prepared.user_id,
            app="careerdesk",
            session=prepared.session_id,
        )
        prepared.cancellation.begin_commit()
        store.append_many(
            [
                Message(content=payload["history_message"], role="user"),
                Message(content=reply, role="assistant"),
            ],
            scope=scope,
        )
        yield ChatStreamEvent(event="message_delta", data={"text": reply})
    finally:
        await _close_resources(resource_closers)
        prepared.execution_stopped = True


async def _run_agent_stream(prepared: PreparedChat) -> AsyncIterator[ChatStreamEvent]:
    """Merge Agent deltas/tool status; after natural exhaustion, close resources in finally."""
    from agentmaker import Scope

    queue: asyncio.Queue = asyncio.Queue()
    resource_closers: list = []
    if prepared.request_llm is not None:
        # The request owns the model client until the Agent (including any
        # to-thread tools) has fully stopped.  Register it first so reverse-order
        # cleanup releases session/retrieval resources before the HTTP pool.
        resource_closers.append(lambda: close_llm_client(prepared.request_llm))
    task: asyncio.Task | None = None
    queue_task: asyncio.Task | None = None
    accepting_output = True
    try:
        if prepared.attempt_token is None:
            raise RuntimeError("executing assistant turn is missing its owner token")

        def record_proposal_in_transaction(
            conn,
            surface: str,
            operation_id: str,
        ) -> None:
            ledger.record_proposal_operation_in_transaction(
                conn,
                prepared.user_id,
                prepared.client_turn_id,
                prepared.attempt_token,
                surface,
                operation_id,
            )

        request_text = content_text(prepared.message_payload)
        required_stage = _requested_stage_correction(request_text)
        write_attestation = _VerifiedWriteAttestation(
            request_text,
            _known_application_identities(
                prepared.settings.db_path,
                prepared.user_id,
            ) if required_stage is not None else (),
            output_locale=prepared.output_locale,
            expected_client_turn_id=prepared.client_turn_id,
        )

        factory = prepared.agent_factory or _default_agent_factory(
            prepared.settings,
            prepared.user_id,
            prepared.client_turn_id,
            prepared.review_supplement_reference,
            request_text,
            resource_closers,
            prepared.request_llm,
            record_proposal_in_transaction,
            [write_attestation, _OutputLocaleGuardrail(prepared.output_locale)],
            prepared.output_locale,
        )

        def record_proposal(surface: str, operation_id: str) -> None:
            ledger.record_proposal_operation(
                prepared.settings.db_path,
                prepared.user_id,
                prepared.client_turn_id,
                prepared.attempt_token,
                surface,
                operation_id,
            )

        agent = factory([_ToolStatusHook(
            queue,
            record_proposal=record_proposal,
            write_attestation=write_attestation,
            cancellation=prepared.cancellation,
            expected_client_turn_id=prepared.client_turn_id,
        )])
        history_store = getattr(agent, "session_store", None)
        deferred_history = (
            _DeferredSessionStore(history_store)
            if history_store is not None
            else None
        )
        if deferred_history is not None:
            agent.session_store = deferred_history
        scope = Scope(
            user=prepared.user_id,
            app="careerdesk",
            session=prepared.session_id,
        )

        async def pump() -> bool:
            produced = False
            published = False
            agent_finished = False
            output_gate = _StreamingOutputGate(write_attestation, prepared.output_locale)
            try:
                stream_parameters = inspect.signature(agent.astream_run).parameters.values()
                supports_buffered_output = any(
                    parameter.name == "buffer_output"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in stream_parameters
                )
                async for piece in agent.astream_run(
                    prepared.message_payload,
                    scope=scope,
                    **({"buffer_output": False} if supports_buffered_output else {}),
                ):
                    produced = True
                    for safe_piece in output_gate.feed(piece):
                        if accepting_output:
                            queue.put_nowait(("delta", safe_piece))
                            published = True
                agent_finished = True
                for safe_piece in output_gate.finish():
                    if accepting_output:
                        queue.put_nowait(("delta", safe_piece))
                        published = True
            except GuardrailTripwireError as error:
                safe_reply = str(error).strip() or write_attestation.failure_message
                add_messages = getattr(agent, "add_messages", None)
                if (
                    not agent_finished
                    and prepared.agent_factory is None
                    and callable(add_messages)
                ):
                    await add_messages(
                        [
                            Message(content=prepared.message_payload, role="user"),
                            Message(content=safe_reply, role="assistant"),
                        ],
                        scope=scope,
                    )
                if accepting_output:
                    queue.put_nowait(("snapshot", safe_reply))
                    published = True
            return published or produced

        task = asyncio.create_task(pump())
        prepared.cancellation.attach_task(task)
        prepared.execution_started = True
        while not task.done() or not queue.empty():
            prepared.cancellation.checkpoint()
            if queue.empty() and not task.done():
                queue_task = asyncio.create_task(queue.get())
                finished, _ = await asyncio.wait(
                    {queue_task, task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_task not in finished:
                    queue_task.cancel()
                    await asyncio.gather(queue_task, return_exceptions=True)
                    queue_task = None
                    continue
                item = queue_task.result()
                queue_task = None
            elif not queue.empty():
                item = queue.get_nowait()
            else:
                break
            if isinstance(item, tuple) and item[0] == "delta":
                yield ChatStreamEvent(event="message_delta", data={"text": item[1]})
            elif isinstance(item, tuple) and item[0] == "snapshot":
                yield ChatStreamEvent(event="message_snapshot", data={"text": item[1]})
            else:
                kind = item[0]
                if kind == "ui_action":
                    yield ChatStreamEvent(event="ui_action", data=item[1])
                    continue
                if kind == "proposal":
                    _, name, trusted_operation_type = item
                    yield ChatStreamEvent(
                        event="tool_status",
                        data={
                            "tool": "proposal_ready",
                            "label": (
                                "方案已准备，等待你确认…"
                                if prepared.output_locale == "zh-CN"
                                else "The proposal is ready for your confirmation…"
                            ),
                            "proposal_surface": name,
                            "proposal_operation_id": trusted_operation_type,
                        },
                    )
                    continue
                _, name, trusted_operation_type, status_variant = item
                label = _tool_status_label(prepared.output_locale, status_variant)
                if label is None:
                    continue
                data = {
                    "tool": name,
                    "label": label,
                }
                if trusted_operation_type is not None:
                    data["trusted_operation_type"] = trusted_operation_type
                yield ChatStreamEvent(
                    event="tool_status",
                    data=data,
                )
        produced = await task
        if not produced:
            yield ChatStreamEvent(
                event="message_delta",
                data={
                    "text": _l(
                        prepared.output_locale,
                        "（这一轮没有产出文本）",
                        "(This turn produced no text.)",
                    )
                },
            )
        prepared.cancellation.begin_commit()
        if deferred_history is not None:
            await _finish_before_reraising_cancellation(deferred_history.commit())
    finally:
        # Once the client stops consuming, let the Agent finish naturally without queuing tokens
        # nobody will read.
        accepting_output = False

        async def finish_execution() -> None:
            if queue_task is not None:
                if not queue_task.done():
                    queue_task.cancel()
                await asyncio.gather(queue_task, return_exceptions=True)
            if task is not None:
                # Transport disconnect cannot cancel the Agent: default async tool adaptation uses
                # asyncio.to_thread, and cancelling the await does not stop its thread. Releasing
                # the session fence early could let the old thread write beside a new turn.
                await asyncio.gather(task, return_exceptions=True)
                prepared.cancellation.detach_task(task)
            await _close_resources(resource_closers)
            prepared.execution_stopped = True

        # A dedicated cleanup task also handles repeated cancellation. shield protects only the
        # child, so keep awaiting after outer cancellations until cleanup truly finishes. Startup
        # recovery converts orphaned running turns to unknown after a killed process.
        cleanup_task = asyncio.create_task(finish_execution())
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
        cleanup_task.result()
