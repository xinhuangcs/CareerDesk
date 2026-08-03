"""Ordinary application corrections with immediate execution and conditional undo."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import date
from hashlib import sha256
from sqlite3 import Connection
from uuid import UUID

from pydantic import ValidationError

from ....core.config import local_today
from ....platform.database import (
    DatabaseBusy,
    application_identity_key,
    normalize_application_identity_part,
    now_iso,
    read_connection,
    transaction,
)
from ...journal import public as journal
from .. import repository
from ..intake_models import MAX_COMPANY_CHARS
from .update_models import (
    APPLICATION_UPDATE_CONTRACT_VERSION,
    MAX_UPDATE_OCCURRENCES,
    MAX_UPDATE_QUESTIONS,
    ApplicationUpdateApplyResult,
    ApplicationUpdateCommand,
    ApplicationUpdateEffect,
    ApplicationUpdateFieldChange,
    ApplicationUpdateOccurrenceProvenance,
    ApplicationUpdateOperationDTO,
    ApplicationUpdateProjection,
    ApplicationUpdateProposal,
    ApplicationUpdateQuestionProvenance,
    ApplicationUpdateReceipt,
    ApplicationUpdateTarget,
    ApplicationUpdateUndoCommandError,
    ApplicationUpdateUndoCommandStatus,
    ApplicationUpdateUndoResult,
)

MAX_PERSISTED_JSON_CHARS = 500_000
MAX_SELECTOR_OPTIONS = 100
MAX_OPERATIONS_PER_TURN = 20
MAX_APPLICATION_UPDATE_BATCH_ITEMS = MAX_OPERATIONS_PER_TURN
MAX_TURN_OPERATION_CANDIDATES = 128
_OPERATION_COLUMNS = (
    "id, operation_id, state, created_time, extraction_json, derivation_json, "
    "revision, kind"
)
_TURN_OPERATION_FAMILIES = frozenset({
    "application_update",
    "preference_update",
    "review_timeline_entry_edit",
    "review_record",
})
_UNDO_COMMAND_COLUMNS = (
    "command_id, operation_id, state, error_code, error_message, finished_time"
)
_UNDO_REJECTION_MESSAGES = {
    "operation_not_found": "岗位修改操作不存在",
    "operation_invalid": "岗位修改回执已损坏，不能撤销",
    "target_missing": "岗位已不存在，不能撤销旧修改",
    "target_changed": "岗位在此后又被修改，不能自动覆盖",
    "prep_changed": "岗位在此后已生成或运行新的准备材料，不能自动清空",
    "provenance_changed": "岗位题目出处在此后已变化，不能自动覆盖",
    "natural_key_taken": "原公司与岗位名已被另一条记录占用",
}


class ApplicationUpdateOperationNotFound(LookupError):
    """Operation is absent or belongs to another user."""


class ApplicationUpdateOperationConflict(RuntimeError):
    """Identity reuse, corrupt contract, or no-longer-safe conditional undo."""


class _UnsafeUpdateDependency(ValueError):
    """Target or provenance is oversized, corrupt, or cross-tenant."""


class _ApplicationUpdateBatchRollback(RuntimeError):
    """Carry a canonical rejected batch result through transaction rollback."""

    def __init__(self, result: dict):
        super().__init__("application update batch rejected")
        self.result = result


def _canonical_uuid(value: str | UUID, *, operation: bool) -> str:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        if operation:
            raise ApplicationUpdateOperationNotFound("岗位修改操作不存在") from error
        raise ValueError("client_turn_id 必须是 UUID") from error


def _canonical_command_uuid(value: str | UUID) -> str:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("command_id 必须是 UUID") from error


def _canonical_json(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    if len(encoded) > MAX_PERSISTED_JSON_CHARS:
        raise _UnsafeUpdateDependency("application update 持久契约超过安全预算")
    return encoded


def _request_digest(
    client_turn_id: str,
    command: ApplicationUpdateCommand,
    occurred_date: str,
) -> str:
    encoded = _canonical_json({
        "client_turn_id": client_turn_id,
        # Nullable state fields distinguish omission (preserve) from explicit null (clear).
        "command": command.model_dump(exclude_unset=True),
        "occurred_date": occurred_date,
        "version": APPLICATION_UPDATE_CONTRACT_VERSION,
    })
    return sha256(encoded.encode("utf-8")).hexdigest()


def _proposal_digest(proposal: ApplicationUpdateProposal) -> str:
    return sha256(_canonical_json(proposal.model_dump()).encode("utf-8")).hexdigest()


def _command_hash(action: str, operation_id: str, proposal_digest: str) -> str:
    encoded = _canonical_json({
        "action": action,
        "operation_id": operation_id,
        "proposal_digest": proposal_digest,
        "version": 1,
    })
    return sha256(encoded.encode("utf-8")).hexdigest()


def _safe_timestamp(value) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    cleaned = value.strip()
    # created/updated timestamps participate in exact CAS and provenance identity.
    # Silently normalizing a damaged database token would let apply succeed and make
    # the freshly returned operation immediately non-undoable.
    return cleaned if cleaned == value else None


def _safe_company(value, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= MAX_COMPANY_CHARS
    )


def _operation_payload(raw: str | None) -> dict:
    if not isinstance(raw, str) or len(raw) > MAX_PERSISTED_JSON_CHARS:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(loaded, dict) or set(loaded) != {"operation"}:
        return {}
    operation = loaded.get("operation")
    return operation if isinstance(operation, dict) else {}


def _persisted_uuid(value, *, label: str) -> str:
    if not isinstance(value, str):
        raise ApplicationUpdateOperationConflict(f"岗位修改 {label} 身份已损坏")
    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ApplicationUpdateOperationConflict(
            f"岗位修改 {label} 身份已损坏",
        ) from error
    if value != canonical:
        raise ApplicationUpdateOperationConflict(f"岗位修改 {label} 身份已损坏")
    return canonical


def _json_object(raw: str | None) -> dict | None:
    if not isinstance(raw, str) or len(raw) > MAX_PERSISTED_JSON_CHARS:
        return None
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _classify_turn_candidate(
    *,
    operation_id,
    kind,
    extraction_json,
    derivation_json,
    client_turn_id: str,
) -> str:
    """Classify a broadly located operation row without trusting one receipt side."""
    _persisted_uuid(operation_id, label="operation_id")
    if kind != "correction":
        raise ApplicationUpdateOperationConflict("同轮 operation kind 身份已损坏")

    extraction = _json_object(extraction_json)
    derivation = _json_object(derivation_json)
    if (
        extraction is None
        or derivation is None
        or set(derivation) != {"operation"}
        or not isinstance(derivation.get("operation"), dict)
    ):
        raise ApplicationUpdateOperationConflict("同轮 operation receipt 身份已损坏")
    operation = derivation["operation"]

    extraction_family = extraction.get("operation_type")
    derivation_family_keys = {"type", "operation_type"} & set(operation)
    if len(derivation_family_keys) != 1:
        raise ApplicationUpdateOperationConflict("同轮 operation family 身份已损坏")
    derivation_family_key = next(iter(derivation_family_keys))
    derivation_family = operation.get(derivation_family_key)
    if (
        not isinstance(extraction_family, str)
        or not isinstance(derivation_family, str)
        or extraction_family != derivation_family
        or extraction_family not in _TURN_OPERATION_FAMILIES
        or (
            extraction_family == "preference_update"
            and derivation_family_key != "operation_type"
        )
        or (
            extraction_family != "preference_update"
            and derivation_family_key != "type"
        )
    ):
        raise ApplicationUpdateOperationConflict("同轮 operation family 身份已损坏")

    extraction_turn = _persisted_uuid(
        extraction.get("client_turn_id"),
        label="client_turn_id",
    )
    derivation_turn = _persisted_uuid(
        operation.get("client_turn_id"),
        label="client_turn_id",
    )
    if extraction_turn != derivation_turn or extraction_turn != client_turn_id:
        raise ApplicationUpdateOperationConflict("同轮 operation turn 身份已损坏")

    # Own-family rows receive complete contract validation in _dto. Other known
    # families are ignored only after both family/turn copies and the column UUID agree.
    return extraction_family


def _operations_for_turn(
    conn: Connection,
    user_id: str,
    client_turn_id: str,
) -> list[dict]:
    candidates = journal.read_operation_candidates_for_turn_in_transaction(
        conn,
        user_id,
        client_turn_id,
        maximum=MAX_TURN_OPERATION_CANDIDATES,
    )
    if len(candidates) > MAX_TURN_OPERATION_CANDIDATES:
        raise ApplicationUpdateOperationConflict("同轮 operation 候选超过安全上限")
    operations = []
    for candidate in candidates:
        family = _classify_turn_candidate(
            operation_id=candidate.operation_id,
            kind=candidate.kind,
            extraction_json=candidate.extraction_json,
            derivation_json=candidate.derivation_json,
            client_turn_id=client_turn_id,
        )
        if family != "application_update":
            continue
        row = _operation_row(conn, user_id, candidate.operation_id)
        if row is None or row[0] != candidate.journal_id:
            raise ApplicationUpdateOperationConflict(
                "同轮岗位修改 operation 身份已损坏",
            )
        operation = _dto(conn, user_id, row)
        if operation["client_turn_id"] != client_turn_id:
            raise ApplicationUpdateOperationConflict(
                "该轮岗位修改回执身份已损坏",
            )
        operations.append(operation)
    return operations


def _proposal(raw: str | None) -> ApplicationUpdateProposal:
    if not isinstance(raw, str) or len(raw) > MAX_PERSISTED_JSON_CHARS:
        raise _UnsafeUpdateDependency("application update proposal 超界或缺失")
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise _UnsafeUpdateDependency("application update proposal 不是有效 JSON") from error
    proposal = ApplicationUpdateProposal.model_validate(loaded)
    if proposal.model_dump() != loaded:
        raise _UnsafeUpdateDependency(
            "application update proposal 不是 canonical contract",
        )
    return proposal


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
        "AND operation_id = ?",
        (user_id, operation_id),
    ).fetchone()


def _raw_application(conn: Connection, user_id: str, application_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, user_id, company, company_id, position, stage, current_step, "
        "current_state_entry_id, next_stage, next_step, next_date, next_time, next_note, "
        "paused_from_stage, pause_reason, application_note, priority, jd_text, applied_date, "
        "prep_status, "
        "prep_generation, prep_heartbeat_time, prep_json, created_time, updated_time, "
        "revision FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "id", "user_id", "company", "company_id", "position", "stage", "current_step",
        "current_state_entry_id", "next_stage", "next_step", "next_date", "next_time",
        "next_note", "paused_from_stage", "pause_reason", "application_note", "priority", "jd_text",
        "applied_date",
        "prep_status", "prep_generation", "prep_heartbeat_time", "prep_json",
        "created_time", "updated_time", "revision",
    )
    return dict(zip(keys, tuple(row), strict=True))


def _projection(application: dict, *, updated_time: str | None = None,
                revision: int | None = None, company: str | None = None,
                company_id: int | None | object = ...) -> ApplicationUpdateProjection:
    resolved_company_id = application["company_id"] if company_id is ... else company_id
    return ApplicationUpdateProjection(
        company=company if company is not None else application["company"],
        company_id=resolved_company_id,
        position=application["position"],
        stage=application["stage"],
        current_step=application["current_step"],
        priority=application["priority"],
        applied_date=application["applied_date"],
        next_action=(
            {
                "stage": application["next_stage"],
                "step": application["next_step"],
                "date": application["next_date"],
                "time": application["next_time"],
                "note": application["next_note"],
            }
            if application["next_step"] is not None
            else None
        ),
        paused_from_stage=application["paused_from_stage"],
        pause_reason=application["pause_reason"],
        application_note=application["application_note"],
        jd_text=application["jd_text"],
        revision=(application["revision"] if revision is None else revision),
        application_updated_time=(updated_time if updated_time is not None
                                  else application["updated_time"]),
    )


def _validate_company_row(conn: Connection, user_id: str, company_id: int | None,
                          company: str) -> bool:
    if company_id is None:
        return True
    row = conn.execute(
        "SELECT user_id, name_key FROM companies WHERE id = ?", (company_id,),
    ).fetchone()
    return row == (user_id, normalize_application_identity_part(company))


def _locate_target(conn: Connection, user_id: str, command: ApplicationUpdateCommand):
    if command.position is not None:
        identity_key = application_identity_key(command.company, command.position)
        row = conn.execute(
            "SELECT id FROM applications WHERE user_id = ? "
            "AND company_key = ? AND position_key = ?",
            (user_id, *identity_key),
        ).fetchone()
        return row[0] if row is not None else {"status": "not_found"}
    company_key = normalize_application_identity_part(command.company)
    rows = conn.execute(
        "SELECT id, position FROM applications WHERE user_id = ? "
        "AND company_key = ? "
        "ORDER BY position, id LIMIT ?",
        (user_id, company_key, MAX_SELECTOR_OPTIONS + 1),
    ).fetchall()
    if len(rows) > MAX_SELECTOR_OPTIONS:
        raise ApplicationUpdateOperationConflict("同公司岗位过多，请提供准确岗位名")
    if len(rows) == 1:
        return rows[0][0]
    if rows:
        return {"status": "ambiguous", "options": [row[1] for row in rows]}
    return {"status": "not_found"}


def _current_provenance(conn: Connection, user_id: str, application_id: int) -> tuple[list[dict], list[dict]]:
    for table in ("questions", "review_question_occurrences"):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE application_id = ? AND user_id != ? LIMIT 1",
            (application_id, user_id),
        ).fetchone() is not None:
            raise _UnsafeUpdateDependency(f"{table} 存在跨租户岗位引用")

    question_rows = conn.execute(
        "SELECT id, application_id, company, created_time, updated_time FROM questions "
        "WHERE user_id = ? AND application_id = ? ORDER BY id LIMIT ?",
        (user_id, application_id, MAX_UPDATE_QUESTIONS + 1),
    ).fetchall()
    if len(question_rows) > MAX_UPDATE_QUESTIONS:
        raise _UnsafeUpdateDependency("application questions 超过安全行数上限")
    questions = []
    for row in question_rows:
        if (
            row[1] != application_id
            or not _safe_company(row[2], nullable=True)
            or _safe_timestamp(row[3]) is None
            or _safe_timestamp(row[4]) is None
        ):
            raise _UnsafeUpdateDependency("application question provenance 损坏")
        questions.append({
            "id": row[0],
            "application_id": row[1],
            "company": row[2],
            "created_time": row[3],
            "updated_time": row[4],
        })

    occurrence_rows = conn.execute(
        "SELECT occurrence.journal_id, occurrence.question_id, occurrence.application_id, "
        "occurrence.company, occurrence.source_step, occurrence.asked_date, "
        "source.created_time, source.state, source.revision, source.user_id, "
        "question.created_time, question.user_id "
        "FROM review_question_occurrences occurrence "
        "LEFT JOIN journal source ON source.id = occurrence.journal_id "
        "LEFT JOIN questions question ON question.id = occurrence.question_id "
        "WHERE occurrence.user_id = ? AND occurrence.application_id = ? "
        "ORDER BY occurrence.journal_id, occurrence.question_id LIMIT ?",
        (user_id, application_id, MAX_UPDATE_OCCURRENCES + 1),
    ).fetchall()
    if len(occurrence_rows) > MAX_UPDATE_OCCURRENCES:
        raise _UnsafeUpdateDependency("application occurrences 超过安全行数上限")
    occurrences = []
    for row in occurrence_rows:
        if (
            row[2] != application_id
            or row[9] != user_id
            or row[11] != user_id
            or not _safe_company(row[3])
            or row[7] != "applied"
            or not isinstance(row[8], int)
            or _safe_timestamp(row[6]) is None
            or _safe_timestamp(row[10]) is None
        ):
            raise _UnsafeUpdateDependency("application occurrence provenance 缺少同租户来源")
        occurrences.append({
            "journal_id": row[0],
            "question_id": row[1],
            "application_id": row[2],
            "company": row[3],
            "source_step": row[4],
            "asked_date": row[5],
            "journal_created_time": row[6],
            "journal_state": row[7],
            "journal_revision": row[8],
            "question_created_time": row[10],
        })
    return questions, occurrences


def _prep_is_cleared(application: dict) -> bool:
    if not (
        application["prep_status"] == "none"
        and application["prep_generation"] is None
        and application["prep_heartbeat_time"] is None
    ):
        return False
    raw = application["prep_json"]
    if raw is None:
        return True
    try:
        prep = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(prep, dict):
        return False
        # Successful snapshots and raw Match/Adaptation envelopes may remain historical;
        # current loose artifacts/attempts must be gone and hashes derive eligibility.
    current_semantic_keys = {
        "anchor",
        "error",
        "planner",
        "position_report",
        "prepared_time",
        "research",
        "research_attempt",
        "web_questions",
    }
    return not current_semantic_keys.intersection(prep)


def _effect_provenance(questions: list[dict], occurrences: list[dict], *,
                       after_company: str, timestamp: str):
    return (
        [ApplicationUpdateQuestionProvenance(
            id=item["id"],
            question_created_time=item["created_time"],
            before_updated_time=item["updated_time"],
            after_updated_time=timestamp,
            before_company=item["company"],
            after_company=after_company,
        ) for item in questions],
        [ApplicationUpdateOccurrenceProvenance(
            journal_id=item["journal_id"],
            question_id=item["question_id"],
            application_id=item["application_id"],
            journal_created_time=item["journal_created_time"],
            journal_state=item["journal_state"],
            journal_revision=item["journal_revision"],
            question_created_time=item["question_created_time"],
            before_company=item["company"],
            after_company=after_company,
            source_step=item["source_step"],
            asked_date=item["asked_date"],
        ) for item in occurrences],
    )


def _provenance_matches(conn: Connection, user_id: str, proposal: ApplicationUpdateProposal,
                        *, after: bool) -> bool:
    try:
        questions, occurrences = _current_provenance(
            conn, user_id, proposal.target.application_id,
        )
    except _UnsafeUpdateDependency:
        return False
    expected_questions = []
    for item in proposal.effect.question_provenance:
        expected_questions.append({
            "id": item.id,
            "application_id": proposal.target.application_id,
            "company": item.after_company if after else item.before_company,
            "created_time": item.question_created_time,
            "updated_time": item.after_updated_time if after else item.before_updated_time,
        })
    expected_occurrences = []
    for item in proposal.effect.question_occurrences:
        expected_occurrences.append({
            "journal_id": item.journal_id,
            "question_id": item.question_id,
            "application_id": item.application_id,
            "company": item.after_company if after else item.before_company,
            "source_step": item.source_step,
            "asked_date": item.asked_date,
            "journal_created_time": item.journal_created_time,
            "journal_state": item.journal_state,
            "journal_revision": item.journal_revision,
            "question_created_time": item.question_created_time,
        })
    return questions == expected_questions and occurrences == expected_occurrences


def _undone_provenance_matches(conn: Connection, user_id: str,
                               proposal: ApplicationUpdateProposal,
                               timestamp: str) -> bool:
    try:
        questions, occurrences = _current_provenance(
            conn, user_id, proposal.target.application_id,
        )
    except _UnsafeUpdateDependency:
        return False
    expected_questions = [
        {
            "id": item.id,
            "application_id": proposal.target.application_id,
            "company": item.before_company,
            "created_time": item.question_created_time,
            "updated_time": timestamp,
        }
        for item in proposal.effect.question_provenance
    ]
    expected_occurrences = [
        {
            "journal_id": item.journal_id,
            "question_id": item.question_id,
            "application_id": item.application_id,
            "company": item.before_company,
            "source_step": item.source_step,
            "asked_date": item.asked_date,
            "journal_created_time": item.journal_created_time,
            "journal_state": item.journal_state,
            "journal_revision": item.journal_revision,
            "question_created_time": item.question_created_time,
        }
        for item in proposal.effect.question_occurrences
    ]
    return questions == expected_questions and occurrences == expected_occurrences


def _result_matches(result: ApplicationUpdateApplyResult,
                    proposal: ApplicationUpdateProposal, *, undo: bool) -> bool:
    return (
        result.application_id == proposal.target.application_id
        and result.revision == proposal.final.revision + (1 if undo else 0)
        and (result.timeline_entry_id is not None) == (
            bool(
                {"stage", "current_step"}
                & {item.field for item in proposal.effect.changed_fields}
            )
        )
        and result.questions_updated == len(proposal.effect.question_provenance)
        and result.question_occurrences_updated == len(proposal.effect.question_occurrences)
        and result.prep_invalidated == proposal.effect.prep_invalidated
    )


def _undo_block_reason(conn: Connection, user_id: str,
                       proposal: ApplicationUpdateProposal) -> str | None:
    application = _raw_application(conn, user_id, proposal.target.application_id)
    if application is None or application["created_time"] != proposal.target.application_created_time:
        return "target_missing"
    changed_fields = {item.field for item in proposal.effect.changed_fields}
    if (
        application["revision"] != proposal.final.revision
        or application["company"] != proposal.final.company
        or application["position"] != proposal.final.position
        or application["stage"] != proposal.final.stage
        or _projection(application) != proposal.final
        or (
            "jd_text" in changed_fields
            and application["jd_text"] != proposal.final.jd_text
        )
    ):
        return "target_changed"
    if proposal.effect.prep_invalidated and not _prep_is_cleared(application):
        return "prep_changed"
    if "company" in changed_fields:
        if application["company_id"] != proposal.final.company_id:
            return "target_changed"
        if not _validate_company_row(
            conn, user_id, proposal.final.company_id, proposal.final.company,
        ) or not _validate_company_row(
            conn, user_id, proposal.before.company_id, proposal.before.company,
        ):
            return "target_changed"
    if {"company", "position"} & changed_fields:
        before_key = application_identity_key(
            proposal.before.company, proposal.before.position,
        )
        occupied = conn.execute(
            "SELECT id FROM applications WHERE user_id = ? AND company_key = ? AND position_key = ? "
            "AND id != ?",
            (user_id, *before_key, proposal.target.application_id),
        ).fetchone()
        if occupied is not None:
            return "natural_key_taken"
    if "company" in changed_fields and not _provenance_matches(
        conn, user_id, proposal, after=True,
    ):
        return "provenance_changed"
    return None


def _dto(conn: Connection, user_id: str, row) -> dict:
    (
        _, operation_id, journal_state, created_time, extraction_json,
        derivation_json, _, kind,
    ) = row
    if kind != "correction":
        raise ApplicationUpdateOperationConflict("岗位修改 operation kind 身份已损坏")
    canonical_operation_id = _persisted_uuid(operation_id, label="operation_id")
    safe_created = _safe_timestamp(created_time)
    if safe_created is None:
        raise ApplicationUpdateOperationConflict("岗位修改操作时间身份已损坏")
    operation = _operation_payload(derivation_json)
    try:
        proposal = _proposal(extraction_json)
        proposal_digest = _proposal_digest(proposal)
    except (TypeError, ValueError, ValidationError) as error:
        raise ApplicationUpdateOperationConflict("岗位修改 proposal 身份已损坏") from error
    if operation.get("type") != "application_update":
        raise ApplicationUpdateOperationConflict("岗位修改 operation family 身份已损坏")
    if operation.get("client_turn_id") != proposal.client_turn_id:
        raise ApplicationUpdateOperationConflict("岗位修改 operation turn 身份已损坏")
    if set(operation) not in (
        {"type", "client_turn_id", "proposal_digest", "apply"},
        {"type", "client_turn_id", "proposal_digest", "apply", "undo"},
    ):
        raise ApplicationUpdateOperationConflict("岗位修改 operation receipt 形状已损坏")
    state = "stale"
    receipt = None
    undo_available = False
    undo_reason = "operation_invalid"
    if (
        proposal is not None
        and proposal_digest is not None
        and operation.get("proposal_digest") == proposal_digest
    ):
        apply_envelope = operation.get("apply")
        try:
            apply_result = ApplicationUpdateApplyResult.model_validate(
                apply_envelope.get("result") if isinstance(apply_envelope, dict) else None,
            )
        except (TypeError, ValueError, ValidationError):
            apply_result = None
        apply_valid = (
            isinstance(apply_envelope, dict)
            and set(apply_envelope) == {"command_hash", "result"}
            and apply_envelope.get("command_hash") == _command_hash(
                "apply", canonical_operation_id, proposal_digest,
            )
            and apply_result is not None
            and _result_matches(apply_result, proposal, undo=False)
        )
        if journal_state == "applied" and apply_valid and "undo" not in operation:
            state = "completed"
            receipt = ApplicationUpdateReceipt(apply=apply_result)
            undo_reason = _undo_block_reason(conn, user_id, proposal)
            undo_available = undo_reason is None
        elif journal_state == "voided" and apply_valid and "undo" in operation:
            undo_envelope = operation.get("undo")
            try:
                undo_result = ApplicationUpdateUndoResult.model_validate(
                    undo_envelope.get("result") if isinstance(undo_envelope, dict) else None,
                )
            except (TypeError, ValueError, ValidationError):
                undo_result = None
            if (
                isinstance(undo_envelope, dict)
                and set(undo_envelope) == {"command_hash", "result"}
                and undo_envelope.get("command_hash") == _command_hash(
                    "undo", canonical_operation_id, proposal_digest,
                )
                and undo_result is not None
                and _result_matches(undo_result, proposal, undo=True)
            ):
                state = "undone"
                receipt = ApplicationUpdateReceipt(
                    apply=apply_result, undo=undo_result,
                )
                undo_reason = "already_undone"
    try:
        return ApplicationUpdateOperationDTO(
            operation_id=canonical_operation_id,
            operation_type="application_update",
            contract_version=APPLICATION_UPDATE_CONTRACT_VERSION,
            state=state,
            created_time=safe_created,
            client_turn_id=proposal.client_turn_id,
            target=proposal.target,
            before=proposal.before,
            final=proposal.final,
            effect=proposal.effect,
            result=receipt,
            undo_available=undo_available,
            undo_block_reason=undo_reason,
        ).model_dump()
    except (TypeError, ValueError, ValidationError) as error:
        raise ApplicationUpdateOperationConflict("岗位修改公共回执已损坏") from error


def _execute_application_update_operation(
    db_path: str,
    user_id: str,
    *,
    operation_id: str | UUID,
    client_turn_id: str | UUID,
    company: str,
    position: str | None = None,
    changes: dict,
    expected_application_id: int | None = None,
    expected_revision: int | None = None,
    occurred_date: str | None = None,
    _connection: Connection | None = None,
) -> dict:
    """Execute one correction in a caller-owned or self-owned write transaction."""
    canonical_operation_id = _canonical_uuid(operation_id, operation=True)
    canonical_turn_id = _canonical_uuid(client_turn_id, operation=False)
    command = ApplicationUpdateCommand.model_validate({
        "company": company,
        "position": position,
        "changes": changes,
        "expected_application_id": expected_application_id,
        "expected_revision": expected_revision,
    })
    resolved_occurred_date = occurred_date or local_today().isoformat()
    try:
        parsed_occurred_date = date.fromisoformat(resolved_occurred_date)
    except (TypeError, ValueError) as error:
        raise ValueError("occurred_date 必须是真实的 YYYY-MM-DD") from error
    if parsed_occurred_date.isoformat() != resolved_occurred_date:
        raise ValueError("occurred_date 必须是真实的 YYYY-MM-DD")
    request_digest = _request_digest(
        canonical_turn_id,
        command,
        resolved_occurred_date,
    )
    context = transaction(db_path) if _connection is None else nullcontext(_connection)
    with context as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        existing = _operation_row(conn, user_id, canonical_operation_id)
        if existing is not None:
            try:
                existing_proposal = _proposal(existing[4])
            except (TypeError, ValueError, ValidationError):
                return _dto(conn, user_id, existing)
            if (
                existing_proposal.request_digest != request_digest
                or existing_proposal.client_turn_id != canonical_turn_id
            ):
                raise ApplicationUpdateOperationConflict(
                    "operation_id 已用于另一条岗位修改命令",
                )
            return _dto(conn, user_id, existing)
        if conn.execute(
            "SELECT 1 FROM journal WHERE operation_id = ? LIMIT 1",
            (canonical_operation_id,),
        ).fetchone() is not None:
            raise ApplicationUpdateOperationNotFound("岗位修改操作不存在")
        operation_count = len(_operations_for_turn(conn, user_id, canonical_turn_id))
        if operation_count >= MAX_OPERATIONS_PER_TURN:
            raise ApplicationUpdateOperationConflict(
                "单轮岗位修改操作已达安全上限",
            )

        located = _locate_target(conn, user_id, command)
        if isinstance(located, dict):
            return located
        if (
            command.expected_application_id is not None
            and located != command.expected_application_id
        ):
            raise ApplicationUpdateOperationConflict(
                "岗位身份在读取下一步后发生变化；本次没有覆盖新状态",
            )
        application = _raw_application(conn, user_id, located)
        if application is None:  # pragma: no cover - same write snapshot invariant
            return {"status": "not_found"}
        if (
            not isinstance(application["revision"], int)
            or _safe_timestamp(application["created_time"]) is None
            or _safe_timestamp(application["updated_time"]) is None
        ):
            raise ApplicationUpdateOperationConflict("岗位记录不符合安全契约")
        if (
            command.expected_revision is not None
            and application["revision"] != command.expected_revision
        ):
            raise ApplicationUpdateOperationConflict(
                "岗位已在读取下一步后更新；本次没有覆盖新状态",
            )
        if not _validate_company_row(
            conn, user_id, application["company_id"], application["company"],
        ):
            raise ApplicationUpdateOperationConflict("岗位公司身份已损坏")

        before = _projection(application)
        requested_fields = command.changes.model_fields_set
        requested = command.changes.model_dump(exclude_unset=True)
        before_values = {
            "company": application["company"],
            "position": application["position"],
            "stage": application["stage"],
            "current_step": application["current_step"],
            "priority": application["priority"],
            "next_action": (
                before.next_action.model_dump() if before.next_action is not None else None
            ),
            "application_note": application["application_note"],
            "jd_text": application["jd_text"],
        }
        final_values = {
            field: requested[field] if field in requested_fields else before_values[field]
            for field in before_values
        }
        if final_values["stage"] in {"rejected", "withdrawn"}:
            if "next_action" in requested_fields and final_values["next_action"] is not None:
                raise ValueError("已挂或不再跟进的岗位不能设置下一步")
            final_values["next_action"] = None
        final_applied_date = application["applied_date"]
        if (
            final_values["stage"] == "applied"
            and application["stage"] != "applied"
            and final_applied_date is None
        ):
            final_applied_date = resolved_occurred_date
        changed_fields = {
            field
            for field in (
                "company", "position", "stage", "current_step", "priority", "next_action",
                "application_note", "jd_text",
            )
            if final_values[field] != before_values[field]
        }
        if not changed_fields:
            return {
                "status": "no_change",
                "application_id": application["id"],
                "company": application["company"],
                "position": application["position"],
            }
        final_identity_key = application_identity_key(
            final_values["company"], final_values["position"],
        )
        collision = conn.execute(
            "SELECT id FROM applications WHERE user_id = ? AND company_key = ? AND position_key = ? "
            "AND id != ?",
            (user_id, *final_identity_key, application["id"]),
        ).fetchone()
        if collision is not None:
            return {
                "status": "merge_required",
                "source_id": application["id"],
                "destination_id": collision[0],
                "source_company": application["company"],
                "source_position": application["position"],
                "source_stage": application["stage"],
                "destination_company": final_values["company"],
                "destination_position": final_values["position"],
            }

        timestamp = now_iso()
        changes_before_operation = conn.total_changes
        final_company_id = application["company_id"]
        company_created = False
        if "company" in changed_fields:
            final_company_key = normalize_application_identity_part(final_values["company"])
            existing_company = conn.execute(
                "SELECT id FROM companies WHERE user_id = ? AND name_key = ?",
                (user_id, final_company_key),
            ).fetchone()
            from ...companies.public import ensure_company_in_transaction

            final_company_id = ensure_company_in_transaction(
                conn, user_id, final_values["company"],
            )
            company_created = existing_company is None

        questions: list[dict] = []
        occurrences: list[dict] = []
        if "company" in changed_fields:
            try:
                questions, occurrences = _current_provenance(
                    conn, user_id, application["id"],
                )
            except _UnsafeUpdateDependency as error:
                raise ApplicationUpdateOperationConflict(str(error)) from error
        question_effect, occurrence_effect = _effect_provenance(
            questions, occurrences, after_company=final_values["company"], timestamp=timestamp,
        )
        field_effect = [ApplicationUpdateFieldChange(
            field=field,
            before=before_values[field],
            after=final_values[field],
        ) for field in (
            "company", "position", "stage", "current_step", "priority", "next_action",
            "application_note", "jd_text",
        ) if field in changed_fields]
        effect = ApplicationUpdateEffect(
            changed_fields=field_effect,
            question_provenance=question_effect,
            question_occurrences=occurrence_effect,
            prep_invalidated=bool({"company", "position", "jd_text"} & changed_fields),
            prep_restored_on_undo=False,
            company_record_created=company_created,
            company_records_retained_on_undo=True,
        )
        final = ApplicationUpdateProjection(
            company=final_values["company"],
            company_id=final_company_id,
            position=final_values["position"],
            stage=final_values["stage"],
            current_step=final_values["current_step"],
            priority=final_values["priority"],
            applied_date=final_applied_date,
            next_action=final_values["next_action"],
            paused_from_stage=(
                (
                    before.stage
                    if final_values["stage"] == "pooled"
                    and before.stage in {
                        "backlog", "applied", "written_test", "interviewing", "offer",
                    }
                    else None
                )
                if "stage" in changed_fields
                else before.paused_from_stage
            ),
            pause_reason=None if "stage" in changed_fields else before.pause_reason,
            application_note=final_values["application_note"],
            jd_text=final_values["jd_text"],
            revision=application["revision"] + 1,
            application_updated_time=timestamp,
        )
        proposal = ApplicationUpdateProposal(
            operation_type="application_update",
            contract_version=APPLICATION_UPDATE_CONTRACT_VERSION,
            client_turn_id=canonical_turn_id,
            request_digest=request_digest,
            occurred_date=resolved_occurred_date,
            target=ApplicationUpdateTarget(
                application_id=application["id"],
                company=application["company"],
                position=application["position"],
                application_created_time=application["created_time"],
            ),
            before=before,
            final=final,
            effect=effect,
        )
        try:
            raw_result = repository._update_application_in_transaction(
                conn,
                user_id,
                application["id"],
                created_time=application["created_time"],
                expected_revision=application["revision"],
                expected=before.model_dump(),
                replacement=final.model_dump(),
                changed_fields=changed_fields,
                question_provenance=[item.model_dump() for item in question_effect],
                occurrence_provenance=[item.model_dump() for item in occurrence_effect],
                invalidate_prep=effect.prep_invalidated,
                occurred_date=resolved_occurred_date,
                timestamp=timestamp,
            )
        except RuntimeError as error:
            raise ApplicationUpdateOperationConflict(
                "岗位修改依赖未精确命中",
            ) from error
        result = ApplicationUpdateApplyResult.model_validate(raw_result)
        # Changing jd_text also fires the schema-level receipt invalidation trigger.
        # sqlite total_changes includes that second, intentional UPDATE.
        application_writes = (
            1
            + int(bool({"stage", "current_step"} & changed_fields))
            + int("jd_text" in changed_fields)
        )
        expected_changes = (
            application_writes + len(question_effect) + len(occurrence_effect)
            + (1 if company_created else 0)
        )
        if conn.total_changes - changes_before_operation != expected_changes:
            raise ApplicationUpdateOperationConflict(
                "岗位修改发生了未展示的额外写入",
            )
        current = _raw_application(conn, user_id, application["id"])
        if current is None or _projection(current) != final:
            raise ApplicationUpdateOperationConflict("岗位修改后投影与冻结结果不一致")
        if effect.prep_invalidated and not _prep_is_cleared(current):
            raise ApplicationUpdateOperationConflict("岗位修改后 Prep 未精确失效")
        if "company" in changed_fields and not _provenance_matches(
            conn, user_id, proposal, after=True,
        ):
            raise ApplicationUpdateOperationConflict("岗位修改后 provenance 与冻结结果不一致")
        if not _result_matches(result, proposal, undo=False):
            raise ApplicationUpdateOperationConflict("岗位修改回执与冻结 effect 不一致")

        proposal_digest = _proposal_digest(proposal)
        derivation = {"operation": {
            "type": "application_update",
            "client_turn_id": canonical_turn_id,
            "proposal_digest": proposal_digest,
            "apply": {
                "command_hash": _command_hash(
                    "apply", canonical_operation_id, proposal_digest,
                ),
                "result": result.model_dump(),
            },
        }}
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, processed_time, "
            "extraction_json, derivation_json, state, operation_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, ?, 'applied', ?)",
            (
                user_id,
                f"[已修改岗位 #{application['id']}]",
                timestamp,
                timestamp,
                _canonical_json(proposal.model_dump()),
                _canonical_json(derivation),
                canonical_operation_id,
            ),
        )
        if conn.total_changes - changes_before_operation != expected_changes + 1:
            raise ApplicationUpdateOperationConflict(
                "岗位修改审计落底发生了额外写入",
            )
        current_after_journal = _raw_application(conn, user_id, application["id"])
        if current_after_journal is None or _projection(current_after_journal) != final:
            raise ApplicationUpdateOperationConflict("审计落底后岗位投影漂移")
        if effect.prep_invalidated and not _prep_is_cleared(current_after_journal):
            raise ApplicationUpdateOperationConflict("审计落底后 Prep 漂移")
        if "company" in changed_fields and not _provenance_matches(
            conn, user_id, proposal, after=True,
        ):
            raise ApplicationUpdateOperationConflict("审计落底后 provenance 漂移")
        terminal = _operation_row(conn, user_id, canonical_operation_id)
        if terminal is None:  # pragma: no cover
            raise RuntimeError("application update operation insert lost")
        completed = _dto(conn, user_id, terminal)
        if completed["state"] != "completed":
            raise ApplicationUpdateOperationConflict("岗位修改回执无法校验")
        return completed


def execute_application_update_operation(
    db_path: str,
    user_id: str,
    *,
    operation_id: str | UUID,
    client_turn_id: str | UUID,
    company: str,
    position: str | None = None,
    changes: dict,
    expected_application_id: int | None = None,
    expected_revision: int | None = None,
    occurred_date: str | None = None,
) -> dict:
    """Execute one low-risk correction with a server-issued operation ID."""
    return _execute_application_update_operation(
        db_path,
        user_id,
        operation_id=operation_id,
        client_turn_id=client_turn_id,
        company=company,
        position=position,
        changes=changes,
        expected_application_id=expected_application_id,
        expected_revision=expected_revision,
        occurred_date=occurred_date,
    )


def _batch_issue(index: int, result: dict) -> dict:
    status = result.get("status")
    issue = {"index": index, "reason": status if isinstance(status, str) else "invalid_result"}
    if status == "ambiguous" and isinstance(result.get("options"), list):
        issue["options"] = result["options"][:MAX_SELECTOR_OPTIONS]
    if status == "merge_required":
        issue["detail"] = {
            key: result[key]
            for key in (
                "source_id",
                "source_company",
                "source_position",
                "source_stage",
                "destination_id",
                "destination_company",
                "destination_position",
            )
            if key in result
        }
    return issue


def execute_application_update_batch(
    db_path: str,
    user_id: str,
    *,
    operation_ids: list[str | UUID],
    client_turn_id: str | UUID,
    commands: list[dict],
    occurred_date: str | None = None,
) -> dict:
    """Execute 1-20 independent corrections atomically in one write transaction."""
    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_OPERATIONS_PER_TURN:
        raise ValueError(f"岗位批量修改必须包含 1–{MAX_OPERATIONS_PER_TURN} 项")
    if not isinstance(operation_ids, list) or len(operation_ids) != len(commands):
        raise ValueError("岗位批量修改 operation 数量不匹配")
    canonical_operation_ids = [
        _canonical_uuid(operation_id, operation=True) for operation_id in operation_ids
    ]
    if len(set(canonical_operation_ids)) != len(canonical_operation_ids):
        raise ValueError("岗位批量修改 operation_id 不能重复")
    canonical_turn_id = _canonical_uuid(client_turn_id, operation=False)
    validated_commands = [ApplicationUpdateCommand.model_validate(command) for command in commands]
    if len(validated_commands) > 1 and any(
        {"company", "position"} & command.changes.model_fields_set
        for command in validated_commands
    ):
        raise ValueError("多个岗位不能在同一批中改公司或岗位名；身份修改需单独处理")
    selector_keys = [
        (
            normalize_application_identity_part(command.company),
            normalize_application_identity_part(command.position)
            if command.position is not None
            else None,
        )
        for command in validated_commands
    ]
    if len(set(selector_keys)) != len(selector_keys):
        raise ValueError("岗位批量修改不能包含重复目标")

    for attempt in range(2):
        try:
            with transaction(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                results: list[dict] = []
                issues: list[dict] = []
                resolved_application_ids: set[int] = set()
                for index, (operation_id, command) in enumerate(zip(
                    canonical_operation_ids,
                    validated_commands,
                    strict=True,
                )):
                    try:
                        result = _execute_application_update_operation(
                            db_path,
                            user_id,
                            operation_id=operation_id,
                            client_turn_id=canonical_turn_id,
                            company=command.company,
                            position=command.position,
                            changes=command.changes.model_dump(exclude_unset=True),
                            expected_application_id=command.expected_application_id,
                            expected_revision=command.expected_revision,
                            occurred_date=occurred_date,
                            _connection=conn,
                        )
                    except (
                        ApplicationUpdateOperationConflict,
                        ApplicationUpdateOperationNotFound,
                        ValueError,
                    ):
                        issues.append({
                            "index": index,
                            "reason": "conflict",
                        })
                        break
                    if (
                        result.get("operation_type") == "application_update"
                        and result.get("state") == "completed"
                    ):
                        application_id = result["target"]["application_id"]
                        if application_id in resolved_application_ids:
                            issues.append({"index": index, "reason": "duplicate_target"})
                            break
                        resolved_application_ids.add(application_id)
                        results.append({
                            "index": index,
                            "status": "completed",
                            "operation": result,
                        })
                        continue
                    if result.get("status") == "no_change":
                        application_id = result["application_id"]
                        if application_id in resolved_application_ids:
                            issues.append({"index": index, "reason": "duplicate_target"})
                            break
                        resolved_application_ids.add(application_id)
                        results.append({
                            "index": index,
                            "status": "no_change",
                            "application_id": application_id,
                            "company": result["company"],
                            "position": result["position"],
                        })
                        continue
                    issues.append(_batch_issue(index, result))
                if issues:
                    raise _ApplicationUpdateBatchRollback({
                        "operation_type": "application_update_batch",
                        "state": "rejected",
                        "requested_count": len(validated_commands),
                        "issues": issues,
                    })
                completed_count = sum(item["status"] == "completed" for item in results)
                return {
                    "operation_type": "application_update_batch",
                    "state": "completed" if completed_count else "no_change",
                    "requested_count": len(validated_commands),
                    "changed_count": completed_count,
                    "no_change_count": len(results) - completed_count,
                    "results": results,
                }
        except _ApplicationUpdateBatchRollback as error:
            return error.result
        except DatabaseBusy:
            if attempt == 1:
                raise
    raise RuntimeError("岗位批量修改重试状态异常")  # pragma: no cover


def get_application_update_operation(db_path: str, user_id: str,
                                     operation_id: str | UUID) -> dict | None:
    canonical = _canonical_uuid(operation_id, operation=True)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _operation_row(conn, user_id, canonical)
        return _dto(conn, user_id, row) if row is not None else None


def list_application_update_operations_for_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str | UUID,
) -> list[dict]:
    canonical_turn = _canonical_uuid(client_turn_id, operation=False)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        operations = _operations_for_turn(conn, user_id, canonical_turn)
        if len(operations) > MAX_OPERATIONS_PER_TURN:
            raise ApplicationUpdateOperationConflict("单轮岗位修改操作超过安全上限")
        return operations


def _undo_command_row(conn: Connection, user_id: str, command_id: str):
    return conn.execute(
        f"SELECT {_UNDO_COMMAND_COLUMNS} "
        "FROM application_update_undo_commands "
        "WHERE user_id = ? AND command_id = ?",
        (user_id, command_id),
    ).fetchone()


def _undo_command_status(command_id: str, row) -> dict:
    if row is None:
        return ApplicationUpdateUndoCommandStatus(
            command_id=command_id,
            state="absent",
            terminal=False,
        ).model_dump()
    (
        stored_command_id,
        operation_id,
        state,
        error_code,
        error_message,
        finished_time,
    ) = row
    try:
        error = (
            ApplicationUpdateUndoCommandError(
                code=error_code,
                message=error_message,
            )
            if state == "rejected"
            else None
        )
        return ApplicationUpdateUndoCommandStatus(
            command_id=stored_command_id,
            operation_id=operation_id,
            state=state,
            terminal=True,
            error=error,
            finished_time=finished_time,
        ).model_dump()
    except (TypeError, ValueError, ValidationError) as error:
        raise ApplicationUpdateOperationConflict(
            "岗位修改撤销命令回执已损坏",
        ) from error


def get_application_update_undo_command_status(
    db_path: str,
    user_id: str,
    command_id: str | UUID,
) -> dict:
    """Return tenant-scoped terminal state without leaking cross-tenant UUID use."""
    canonical = _canonical_command_uuid(command_id)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        return _undo_command_status(
            canonical,
            _undo_command_row(conn, user_id, canonical),
        )


def _insert_undo_command_receipt(
    conn: Connection,
    user_id: str,
    command_id: str,
    operation_id: str,
    *,
    state: str,
    timestamp: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict:
    before = conn.total_changes
    conn.execute(
        "INSERT INTO application_update_undo_commands "
        "(user_id, command_id, operation_id, state, error_code, error_message, "
        "finished_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            command_id,
            operation_id,
            state,
            error_code,
            error_message,
            timestamp,
        ),
    )
    if conn.total_changes - before != 1:
        raise ApplicationUpdateOperationConflict(
            "岗位修改撤销命令落底发生了额外写入",
        )
    row = _undo_command_row(conn, user_id, command_id)
    status = _undo_command_status(command_id, row)
    if status["state"] != state or status["operation_id"] != operation_id:
        raise ApplicationUpdateOperationConflict("岗位修改撤销命令回执无法校验")
    return status


def _persist_undo_rejection(
    conn: Connection,
    user_id: str,
    command_id: str,
    operation_id: str,
    code: str,
) -> tuple[None, tuple[str, str]]:
    message = _UNDO_REJECTION_MESSAGES[code]
    _insert_undo_command_receipt(
        conn,
        user_id,
        command_id,
        operation_id,
        state="rejected",
        timestamp=now_iso(),
        error_code=code,
        error_message=message,
    )
    return None, (code, message)


def undo_application_update_operation(db_path: str, user_id: str,
                                      operation_id: str | UUID, *,
                                      command_id: str | UUID) -> dict:
    canonical = _canonical_uuid(operation_id, operation=True)
    canonical_command = _canonical_command_uuid(command_id)
    result: dict | None = None
    rejection: tuple[str, str] | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_command = _undo_command_row(conn, user_id, canonical_command)
        if existing_command is not None:
            status = _undo_command_status(canonical_command, existing_command)
            if status["operation_id"] != canonical:
                raise ApplicationUpdateOperationConflict(
                    "岗位修改撤销命令已绑定另一条操作",
                )
            if status["state"] == "rejected":
                rejection = (
                    status["error"]["code"],
                    status["error"]["message"],
                )
            else:
                operation_row = _operation_row(conn, user_id, canonical)
                if operation_row is None:
                    raise ApplicationUpdateOperationConflict(
                        "岗位修改撤销命令与操作回执不一致",
                    )
                result = _dto(conn, user_id, operation_row)
                if result["state"] != "undone":
                    raise ApplicationUpdateOperationConflict(
                        "岗位修改撤销命令与操作回执不一致",
                    )
        else:
            row = _operation_row(conn, user_id, canonical)
            if row is None:
                result, rejection = _persist_undo_rejection(
                    conn,
                    user_id,
                    canonical_command,
                    canonical,
                    "operation_not_found",
                )
            else:
                try:
                    current_dto = _dto(conn, user_id, row)
                except ApplicationUpdateOperationConflict:
                    result, rejection = _persist_undo_rejection(
                        conn,
                        user_id,
                        canonical_command,
                        canonical,
                        "operation_invalid",
                    )
                else:
                    if current_dto["state"] == "undone":
                        _insert_undo_command_receipt(
                            conn,
                            user_id,
                            canonical_command,
                            canonical,
                            state="completed",
                            timestamp=now_iso(),
                        )
                        result = current_dto
                    elif current_dto["state"] != "completed":
                        result, rejection = _persist_undo_rejection(
                            conn,
                            user_id,
                            canonical_command,
                            canonical,
                            "operation_invalid",
                        )
                    elif current_dto["undo_block_reason"] is not None:
                        result, rejection = _persist_undo_rejection(
                            conn,
                            user_id,
                            canonical_command,
                            canonical,
                            current_dto["undo_block_reason"],
                        )
                    else:
                        result, rejection = _undo_application_update_for_new_command(
                            conn,
                            user_id,
                            canonical,
                            canonical_command,
                            row,
                        )

    if rejection is not None:
        code, message = rejection
        if code == "operation_not_found":
            raise ApplicationUpdateOperationNotFound(message)
        raise ApplicationUpdateOperationConflict(message)
    if result is None:  # pragma: no cover - every transactional branch is terminal
        raise RuntimeError("application update undo command produced no terminal result")
    return result


def _undo_application_update_for_new_command(
    conn: Connection,
    user_id: str,
    canonical: str,
    canonical_command: str,
    row,
) -> tuple[dict | None, tuple[str, str] | None]:
    """Complete business undo and its command receipt under BEGIN IMMEDIATE."""
    try:
        proposal = _proposal(row[4])
        proposal_digest = _proposal_digest(proposal)
    except (TypeError, ValueError, ValidationError):
        return _persist_undo_rejection(
            conn,
            user_id,
            canonical_command,
            canonical,
            "operation_invalid",
        )
    operation = _operation_payload(row[5])
    apply_envelope = operation.get("apply")
    try:
        apply_result = ApplicationUpdateApplyResult.model_validate(
            apply_envelope.get("result") if isinstance(apply_envelope, dict) else None,
        )
    except (TypeError, ValueError, ValidationError):
        return _persist_undo_rejection(
            conn,
            user_id,
            canonical_command,
            canonical,
            "operation_invalid",
        )
    if (
        set(operation) != {"type", "client_turn_id", "proposal_digest", "apply"}
        or operation.get("type") != "application_update"
        or operation.get("client_turn_id") != proposal.client_turn_id
        or operation.get("proposal_digest") != proposal_digest
        or not isinstance(apply_envelope, dict)
        or set(apply_envelope) != {"command_hash", "result"}
        or apply_envelope.get("command_hash") != _command_hash(
            "apply", canonical, proposal_digest,
        )
        or not _result_matches(apply_result, proposal, undo=False)
    ):
        return _persist_undo_rejection(
            conn,
            user_id,
            canonical_command,
            canonical,
            "operation_invalid",
        )

    changed_fields = {item.field for item in proposal.effect.changed_fields}
    timestamp = now_iso()
    changes_before_operation = conn.total_changes
    try:
        raw_result = repository._undo_application_update_in_transaction(
            conn,
            user_id,
            proposal.target.application_id,
            created_time=proposal.target.application_created_time,
            expected_revision=proposal.final.revision,
            expected=proposal.final.model_dump(),
            replacement=proposal.before.model_dump(),
            changed_fields=changed_fields,
            question_provenance=[
                item.model_dump() for item in proposal.effect.question_provenance
            ],
            occurrence_provenance=[
                item.model_dump() for item in proposal.effect.question_occurrences
            ],
            invalidate_prep=proposal.effect.prep_invalidated,
            occurred_date=local_today().isoformat(),
            timestamp=timestamp,
        )
    except RuntimeError as error:
        raise ApplicationUpdateOperationConflict(
            "岗位修改撤销依赖未精确命中",
        ) from error
    undo_result = ApplicationUpdateUndoResult.model_validate(raw_result)
    # Undoing jd_text fires the same receipt invalidation trigger as the apply path.
    application_writes = (
        1
        + int(bool({"stage", "current_step"} & changed_fields))
        + int("jd_text" in changed_fields)
    )
    expected_changes = (
        application_writes
        + len(proposal.effect.question_provenance)
        + len(proposal.effect.question_occurrences)
    )
    if conn.total_changes - changes_before_operation != expected_changes:
        raise ApplicationUpdateOperationConflict(
            "岗位修改撤销发生了未展示的额外写入",
        )
    application = _raw_application(conn, user_id, proposal.target.application_id)
    if application is None:
        raise ApplicationUpdateOperationConflict("岗位修改撤销后目标消失")
    expected_projection = proposal.before.model_copy(update={
        "revision": proposal.final.revision + 1,
        "application_updated_time": timestamp,
    })
    if _projection(application) != expected_projection:
        raise ApplicationUpdateOperationConflict("岗位修改撤销投影不一致")
    if proposal.effect.prep_invalidated and not _prep_is_cleared(application):
        raise ApplicationUpdateOperationConflict("岗位修改撤销后 Prep 未精确失效")
    if "company" in changed_fields and not _undone_provenance_matches(
        conn, user_id, proposal, timestamp,
    ):
        raise ApplicationUpdateOperationConflict("岗位修改撤销 provenance 不一致")
    if not _result_matches(undo_result, proposal, undo=True):
        raise ApplicationUpdateOperationConflict("岗位修改撤销回执不一致")

    derivation = {"operation": {
        "type": "application_update",
        "client_turn_id": proposal.client_turn_id,
        "proposal_digest": proposal_digest,
        "apply": apply_envelope,
        "undo": {
            "command_hash": _command_hash("undo", canonical, proposal_digest),
            "result": undo_result.model_dump(),
        },
    }}
    changed = conn.execute(
        "UPDATE journal SET state = 'voided', processed_time = ?, "
        "derivation_json = ?, revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND state = 'applied' AND revision = ?",
        (
            timestamp,
            _canonical_json(derivation),
            user_id,
            row[0],
            row[6],
        ),
    ).rowcount
    if changed != 1:
        raise ApplicationUpdateOperationConflict("岗位修改操作状态已变化")
    if conn.total_changes - changes_before_operation != expected_changes + 1:
        raise ApplicationUpdateOperationConflict(
            "岗位修改撤销审计落底发生了额外写入",
        )
    application_after_journal = _raw_application(
        conn, user_id, proposal.target.application_id,
    )
    if (
        application_after_journal is None
        or _projection(application_after_journal) != expected_projection
    ):
        raise ApplicationUpdateOperationConflict("撤销审计落底后岗位投影漂移")
    if (
        proposal.effect.prep_invalidated
        and not _prep_is_cleared(application_after_journal)
    ):
        raise ApplicationUpdateOperationConflict("撤销审计落底后 Prep 漂移")
    if "company" in changed_fields and not _undone_provenance_matches(
        conn, user_id, proposal, timestamp,
    ):
        raise ApplicationUpdateOperationConflict("撤销审计落底后 provenance 漂移")
    terminal = _operation_row(conn, user_id, canonical)
    if terminal is None:  # pragma: no cover
        raise ApplicationUpdateOperationNotFound("岗位修改操作不存在")
    undone = _dto(conn, user_id, terminal)
    if undone["state"] != "undone":
        raise ApplicationUpdateOperationConflict("岗位修改撤销回执无法校验")
    _insert_undo_command_receipt(
        conn,
        user_id,
        canonical_command,
        canonical,
        state="completed",
        timestamp=timestamp,
    )
    if conn.total_changes - changes_before_operation != expected_changes + 2:
        raise ApplicationUpdateOperationConflict(
            "岗位修改撤销命令落底发生了额外写入",
        )
    return undone, None
