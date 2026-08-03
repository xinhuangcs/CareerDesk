"""Trusted application deletion with frozen targets, drift checks, and receipts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import UUID, uuid4

from pydantic import ValidationError

from ....platform.database import (
    application_identity_key,
    normalize_application_identity_part,
    now_iso,
    read_connection,
    transaction,
)
from .. import repository
from .models import (
    APPLICATION_DELETE_CONTRACT_VERSION,
    MAX_DELETE_JD_PREVIEW_CHARS,
    MAX_DELETE_QUESTIONS,
    MAX_DELETE_QUESTION_PREVIEW_CHARS,
    MAX_DELETE_RESUMES,
    MAX_DELETE_TIMELINE_ENTRIES,
    ApplicationDeleteEffect,
    ApplicationDeleteOperationDTO,
    ApplicationDeleteOperationError,
    ApplicationDeleteProposal,
    ApplicationDeleteQuestion,
    ApplicationDeleteResult,
    ApplicationDeleteResume,
    ApplicationDeleteResumeRef,
    ApplicationDeleteTarget,
    ApplicationDeleteTimelineEntry,
)

MAX_APPLICATION_SOURCE_CHARS = 500_000
MAX_PERSISTED_JSON_CHARS = 750_000
MAX_DEPENDENCY_JSON_CHARS = 2_000_000
MAX_QUESTION_OCCURRENCES = 1_000
MAX_PENDING_DELETE_OPERATIONS = 200
MAX_APPLICATION_DELETE_OPERATIONS_PER_BATCH = 200
MAX_SELECTOR_OPTIONS = 100
SAFE_FALLBACK_TIMESTAMP = "1970-01-01T00:00:00+00:00"

_OPERATION_COLUMNS = (
    "id, operation_id, state, created_time, extraction_json, derivation_json, revision"
)


class ApplicationDeleteOperationNotFound(LookupError):
    """Deletion operation is absent or belongs to another user."""


class ApplicationDeleteOperationConflict(RuntimeError):
    """Terminal operation, target drift, or corrupt durable contract."""


class _UnsafeDependency(ValueError):
    """Application or dependencies are too corrupt for a trusted preview."""


@dataclass(frozen=True, slots=True)
class _Bundle:
    target: ApplicationDeleteTarget
    effect: ApplicationDeleteEffect
    fingerprint: str


def _bounded_text(value, limit: int, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise _UnsafeDependency(f"{label} 超过安全边界或类型损坏")
    return value


def _bounded_rows(rows: list, limit: int, label: str) -> list:
    if len(rows) > limit:
        raise _UnsafeDependency(f"{label} 超过安全行数上限")
    return rows


def _json_object(raw: str | None, label: str, *, limit: int = MAX_APPLICATION_SOURCE_CHARS) -> dict:
    if raw is None:
        return {}
    _bounded_text(raw, limit, label)
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise _UnsafeDependency(f"{label} 不是有效 JSON object") from error
    if not isinstance(loaded, dict):
        raise _UnsafeDependency(f"{label} 不是 JSON object")
    return loaded


def _canonical_fingerprint(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_DEPENDENCY_JSON_CHARS:
        raise _UnsafeDependency("岗位删除完整依赖超过安全预算")
    return sha256(encoded.encode("utf-8")).hexdigest()


def _safe_timestamp(value) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    cleaned = value.strip()
    return cleaned or None


def _ensure_no_cross_tenant_reference(conn: Connection, user_id: str,
                                      application_id: int) -> None:
    for table in (
        "timeline_entries", "questions", "review_question_occurrences", "resumes",
    ):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE application_id = ? AND user_id != ? LIMIT 1",
            (application_id, user_id),
        ).fetchone()
        if row is not None:
            raise _UnsafeDependency(f"{table} 存在跨租户岗位引用")


def _load_bundle(conn: Connection, user_id: str, application_id: int) -> _Bundle:
    row = conn.execute(
        "SELECT id, user_id, company, company_id, position, department, channel, jd_text, "
        "jd_parsed_json, stage, current_step, current_state_entry_id, next_stage, next_step, "
        "next_date, next_time, next_note, paused_from_stage, pause_reason, application_note, "
        "priority, resume_id, applied_date, prep_status, prep_generation, "
        "prep_heartbeat_time, prep_json, revision, created_time, updated_time "
        "FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if row is None:
        raise ApplicationDeleteOperationNotFound("找不到这条岗位记录")
    (
        target_id,
        _target_user_id,
        company,
        company_id,
        position,
        department,
        channel,
        jd_text,
        jd_parsed_json,
        stage,
        current_step,
        _current_state_entry_id,
        next_stage,
        next_step,
        next_date,
        next_time,
        next_note,
        paused_from_stage,
        pause_reason,
        application_note,
        priority,
        resume_id,
        applied_date,
        prep_status,
        prep_generation,
        prep_heartbeat_time,
        prep_json,
        _revision,
        created_time,
        updated_time,
    ) = row

    _bounded_text(company, 200, "application company")
    _bounded_text(position, 300, "application position")
    _bounded_text(department, 200, "application department", nullable=True)
    _bounded_text(channel, 100, "application channel", nullable=True)
    _bounded_text(jd_text, MAX_APPLICATION_SOURCE_CHARS, "application JD", nullable=True)
    _bounded_text(
        jd_parsed_json,
        MAX_APPLICATION_SOURCE_CHARS,
        "application JD parsed",
        nullable=True,
    )
    _bounded_text(current_step, 300, "application current step", nullable=True)
    _bounded_text(next_step, 300, "application next step", nullable=True)
    _bounded_text(next_note, 2_000, "application next note", nullable=True)
    _bounded_text(pause_reason, 2_000, "application pause reason", nullable=True)
    _bounded_text(application_note, 2_000, "application note", nullable=True)
    _bounded_text(prep_generation, 500, "application prep generation", nullable=True)
    _bounded_text(prep_heartbeat_time, 64, "application prep heartbeat", nullable=True)
    _bounded_text(prep_json, MAX_APPLICATION_SOURCE_CHARS, "application prep", nullable=True)
    _bounded_text(created_time, 64, "application created_time")
    _bounded_text(updated_time, 64, "application updated_time")
    if priority not in {None, "high", "medium", "low"}:
        raise _UnsafeDependency("application priority 类型损坏")
    parsed_jd = _json_object(jd_parsed_json, "application JD parsed")
    skills = parsed_jd.get("skills", [])
    highlights = parsed_jd.get("highlights", [])
    if not isinstance(skills, list) or not isinstance(highlights, list):
        raise _UnsafeDependency("application JD skills/highlights 类型损坏")

    company_dependency = None
    if company_id is not None:
        company_dependency = conn.execute(
            "SELECT id, user_id, name FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        if company_dependency is None or company_dependency[1] != user_id:
            raise _UnsafeDependency("application company_id 缺少同租户绑定")

    selected_resume = None
    selected_resume_dependency = None
    if resume_id is not None:
        selected_resume_dependency = conn.execute(
            "SELECT id, user_id, name, binding, application_id, archived, updated_time "
            "FROM resumes WHERE id = ?",
            (resume_id,),
        ).fetchone()
        if selected_resume_dependency is None or selected_resume_dependency[1] != user_id:
            raise _UnsafeDependency("application resume_id 缺少同租户绑定")
        if selected_resume_dependency[5] not in {0, 1}:
            raise _UnsafeDependency("selected resume archived 类型损坏")
        selected_resume = ApplicationDeleteResumeRef(
            id=selected_resume_dependency[0],
            name=selected_resume_dependency[2],
            archived=selected_resume_dependency[5] == 1,
        )

    _ensure_no_cross_tenant_reference(conn, user_id, target_id)

    raw_entry_rows = _bounded_rows(conn.execute(
        "SELECT id, application_id, step, occurred_date, outcome, summary, "
        "from_stage, from_step, to_stage, to_step, journal_id, created_time "
        "FROM timeline_entries WHERE user_id = ? AND application_id = ? "
        "ORDER BY id LIMIT ?",
        (user_id, target_id, MAX_DELETE_TIMELINE_ENTRIES + 1),
    ).fetchall(), MAX_DELETE_TIMELINE_ENTRIES, "application timeline entries")
    timeline_entries: list[ApplicationDeleteTimelineEntry] = []
    entry_dependencies: list[list] = []
    for entry_row in raw_entry_rows:
        (
            entry_id,
            bound_id,
            step,
            occurred_date,
            outcome,
            summary,
            from_stage,
            from_step,
            to_stage,
            to_step,
            _journal_id,
            entry_time,
        ) = entry_row
        _bounded_text(summary, 2_000, "application timeline summary", nullable=True)
        _bounded_text(entry_time, 64, "application timeline created_time")
        timeline_entries.append(ApplicationDeleteTimelineEntry(
            id=entry_id,
            step=step,
            occurred_date=occurred_date,
            outcome=outcome,
            summary=summary,
            from_stage=from_stage,
            from_step=from_step,
            to_stage=to_stage,
            to_step=to_step,
        ))
        entry_dependencies.append(list(entry_row))
        if bound_id != target_id:
            raise _UnsafeDependency("timeline entry application binding drifted")

    raw_question_rows = _bounded_rows(conn.execute(
        "SELECT id, application_id, text, source, company, source_step, asked_date, status, "
        "journal_id, updated_time FROM questions WHERE user_id = ? AND application_id = ? "
        "ORDER BY id LIMIT ?",
        (user_id, target_id, MAX_DELETE_QUESTIONS + 1),
    ).fetchall(), MAX_DELETE_QUESTIONS, "application questions")
    questions: list[ApplicationDeleteQuestion] = []
    question_dependencies: list[list] = []
    for question_row in raw_question_rows:
        question_id, bound_id, text, source, *_ = question_row
        text = _bounded_text(text, 100_000, "application question text")
        questions.append(ApplicationDeleteQuestion(
            id=question_id,
            text_preview=text[:MAX_DELETE_QUESTION_PREVIEW_CHARS],
            text_truncated=len(text) > MAX_DELETE_QUESTION_PREVIEW_CHARS,
            source=source,
        ))
        question_dependencies.append(list(question_row))
        if bound_id != target_id:
            raise _UnsafeDependency("question application binding drifted")

    raw_occurrence_rows = _bounded_rows(conn.execute(
        "SELECT occurrence.user_id, occurrence.journal_id, occurrence.question_id, "
        "occurrence.application_id, occurrence.company, occurrence.source_step, "
        "occurrence.asked_date, question.id, source.id "
        "FROM review_question_occurrences occurrence "
        "LEFT JOIN questions question ON question.id = occurrence.question_id "
        "AND question.user_id = occurrence.user_id "
        "LEFT JOIN journal source ON source.id = occurrence.journal_id "
        "AND source.user_id = occurrence.user_id "
        "WHERE occurrence.user_id = ? AND occurrence.application_id = ? "
        "ORDER BY occurrence.journal_id, occurrence.question_id LIMIT ?",
        (user_id, target_id, MAX_QUESTION_OCCURRENCES + 1),
    ).fetchall(), MAX_QUESTION_OCCURRENCES, "application review occurrences")
    occurrence_dependencies: list[list] = []
    for occurrence_row in raw_occurrence_rows:
        if occurrence_row[3] != target_id:
            raise _UnsafeDependency("review occurrence application binding drifted")
        if occurrence_row[7] != occurrence_row[2] or occurrence_row[8] != occurrence_row[1]:
            raise _UnsafeDependency("review occurrence 缺少同租户 question/journal 绑定")
        occurrence_dependencies.append(list(occurrence_row[:7]))

    raw_resume_rows = _bounded_rows(conn.execute(
        "SELECT id, application_id, name, binding, archived, updated_time FROM resumes "
        "WHERE user_id = ? AND application_id = ? ORDER BY id LIMIT ?",
        (user_id, target_id, MAX_DELETE_RESUMES + 1),
    ).fetchall(), MAX_DELETE_RESUMES, "application resumes")
    resumes: list[ApplicationDeleteResume] = []
    resume_dependencies: list[list] = []
    for resume_row in raw_resume_rows:
        bound_resume_id, bound_id, name, binding, _archived, resume_updated = resume_row
        _bounded_text(name, 500, "application resume name")
        _bounded_text(resume_updated, 64, "application resume updated_time")
        if _archived not in {0, 1}:
            raise _UnsafeDependency("application resume archived 类型损坏")
        resumes.append(ApplicationDeleteResume(
            id=bound_resume_id,
            name=name,
            binding=binding,
            archived=_archived == 1,
        ))
        resume_dependencies.append(list(resume_row))
        if bound_id != target_id:
            raise _UnsafeDependency("resume application binding drifted")

    jd_value = jd_text or ""
    target = ApplicationDeleteTarget(
        application_id=target_id,
        company=company,
        position=position,
        department=department,
        channel=channel,
        stage=stage,
        current_step=current_step,
        priority=priority,
        selected_resume=selected_resume,
        applied_date=applied_date,
        next_action=(
            {
                "stage": next_stage,
                "step": next_step,
                "date": next_date,
                "time": next_time,
                "note": next_note,
            }
            if next_step is not None else None
        ),
        paused_from_stage=paused_from_stage,
        pause_reason=pause_reason,
        application_note=application_note,
        jd_preview=jd_value[:MAX_DELETE_JD_PREVIEW_CHARS],
        jd_truncated=len(jd_value) > MAX_DELETE_JD_PREVIEW_CHARS,
        skills=skills,
        highlights=highlights,
        prep_status=prep_status,
        prep_artifact_present=prep_json is not None,
        application_created_time=created_time,
        application_updated_time=updated_time,
    )
    effect = ApplicationDeleteEffect(
        timeline_entries=timeline_entries,
        questions_detached=questions,
        question_occurrences_detached=len(raw_occurrence_rows),
        resumes_detached=resumes,
        selected_resume_retained=True,
        company_records_untouched=True,
        journal_records_untouched=True,
        external_logs_untouched=True,
    )
    # A worker heartbeat is lease liveness, not a user-visible delete dependency.
    # Keep status/generation/artifact in the fingerprint, but do not turn every
    # heartbeat into an unapprovable confirmation card.
    application_dependency = [*row[:25], *row[26:]]
    dependency = {
        "application": application_dependency,
        "company": list(company_dependency) if company_dependency is not None else None,
        "selected_resume": (
            list(selected_resume_dependency) if selected_resume_dependency is not None else None
        ),
        "timeline_entries": entry_dependencies,
        "questions": question_dependencies,
        "occurrences": occurrence_dependencies,
        "resumes": resume_dependencies,
    }
    return _Bundle(
        target=target,
        effect=effect,
        fingerprint=_canonical_fingerprint(dependency),
    )


def _canonical_operation_id(operation_id: str | UUID) -> str:
    try:
        return str(UUID(str(operation_id)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ApplicationDeleteOperationNotFound("岗位删除操作不存在") from error


def _proposal_digest(proposal: ApplicationDeleteProposal) -> str:
    encoded = json.dumps(
        proposal.model_dump(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    _bounded_text(encoded, MAX_PERSISTED_JSON_CHARS, "application delete proposal digest input")
    return sha256(encoded.encode("utf-8")).hexdigest()


def _command_hash(action: str, operation_id: str, proposal_digest: str) -> str:
    encoded = json.dumps(
        {
            "action": action,
            "operation_id": operation_id,
            "proposal_digest": proposal_digest,
            "version": 2,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
        "AND kind = 'correction' AND operation_id = ? "
        "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.operation.type') = 'application_delete'",
        (user_id, operation_id),
    ).fetchone()


def _proposal(raw: str | None) -> ApplicationDeleteProposal:
    _bounded_text(raw, MAX_PERSISTED_JSON_CHARS, "application delete proposal")
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise _UnsafeDependency("application delete proposal 不是有效 JSON") from error
    return ApplicationDeleteProposal.model_validate(loaded)


def _operation_payload(raw: str | None) -> dict:
    if not isinstance(raw, str) or len(raw) > MAX_PERSISTED_JSON_CHARS:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    operation = loaded.get("operation", {})
    return operation if isinstance(operation, dict) else {}


def _fallback_target(created_time: str) -> ApplicationDeleteTarget:
    return ApplicationDeleteTarget(
        application_id=1,
        company="未知公司",
        position="未知岗位",
        department=None,
        channel=None,
        stage="backlog",
        current_step=None,
        priority=None,
        selected_resume=None,
        applied_date=None,
        next_action=None,
        paused_from_stage=None,
        pause_reason=None,
        application_note=None,
        jd_preview="",
        jd_truncated=False,
        skills=[],
        highlights=[],
        prep_status="none",
        prep_artifact_present=False,
        application_created_time=created_time,
        application_updated_time=created_time,
    )


def _fallback_effect() -> ApplicationDeleteEffect:
    return ApplicationDeleteEffect(
        timeline_entries=[],
        questions_detached=[],
        question_occurrences_detached=0,
        resumes_detached=[],
        selected_resume_retained=True,
        company_records_untouched=True,
        journal_records_untouched=True,
        external_logs_untouched=True,
    )


def _result_matches_proposal(
    result: ApplicationDeleteResult,
    proposal: ApplicationDeleteProposal,
) -> bool:
    expected = {
        "timeline_entries_removed": len(proposal.effect.timeline_entries),
        "questions_detached": len(proposal.effect.questions_detached),
        "question_occurrences_detached": proposal.effect.question_occurrences_detached,
        "resumes_detached": len(proposal.effect.resumes_detached),
    }
    actual = result.model_dump(exclude={"status", "application_id"})
    return result.application_id == proposal.target.application_id and actual == expected


def _dto(conn: Connection, user_id: str, row) -> dict:
    (_journal_id, operation_id, journal_state, created_time, extraction_json,
     derivation_json, _revision) = row
    safe_created_time = _safe_timestamp(created_time)
    try:
        proposal = _proposal(extraction_json)
        proposal_digest = _proposal_digest(proposal)
    except (TypeError, ValueError):
        proposal = None
        proposal_digest = None
    operation = _operation_payload(derivation_json)
    result = None
    initial_envelope = (
        {"type": "application_delete", "proposal_digest": proposal_digest}
        if proposal_digest is not None
        else None
    )
    if (journal_state == "awaiting_user" and proposal is not None
            and operation == initial_envelope):
        try:
            bundle = _load_bundle(conn, user_id, proposal.target.application_id)
            live = (
                bundle.fingerprint == proposal.dependency_fingerprint
                and bundle.target == proposal.target
                and bundle.effect == proposal.effect
            )
        except (
            ApplicationDeleteOperationNotFound,
            _UnsafeDependency,
            ValidationError,
            ValueError,
        ):
            live = False
        state = "pending" if live else "stale"
    elif (journal_state == "applied" and proposal is not None
          and proposal_digest is not None
          and set(operation) == {
              "type", "proposal_digest", "action", "command_hash", "result",
          }
          and operation.get("proposal_digest") == proposal_digest
          and operation.get("action") == "approve"):
        try:
            parsed_result = ApplicationDeleteResult.model_validate(operation.get("result"))
        except (TypeError, ValueError):
            state = "stale"
        else:
            if (operation.get("command_hash") == _command_hash(
                    "approve", operation_id, proposal_digest,
                )
                    and _result_matches_proposal(parsed_result, proposal)):
                result = parsed_result.model_dump()
                state = "completed"
            else:
                state = "stale"
    elif (journal_state == "voided" and proposal is not None
          and proposal_digest is not None
          and set(operation) == {"type", "proposal_digest", "action", "command_hash"}
          and operation.get("proposal_digest") == proposal_digest
          and operation.get("action") == "reject"
          and operation.get("command_hash") == _command_hash(
              "reject", operation_id, proposal_digest,
          )):
        state = "rejected"
    else:
        state = "stale"
    if safe_created_time is None:
        state = "stale"
        result = None
    target = proposal.target if proposal is not None else _fallback_target(
        safe_created_time or SAFE_FALLBACK_TIMESTAMP,
    )
    effect = proposal.effect if proposal is not None else _fallback_effect()
    return ApplicationDeleteOperationDTO(
        operation_id=operation_id,
        operation_type="application_delete",
        state=state,
        created_time=safe_created_time or SAFE_FALLBACK_TIMESTAMP,
        target=target,
        effect=effect,
        result=result,
    ).model_dump()


def _locate_target(conn: Connection, user_id: str, *, company: str,
                   position: str | None, application_id: int | None) -> int | dict:
    if application_id is not None:
        if (isinstance(application_id, bool)
                or not isinstance(application_id, int)
                or application_id < 1):
            return {"status": "not_found"}
        row = conn.execute(
            "SELECT id FROM applications WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        return row[0] if row is not None else {"status": "not_found"}
    if position is not None:
        try:
            identity_key = application_identity_key(company, position)
        except ValueError:
            return {"status": "not_found"}
        row = conn.execute(
            "SELECT id FROM applications WHERE user_id = ? "
            "AND company_key = ? AND position_key = ?",
            (user_id, *identity_key),
        ).fetchone()
        return row[0] if row is not None else {"status": "not_found"}
    company_key = normalize_application_identity_part(company)
    if not company_key:
        return {"status": "not_found"}
    rows = conn.execute(
        "SELECT id, position FROM applications WHERE user_id = ? "
        "AND company_key = ? "
        "ORDER BY position, id LIMIT ?",
        (user_id, company_key, MAX_SELECTOR_OPTIONS + 1),
    ).fetchall()
    if len(rows) > MAX_SELECTOR_OPTIONS:
        raise ApplicationDeleteOperationConflict("同公司岗位过多，请提供准确岗位名")
    if len(rows) == 1:
        return rows[0][0]
    if rows:
        return {"status": "ambiguous", "options": [row[1] for row in rows]}
    return {"status": "not_found"}


def _mark_stale(conn: Connection, user_id: str, journal_id: int,
                expected_revision: int, code: str) -> None:
    error = ApplicationDeleteOperationError(
        code=code,
        message="岗位或其关联记录已变化，请重新生成删除预览。",
    )
    derivation = {
        "operation": {
            "type": "application_delete",
            "action": "stale",
            "error": error.model_dump(),
        },
    }
    changed = conn.execute(
        "UPDATE journal SET state = 'superseded', processed_time = NULL, "
        "derivation_json = ?, revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND state = 'awaiting_user' AND revision = ?",
        (json.dumps(derivation, ensure_ascii=False), user_id, journal_id, expected_revision),
    ).rowcount
    if changed != 1:
        raise ApplicationDeleteOperationConflict("岗位删除操作状态已变化")


def _validate_delete_selector(
    *,
    company: object,
    position: object = None,
) -> tuple[str, str | None]:
    if not isinstance(company, str) or not company.strip() or len(company) > 200:
        raise ValueError("删除岗位需要准确公司名")
    canonical_company = company.strip()
    if position is None:
        return canonical_company, None
    if not isinstance(position, str) or not position.strip() or len(position) > 300:
        raise ValueError("岗位名不符合安全边界")
    return canonical_company, position.strip()


def _prepare_application_delete_operation_in_transaction(
    conn: Connection,
    user_id: str,
    *,
    company: str,
    position: str | None = None,
    application_id: int | None = None,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> dict:
    located = _locate_target(
        conn,
        user_id,
        company=company,
        position=position,
        application_id=application_id,
    )
    if isinstance(located, dict):
        return located
    target_id = located
    existing_rows = conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
        "AND kind = 'correction' AND operation_id IS NOT NULL "
        "AND state = 'awaiting_user' "
        "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.operation.type') = 'application_delete' "
        "AND CAST(json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json "
        "ELSE '{}' END, '$.target.application_id') AS INTEGER) = ? "
        "ORDER BY id DESC LIMIT 2",
        (user_id, target_id),
    ).fetchall()
    for existing in existing_rows:
        dto = _dto(conn, user_id, existing)
        if dto["state"] == "pending":
            if proposal_recorder is not None:
                proposal_recorder(conn, "application_delete", dto["operation_id"])
            return dto
        _mark_stale(conn, user_id, existing[0], existing[6], "dependency_drifted")
    pending_rows = conn.execute(
        "SELECT id FROM journal WHERE user_id = ? AND kind = 'correction' "
        "AND operation_id IS NOT NULL AND state = 'awaiting_user' "
        "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.operation.type') = 'application_delete' "
        "ORDER BY id LIMIT ?",
        (user_id, MAX_PENDING_DELETE_OPERATIONS + 1),
    ).fetchall()
    if len(pending_rows) >= MAX_PENDING_DELETE_OPERATIONS:
        raise ApplicationDeleteOperationConflict("待确认岗位删除操作已达安全上限")
    try:
        bundle = _load_bundle(conn, user_id, target_id)
    except _UnsafeDependency as error:
        raise ApplicationDeleteOperationConflict(str(error)) from error
    except ValidationError as error:
        raise ApplicationDeleteOperationConflict("岗位或其关联记录不符合安全契约") from error
    proposal = ApplicationDeleteProposal(
        operation_type="application_delete",
        contract_version=APPLICATION_DELETE_CONTRACT_VERSION,
        dependency_fingerprint=bundle.fingerprint,
        target=bundle.target,
        effect=bundle.effect,
    )
    proposal_json = json.dumps(proposal.model_dump(), ensure_ascii=False)
    _bounded_text(proposal_json, MAX_PERSISTED_JSON_CHARS, "application delete proposal")
    proposal_digest = _proposal_digest(proposal)
    operation_id = str(uuid4())
    created_time = now_iso()
    cursor = conn.execute(
        "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
        "derivation_json, state, operation_id) "
        "VALUES (?, 'correction', ?, ?, ?, ?, 'awaiting_user', ?)",
        (
            user_id,
            f"[待确认删除岗位 #{target_id}]",
            created_time,
            proposal_json,
            json.dumps({
                "operation": {
                    "type": "application_delete",
                    "proposal_digest": proposal_digest,
                },
            }, ensure_ascii=False),
            operation_id,
        ),
    )
    operation_row = _operation_row(conn, user_id, operation_id)
    if operation_row is None:  # pragma: no cover
        raise RuntimeError(f"application delete operation insert lost: {cursor.lastrowid}")
    pending = _dto(conn, user_id, operation_row)
    if pending["state"] != "pending":  # pragma: no cover - transaction invariant
        raise RuntimeError("application delete proposal receipt is not pending")
    if proposal_recorder is not None:
        proposal_recorder(conn, "application_delete", pending["operation_id"])
    return pending


def prepare_application_delete_operation(
    db_path: str,
    user_id: str,
    *,
    company: str,
    position: str | None = None,
    application_id: int | None = None,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> dict:
    """Atomically resolve/freeze a preview, reusing a pending live target."""
    company, position = _validate_delete_selector(company=company, position=position)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _prepare_application_delete_operation_in_transaction(
            conn,
            user_id,
            company=company,
            position=position,
            application_id=application_id,
            proposal_recorder=proposal_recorder,
        )


def prepare_application_delete_operations(
    db_path: str,
    user_id: str,
    targets: list[dict],
    *,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> list[dict]:
    """Freeze a complete deletion batch atomically, leaving no partial proposals."""
    if not isinstance(targets, list) or not (
        1 <= len(targets) <= MAX_APPLICATION_DELETE_OPERATIONS_PER_BATCH
    ):
        raise ValueError(
            "批量删除必须包含 1–"
            f"{MAX_APPLICATION_DELETE_OPERATIONS_PER_BATCH} 条岗位",
        )
    normalized: list[tuple[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"company", "position"}:
            raise ValueError("批量删除的每一项都必须只包含 company 和 position")
        company, position = _validate_delete_selector(
            company=target.get("company"),
            position=target.get("position"),
        )
        if position is None:  # pragma: no cover - exact shape above, defensive only
            raise ValueError("批量删除必须为每条岗位提供准确岗位名")
        identity = application_identity_key(company, position)
        if identity in identities:
            raise ValueError("批量删除不能包含重复岗位")
        identities.add(identity)
        normalized.append((company, position))

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _prepare_application_delete_batch_in_transaction(
            conn,
            user_id,
            [(company, position, None) for company, position in normalized],
            proposal_recorder=proposal_recorder,
        )


def prepare_all_application_delete_operations(
    db_path: str,
    user_id: str,
    *,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> list[dict]:
    """Resolve and freeze the user's complete current application set atomically."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, company, position FROM applications WHERE user_id = ? "
            "ORDER BY id LIMIT ?",
            (user_id, MAX_APPLICATION_DELETE_OPERATIONS_PER_BATCH + 1),
        ).fetchall()
        if len(rows) > MAX_APPLICATION_DELETE_OPERATIONS_PER_BATCH:
            raise ValueError(
                "批量删除必须包含 1–"
                f"{MAX_APPLICATION_DELETE_OPERATIONS_PER_BATCH} 条岗位",
            )
        return _prepare_application_delete_batch_in_transaction(
            conn,
            user_id,
            [(company, position, application_id) for application_id, company, position in rows],
            proposal_recorder=proposal_recorder,
        )


def _prepare_application_delete_batch_in_transaction(
    conn: Connection,
    user_id: str,
    targets: list[tuple[str, str, int | None]],
    *,
    proposal_recorder: Callable[[Connection, str, str], object] | None,
) -> list[dict]:
    operations: list[dict] = []
    for company, position, application_id in targets:
        operation = _prepare_application_delete_operation_in_transaction(
            conn,
            user_id,
            company=company,
            position=position,
            application_id=application_id,
            proposal_recorder=proposal_recorder,
        )
        if operation.get("status") == "not_found":
            raise ApplicationDeleteOperationConflict(
                f"没找到精确匹配的岗位记录：{company}·{position}",
            )
        if operation.get("status") == "ambiguous":
            raise ApplicationDeleteOperationConflict(
                f"岗位记录无法唯一定位：{company}·{position}",
            )
        operations.append(operation)
    return operations


def list_pending_application_delete_operations(db_path: str, user_id: str) -> list[dict]:
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND operation_id IS NOT NULL "
            "AND state = 'awaiting_user' "
            "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'application_delete' "
            "ORDER BY created_time DESC, id DESC LIMIT ?",
            (user_id, MAX_PENDING_DELETE_OPERATIONS + 1),
        ).fetchall()
        if len(rows) > MAX_PENDING_DELETE_OPERATIONS:
            raise ApplicationDeleteOperationConflict("待确认岗位删除操作超过安全上限")
        operations = [_dto(conn, user_id, row) for row in rows]
    return [operation for operation in operations if operation["state"] == "pending"]


def get_application_delete_operation(db_path: str, user_id: str,
                                     operation_id: str | UUID) -> dict | None:
    canonical = _canonical_operation_id(operation_id)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _operation_row(conn, user_id, canonical)
        return _dto(conn, user_id, row) if row is not None else None


def _assert_delete_postconditions(conn: Connection, user_id: str,
                                  application_id: int) -> None:
    if conn.execute(
        "SELECT 1 FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone() is not None:
        raise ApplicationDeleteOperationConflict("岗位删除后目标仍然存在")
    for table in (
        "timeline_entries", "questions", "review_question_occurrences", "resumes",
    ):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE user_id = ? AND application_id = ? LIMIT 1",
            (user_id, application_id),
        ).fetchone() is not None:
            raise ApplicationDeleteOperationConflict(f"岗位删除后 {table} 仍有绑定")


def approve_application_delete_operation(db_path: str, user_id: str,
                                         operation_id: str | UUID) -> dict:
    canonical = _canonical_operation_id(operation_id)
    conflict_reason: str | None = None
    completed: dict | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _operation_row(conn, user_id, canonical)
        if row is None:
            raise ApplicationDeleteOperationNotFound("岗位删除操作不存在")
        operation = _operation_payload(row[5])
        if row[2] != "awaiting_user":
            current = _dto(conn, user_id, row)
            if current["state"] == "completed":
                return current
            raise ApplicationDeleteOperationConflict(
                f"该操作当前为 {current['state']}，不能批准",
            )
        if _safe_timestamp(row[3]) is None:
            conflict_reason = "envelope_invalid"
            _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
        else:
            try:
                proposal = _proposal(row[4])
            except (TypeError, ValueError):
                conflict_reason = "contract_invalid"
                _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
            else:
                proposal_digest = _proposal_digest(proposal)
                expected_envelope = {
                    "type": "application_delete",
                    "proposal_digest": proposal_digest,
                }
                if operation != expected_envelope:
                    conflict_reason = "envelope_invalid"
                    _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
                else:
                    try:
                        bundle = _load_bundle(conn, user_id, proposal.target.application_id)
                    except (
                        ApplicationDeleteOperationNotFound,
                        _UnsafeDependency,
                        ValidationError,
                        ValueError,
                    ):
                        bundle = None
                    if (bundle is None
                            or bundle.fingerprint != proposal.dependency_fingerprint
                            or bundle.target != proposal.target
                            or bundle.effect != proposal.effect):
                        conflict_reason = "dependency_drifted"
                        _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
                    else:
                        raw_result = repository._delete_application_in_transaction(
                            conn,
                            user_id,
                            proposal.target.application_id,
                        )
                        result = ApplicationDeleteResult.model_validate(raw_result)
                        if not _result_matches_proposal(result, proposal):
                            raise ApplicationDeleteOperationConflict(
                                "岗位删除的实际影响与冻结预览不一致",
                            )
                        _assert_delete_postconditions(
                            conn,
                            user_id,
                            proposal.target.application_id,
                        )
                        command_hash = _command_hash(
                            "approve", canonical, proposal_digest,
                        )
                        derivation = {
                            "operation": {
                                "type": "application_delete",
                                "proposal_digest": proposal_digest,
                                "action": "approve",
                                "command_hash": command_hash,
                                "result": result.model_dump(),
                            },
                        }
                        changed = conn.execute(
                            "UPDATE journal SET state = 'applied', processed_time = ?, "
                            "derivation_json = ?, revision = revision + 1 "
                            "WHERE user_id = ? AND id = ? AND kind = 'correction' "
                            "AND state = 'awaiting_user' AND revision = ?",
                            (
                                now_iso(),
                                json.dumps(derivation, ensure_ascii=False),
                                user_id,
                                row[0],
                                row[6],
                            ),
                        ).rowcount
                        if changed != 1:
                            raise ApplicationDeleteOperationConflict(
                                "岗位删除操作状态已变化",
                            )
                        terminal = _operation_row(conn, user_id, canonical)
                        if terminal is None:  # pragma: no cover
                            raise ApplicationDeleteOperationNotFound("岗位删除操作不存在")
                        completed = _dto(conn, user_id, terminal)
                        if completed["state"] != "completed":
                            raise ApplicationDeleteOperationConflict(
                                "岗位删除完成回执无法与冻结预览核对",
                            )
    if conflict_reason is not None:
        raise ApplicationDeleteOperationConflict("岗位删除预览已失效，请重新生成")
    if completed is None:  # pragma: no cover
        raise ApplicationDeleteOperationConflict("岗位删除操作未完成")
    return completed


def reject_application_delete_operation(db_path: str, user_id: str,
                                        operation_id: str | UUID) -> dict:
    canonical = _canonical_operation_id(operation_id)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _operation_row(conn, user_id, canonical)
        if row is None:
            raise ApplicationDeleteOperationNotFound("岗位删除操作不存在")
        operation = _operation_payload(row[5])
        if row[2] != "awaiting_user":
            current = _dto(conn, user_id, row)
            if current["state"] == "rejected":
                return current
            raise ApplicationDeleteOperationConflict(
                f"该操作当前为 {current['state']}，不能拒绝",
            )
        try:
            if _safe_timestamp(row[3]) is None:
                raise ValueError("invalid operation timestamp")
            proposal = _proposal(row[4])
            proposal_digest = _proposal_digest(proposal)
            if operation != {
                "type": "application_delete",
                "proposal_digest": proposal_digest,
            }:
                raise ValueError("proposal envelope digest mismatch")
        except (TypeError, ValueError):
            _mark_stale(conn, user_id, row[0], row[6], "contract_invalid")
            stale = True
        else:
            stale = False
            command_hash = _command_hash("reject", canonical, proposal_digest)
            derivation = {
                "operation": {
                    "type": "application_delete",
                    "proposal_digest": proposal_digest,
                    "action": "reject",
                    "command_hash": command_hash,
                },
            }
            changed = conn.execute(
                "UPDATE journal SET state = 'voided', processed_time = NULL, "
                "derivation_json = ?, revision = revision + 1 "
                "WHERE user_id = ? AND id = ? AND kind = 'correction' "
                "AND state = 'awaiting_user' AND revision = ?",
                (
                    json.dumps(derivation, ensure_ascii=False),
                    user_id,
                    row[0],
                    row[6],
                ),
            ).rowcount
            if changed != 1:
                raise ApplicationDeleteOperationConflict("岗位删除操作状态已变化")
            terminal = _operation_row(conn, user_id, canonical)
            if terminal is None:  # pragma: no cover
                raise ApplicationDeleteOperationNotFound("岗位删除操作不存在")
            rejected = _dto(conn, user_id, terminal)
            if rejected["state"] != "rejected":
                raise ApplicationDeleteOperationConflict(
                    "岗位删除保留回执无法与冻结预览核对",
                )
    if stale:
        raise ApplicationDeleteOperationConflict("岗位删除预览已损坏，已安全终结")
    return rejected
