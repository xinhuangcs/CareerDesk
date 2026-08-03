"""Trusted whole-Review undo operations.

The preview freezes every Review-owned dependency. Approval either removes the
entire Review projection in one transaction or makes no business-data change.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import UUID, uuid4

from pydantic import ValidationError

from ....platform.database import (
    loads_json,
    now_iso,
    read_connection,
    squash_whitespace,
    transaction,
)
from ...applications import public as applications
from ...applications.public import ApplicationNextAction, timeline_entry_snapshot_fingerprint
from ...journal import public as journal
from ..ai_models import ReviewExtraction
from .record_models import ReviewRecordDerivation
from .undo_models import (
    MAX_CONTENT_PREVIEW_CHARS,
    MAX_UNDO_QUESTIONS,
    MAX_UNDO_STATUS_LOGS,
    MAX_UNDO_TIMELINE_ENTRIES,
    REVIEW_UNDO_CONTRACT_VERSION,
    ReviewOperationDTO,
    ReviewOperationError,
    ReviewUndoApplication,
    ReviewUndoApplicationProjection,
    ReviewUndoEffect,
    ReviewUndoProposal,
    ReviewUndoQuestion,
    ReviewUndoRemoved,
    ReviewUndoResult,
    ReviewUndoTarget,
    ReviewUndoTimelineEntry,
)

MAX_REVIEW_SOURCE_CHARS = 100_000
MAX_PERSISTED_JSON_CHARS = 200_000
MAX_DEPENDENCY_JSON_CHARS = 1_000_000
MAX_REVIEW_SELECTOR_OPTIONS = 100
MAX_PENDING_REVIEW_UNDOS = 100
UNKNOWN_POSITION = "未注明岗位"
SAFE_FALLBACK_TIMESTAMP = "1970-01-01T00:00:00+00:00"

_OPERATION_COLUMNS = (
    "id, operation_id, state, created_time, extraction_json, derivation_json, "
    "parent_journal_id, revision"
)


class ReviewOperationNotFound(LookupError):
    """The Review or Review-undo operation does not exist for this user."""


class ReviewOperationConflict(RuntimeError):
    """The frozen Review undo can no longer be executed safely."""


class _UnsafeDependency(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Bundle:
    target_state: str
    target_revision: int
    target_derivation: dict
    target: ReviewUndoTarget
    effect: ReviewUndoEffect
    fingerprint: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: object) -> str:
    encoded = _canonical_json(value)
    if len(encoded) > MAX_DEPENDENCY_JSON_CHARS:
        raise _UnsafeDependency("复盘撤销依赖超过安全预算")
    return sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_text(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise _UnsafeDependency(f"{label} 缺失、类型损坏或超过安全边界")
    return value


def _object_json(raw: object, label: str) -> dict:
    text = _bounded_text(raw, MAX_PERSISTED_JSON_CHARS, label)
    loaded = loads_json(text, None)
    if not isinstance(loaded, dict):
        raise _UnsafeDependency(f"{label} 不是 JSON object")
    return loaded


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    return value.strip()


def _next_action(
    stage: str | None,
    step: str | None,
    date: str | None,
    time: str | None,
    note: str | None,
) -> ApplicationNextAction | None:
    if all(value is None for value in (stage, step, date, time, note)):
        return None
    return ApplicationNextAction.model_validate({
        "stage": stage,
        "step": step,
        "date": date,
        "time": time,
        "note": note,
    })


def _apply_replayed_state(
    projection: dict,
    *,
    stage: str,
    step: str | None,
    entry_id: int,
) -> None:
    """Apply one absolute state command to a replay cursor."""
    before_stage = projection["stage"]
    state_changed = (stage, step) != (
        before_stage,
        projection["current_step"],
    )
    if stage == "pooled":
        projection["paused_from_stage"] = (
            before_stage
            if before_stage in {"backlog", "applied", "written_test", "interviewing", "offer"}
            else projection["paused_from_stage"]
        )
        projection["pause_reason"] = (
            projection["pause_reason"] if before_stage == "pooled" else None
        )
    else:
        projection["paused_from_stage"] = None
        projection["pause_reason"] = None
    projection["stage"] = stage
    projection["current_step"] = step
    if state_changed:
        projection["current_state_entry_id"] = entry_id
    if stage in {"withdrawn", "rejected"}:
        projection["next_action"] = None


def _apply_replayed_review(
    projection: dict,
    extraction: ReviewExtraction,
    *,
    entry_id: int,
) -> None:
    before_stage = projection["stage"]
    projected = extraction.projected_state
    if projected is not None:
        final_stage = projected.stage or projection["stage"]
        final_step = projected.current_step or projection["current_step"]
        _apply_replayed_state(
            projection,
            stage=final_stage,
            step=final_step,
            entry_id=entry_id,
        )
    if projection["stage"] in {"withdrawn", "rejected"}:
        projection["next_action"] = None
    elif extraction.clear_next_action:
        projection["next_action"] = None
    elif extraction.next_action is not None:
        projection["next_action"] = extraction.next_action.model_dump(mode="json")
    if extraction.channel is not None:
        projection["channel"] = extraction.channel
    history = extraction.history
    if (
        before_stage != "applied"
        and projection["stage"] == "applied"
        and projection["applied_date"] is None
        and history is not None
    ):
        projection["applied_date"] = history.date


def _history_tuple(extraction: ReviewExtraction) -> tuple:
    history = extraction.history
    if history is not None:
        return history.step, history.date, history.outcome, history.summary
    return (
        None,
        None,
        None,
        f"已安排下一步：{extraction.next_action.step}"
        if extraction.next_action is not None
        else "已清除下一步安排" if extraction.clear_next_action else None,
    )


def _replay_later_projection(
    conn: Connection,
    user_id: str,
    application_id: int,
    target_entry_id: int,
    *,
    original: dict,
    replacement: dict,
) -> tuple[dict, dict, list[list]]:
    """Replay later facts with and without the target Review.

    The original cursor proves that the remaining ledger still derives the live
    application projection.  The replacement cursor applies the same commands
    to the target's before-snapshot and is the projection frozen for undo.
    """
    rows = conn.execute(
        "SELECT entry.id, entry.step, entry.occurred_date, entry.outcome, entry.summary, "
        "entry.from_stage, entry.from_step, entry.to_stage, entry.to_step, entry.source, "
        "entry.journal_id, entry.created_time, source.kind, source.state, source.revision, "
        "source.extraction_json, source.derivation_json "
        "FROM timeline_entries entry LEFT JOIN journal source "
        "ON source.user_id = entry.user_id AND source.id = entry.journal_id "
        "WHERE entry.user_id = ? AND entry.application_id = ? AND entry.id > ? "
        "ORDER BY entry.id LIMIT ?",
        (user_id, application_id, target_entry_id, MAX_UNDO_TIMELINE_ENTRIES + 1),
    ).fetchall()
    if len(rows) > MAX_UNDO_TIMELINE_ENTRIES:
        raise _UnsafeDependency("复盘后续历程超过安全上限")
    dependencies: list[list] = []
    allowed_sources = {"manual", "agent", "review", "drag", "system"}
    for row in rows:
        if row[9] not in allowed_sources or _safe_timestamp(row[11]) is None:
            raise _UnsafeDependency("复盘后续历程来源或时间已损坏")
        if (row[5], row[6]) != (original["stage"], original["current_step"]):
            raise _UnsafeDependency("复盘后续历程无法从已冻结投影连续重放")
        if row[9] == "review":
            if row[10] is None or row[12:14] != ("review", "applied"):
                raise _UnsafeDependency("复盘后续历程缺少 applied Review 来源")
            extraction_raw = _object_json(row[15], "later review extraction_json")
            derivation_raw = _object_json(row[16], "later review derivation_json")
            try:
                later_extraction = ReviewExtraction.model_validate(extraction_raw)
                later_derivation = ReviewRecordDerivation.model_validate({
                    **derivation_raw,
                    "revision": row[14],
                })
            except ValidationError as error:
                raise _UnsafeDependency("复盘后续 Review 契约已损坏") from error
            if (
                later_derivation.application_id != application_id
                or later_derivation.timeline_entry_ids != [row[0]]
                or (row[5], row[6]) != (
                    later_derivation.application_before.stage,
                    later_derivation.application_before.current_step,
                )
                or (row[7], row[8]) != (
                    later_derivation.application_after.stage,
                    later_derivation.application_after.current_step,
                )
                or tuple(row[1:5]) != _history_tuple(later_extraction)
            ):
                raise _UnsafeDependency("复盘后续 Review 与历程派生快照不一致")
            _apply_replayed_review(original, later_extraction, entry_id=row[0])
            _apply_replayed_review(replacement, later_extraction, entry_id=row[0])
        else:
            if row[10] is not None and row[12] is None:
                raise _UnsafeDependency("复盘后续历程引用了不存在的日志")
            if (row[5], row[6]) != (row[7], row[8]):
                _apply_replayed_state(
                    original,
                    stage=row[7],
                    step=row[8],
                    entry_id=row[0],
                )
                _apply_replayed_state(
                    replacement,
                    stage=row[7],
                    step=row[8],
                    entry_id=row[0],
                )
        if (original["stage"], original["current_step"]) != (row[7], row[8]):
            raise _UnsafeDependency("复盘后续历程重放结果与冻结记录不一致")
        dependencies.append(list(row))
    return original, replacement, dependencies


def _load_bundle(conn: Connection, user_id: str, journal_id: int) -> _Bundle:
    source = conn.execute(
        "SELECT id, content, created_time, processed_time, extraction_json, "
        "derivation_json, state, revision FROM journal "
        "WHERE user_id = ? AND id = ? AND kind = 'review'",
        (user_id, journal_id),
    ).fetchone()
    if source is None:
        raise ReviewOperationNotFound("找不到这条复盘记录")
    content = _bounded_text(source[1], MAX_REVIEW_SOURCE_CHARS, "review content")
    created_time = _safe_timestamp(source[2])
    if created_time is None:
        raise _UnsafeDependency("review created_time 已损坏")
    extraction_raw = _object_json(source[4], "review extraction_json")
    derivation_raw = _object_json(source[5], "review derivation_json")
    try:
        extraction = ReviewExtraction.model_validate(extraction_raw)
        derivation = ReviewRecordDerivation.model_validate({
            **derivation_raw,
            "revision": source[7],
        })
    except ValidationError as error:
        raise _UnsafeDependency("复盘结构化记录不符合当前契约") from error

    entry_rows = conn.execute(
        "SELECT id, application_id, step, occurred_date, outcome, summary, from_stage, "
        "from_step, to_stage, to_step, source, created_time FROM timeline_entries "
        "WHERE user_id = ? AND journal_id = ? ORDER BY id LIMIT ?",
        (user_id, journal_id, MAX_UNDO_TIMELINE_ENTRIES + 1),
    ).fetchall()
    if len(entry_rows) != 1 or len(entry_rows) > MAX_UNDO_TIMELINE_ENTRIES:
        raise _UnsafeDependency("一条复盘必须精确对应一条历程")
    entry_row = entry_rows[0]
    if derivation.timeline_entry_ids != [entry_row[0]]:
        raise _UnsafeDependency("复盘派生的历程身份不一致")
    merged_application = derivation.application_id != entry_row[1]
    has_merge_lineage = (
        merged_application
        and applications.has_completed_application_merge_lineage_in_transaction(
            conn,
            user_id,
            derivation.application_id,
            entry_row[1],
        )
    )
    if merged_application and not has_merge_lineage:
        raise _UnsafeDependency("复盘岗位身份缺少可验证的合并链路")
    if entry_row[10] != "review":
        raise _UnsafeDependency("复盘派生的历程来源不一致")
    entry = ReviewUndoTimelineEntry(
        id=entry_row[0],
        step=entry_row[2],
        occurred_date=entry_row[3],
        outcome=entry_row[4],
        summary=entry_row[5],
        from_stage=entry_row[6],
        from_step=entry_row[7],
        to_stage=entry_row[8],
        to_step=entry_row[9],
    )

    application_row = conn.execute(
        "SELECT id, company, position, created_time, stage, current_step, "
        "current_state_entry_id, next_stage, next_step, next_date, next_time, next_note, "
        "paused_from_stage, pause_reason, channel, applied_date, revision "
        "FROM applications WHERE user_id = ? AND id = ?",
        (user_id, entry_row[1]),
    ).fetchone()
    if application_row is None:
        raise _UnsafeDependency("复盘对应的岗位已不存在")
    # Extraction company/position are historical prose.  Stable row ids plus
    # the frozen application created_time (or a verified merge receipt chain)
    # own the relationship, so a normal profile rename remains reversible.
    if (
        (entry_row[6], entry_row[7])
        != (
            derivation.application_before.stage,
            derivation.application_before.current_step,
        )
        or (entry_row[8], entry_row[9])
        != (
            derivation.application_after.stage,
            derivation.application_after.current_step,
        )
    ):
        raise _UnsafeDependency("复盘历程跳转与岗位派生快照不一致")
    if tuple(entry_row[2:6]) != _history_tuple(extraction):
        raise _UnsafeDependency("复盘历程与结构化事实不一致")
    expected = ReviewUndoApplicationProjection(
        stage=application_row[4],
        current_step=application_row[5],
        current_state_entry_id=application_row[6],
        next_action=_next_action(*application_row[7:12]),
        paused_from_stage=application_row[12],
        pause_reason=application_row[13],
        channel=application_row[14],
        applied_date=application_row[15],
        revision=application_row[16],
    )
    later_dependencies: list[list] = []
    if merged_application:
        # Merge deliberately makes the destination projection authoritative.
        # Removing one rebound source Review must retain that projection.
        replacement_values = expected.model_dump(mode="json")
        replacement_values["revision"] = expected.revision + 1
        replacement = ReviewUndoApplicationProjection.model_validate(replacement_values)
        record_retained = True
    else:
        original_values = derivation.application_after.model_dump(mode="json")
        replacement_values = derivation.application_before.model_dump(mode="json")
        original_values, replacement_values, later_dependencies = _replay_later_projection(
            conn,
            user_id,
            application_row[0],
            entry_row[0],
            original=original_values,
            replacement=replacement_values,
        )
        original_values["revision"] = expected.revision
        derived_live = ReviewUndoApplicationProjection.model_validate(original_values)
        if expected != derived_live:
            raise _UnsafeDependency("岗位当前投影无法由剩余历程安全重放")
        record_retained = not derivation.application_created or bool(later_dependencies)
        if record_retained:
            replacement_values["revision"] = expected.revision + 1
            replacement = ReviewUndoApplicationProjection.model_validate(replacement_values)
        else:
            replacement = None

    if derivation.application_created and not record_retained:
        foreign_entry = conn.execute(
            "SELECT 1 FROM timeline_entries WHERE user_id = ? AND application_id = ? "
            "AND journal_id IS NOT ? LIMIT 1",
            (user_id, application_row[0], journal_id),
        ).fetchone()
        if foreign_entry is not None:
            raise _UnsafeDependency("复盘创建的岗位已有后续历程，不能整条撤销")
        extras = conn.execute(
            "SELECT department, jd_text, application_note, priority, resume_id, prep_status "
            "FROM applications WHERE user_id = ? AND id = ?",
            (user_id, application_row[0]),
        ).fetchone()
        if extras is None or any((
            extras[0] is not None,
            extras[1] is not None,
            extras[2] is not None,
            extras[3] is not None,
            extras[4] is not None,
            extras[5] != "none",
        )):
            raise _UnsafeDependency("复盘创建的岗位已有独立资料或优先级，不能整条删除")
        dependent = conn.execute(
            "SELECT 1 FROM questions WHERE user_id = ? AND application_id = ? "
            "AND journal_id IS NOT ? UNION ALL "
            "SELECT 1 FROM review_question_occurrences WHERE user_id = ? "
            "AND application_id = ? AND journal_id != ? UNION ALL "
            "SELECT 1 FROM resumes WHERE user_id = ? AND application_id = ? LIMIT 1",
            (
                user_id, application_row[0], journal_id,
                user_id, application_row[0], journal_id,
                user_id, application_row[0],
            ),
        ).fetchone()
        if dependent is not None:
            raise _UnsafeDependency("复盘创建的岗位已有其它资料绑定，不能整条删除")

    occurrence_rows = conn.execute(
        "SELECT question_id, application_id, company, source_step, asked_date "
        "FROM review_question_occurrences WHERE user_id = ? AND journal_id = ? "
        "ORDER BY question_id LIMIT ?",
        (user_id, journal_id, MAX_UNDO_QUESTIONS + 1),
    ).fetchall()
    if len(occurrence_rows) > MAX_UNDO_QUESTIONS:
        raise _UnsafeDependency("复盘题目出处超过安全上限")
    question_rows = conn.execute(
        "SELECT DISTINCT question.id, question.text, question.status, question.source "
        "FROM review_question_occurrences occurrence JOIN questions question "
        "ON question.user_id = occurrence.user_id AND question.id = occurrence.question_id "
        "WHERE occurrence.user_id = ? AND occurrence.journal_id = ? ORDER BY question.id",
        (user_id, journal_id),
    ).fetchall()
    occurrence_question_ids = {row[0] for row in occurrence_rows}
    if occurrence_question_ids != {row[0] for row in question_rows}:
        raise _UnsafeDependency("复盘题目出处缺少同租户 question 绑定")
    if any(
        row[1] != application_row[0] or row[2] != application_row[1]
        for row in occurrence_rows
    ):
        raise _UnsafeDependency("复盘题目出处与岗位身份不一致")
    questions_archived: list[ReviewUndoQuestion] = []
    for question_id, text, status, question_source in question_rows:
        if status != "active" or question_source != "real":
            continue
        other = conn.execute(
            "SELECT 1 FROM review_question_occurrences occurrence JOIN journal source "
            "ON source.user_id = occurrence.user_id AND source.id = occurrence.journal_id "
            "WHERE occurrence.user_id = ? AND occurrence.question_id = ? "
            "AND occurrence.journal_id != ? AND source.kind = 'review' "
            "AND source.state = 'applied' LIMIT 1",
            (user_id, question_id, journal_id),
        ).fetchone()
        if other is None:
            questions_archived.append(ReviewUndoQuestion(id=question_id, text=text))
    if len(questions_archived) > MAX_UNDO_QUESTIONS:
        raise _UnsafeDependency("复盘将归档的题目超过安全上限")

    status_rows = conn.execute(
        "SELECT id, log_date, time_of_day, mood, factors_json, created_time "
        "FROM status_log WHERE user_id = ? AND journal_id = ? ORDER BY id LIMIT ?",
        (user_id, journal_id, MAX_UNDO_STATUS_LOGS + 1),
    ).fetchall()
    if len(status_rows) > MAX_UNDO_STATUS_LOGS:
        raise _UnsafeDependency("复盘状态日志超过安全上限")
    if any(row[1] != entry_row[3] for row in status_rows):
        raise _UnsafeDependency("复盘状态日志日期与历程不一致")

    target = ReviewUndoTarget(
        journal_id=source[0],
        expected_revision=source[7],
        company=application_row[1],
        position=application_row[2],
        content_preview=content[:MAX_CONTENT_PREVIEW_CHARS],
        content_truncated=len(content) > MAX_CONTENT_PREVIEW_CHARS,
        review_created_time=created_time,
    )
    effect = ReviewUndoEffect(
        timeline_entries=[entry],
        status_logs_removed=len(status_rows),
        questions_archived=questions_archived,
        application=ReviewUndoApplication(
            id=application_row[0],
            company=application_row[1],
            position=application_row[2],
            record_exists=True,
            record_retained=record_retained,
            expected=expected,
            replacement=replacement,
        ),
    )
    dependency = {
        "journal": list(source),
        "extraction": extraction.model_dump(mode="json"),
        "derivation": derivation.model_dump(mode="json", exclude={"revision"}),
        "timeline_entry": list(entry_row),
        "application": list(application_row),
        "occurrences": [list(row) for row in occurrence_rows],
        "questions": [list(row) for row in question_rows],
        "status_logs": [list(row) for row in status_rows],
        "later_entries": later_dependencies,
    }
    return _Bundle(
        target_state=source[6],
        target_revision=source[7],
        target_derivation=derivation_raw,
        target=target,
        effect=effect,
        fingerprint=_fingerprint(dependency),
    )


def _canonical_operation_id(operation_id: str | UUID) -> str:
    try:
        return str(UUID(str(operation_id)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ReviewOperationNotFound("复盘撤销操作不存在") from error


def _command_hash(action: str, operation_id: str) -> str:
    return _fingerprint({"action": action, "operation_id": operation_id, "version": 1})


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
        "AND kind = 'correction' AND operation_id = ? "
        "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.operation.type') = 'review_undo'",
        (user_id, operation_id),
    ).fetchone()


def _proposal(raw: object) -> ReviewUndoProposal:
    return ReviewUndoProposal.model_validate(_object_json(raw, "review undo proposal"))


def _operation_payload(raw: object) -> dict:
    try:
        loaded = _object_json(raw, "review undo receipt")
    except _UnsafeDependency:
        return {}
    operation = loaded.get("operation")
    return operation if isinstance(operation, dict) else {}


def _bundle_matches_proposal(
    conn: Connection,
    user_id: str,
    proposal: ReviewUndoProposal,
    *,
    parent_journal_id: int | None,
) -> bool:
    try:
        bundle = _load_bundle(conn, user_id, proposal.target.journal_id)
    except (ReviewOperationNotFound, _UnsafeDependency, ValidationError, ValueError):
        return False
    return (
        parent_journal_id == proposal.target.journal_id
        and bundle.target_state == "applied"
        and bundle.target_revision == proposal.target.expected_revision
        and bundle.fingerprint == proposal.dependency_fingerprint
        and bundle.target == proposal.target
        and bundle.effect == proposal.effect
    )


def _fallback_target(parent_id: int | None, created_time: str) -> ReviewUndoTarget:
    return ReviewUndoTarget(
        journal_id=parent_id if isinstance(parent_id, int) and parent_id > 0 else 1,
        expected_revision=0,
        company="未知公司",
        position=UNKNOWN_POSITION,
        content_preview="",
        content_truncated=False,
        review_created_time=created_time,
    )


def _fallback_effect() -> ReviewUndoEffect:
    return ReviewUndoEffect(
        timeline_entries=[],
        status_logs_removed=0,
        questions_archived=[],
        application=ReviewUndoApplication(
            company="未知公司",
            position=UNKNOWN_POSITION,
            record_exists=False,
            record_retained=False,
        ),
    )


def _dto(conn: Connection, user_id: str, row) -> dict:
    (_journal_id, operation_id, journal_state, created_time, extraction_json,
     derivation_json, parent_id, _revision) = row
    safe_created_time = _safe_timestamp(created_time) or SAFE_FALLBACK_TIMESTAMP
    try:
        proposal = _proposal(extraction_json)
    except (TypeError, ValueError, ValidationError):
        proposal = None
    operation = _operation_payload(derivation_json)
    result = None
    if journal_state == "awaiting_user" and proposal is not None:
        state = (
            "pending"
            if _bundle_matches_proposal(
                conn, user_id, proposal, parent_journal_id=parent_id,
            )
            else "stale"
        )
    elif (
        journal_state == "applied"
        and proposal is not None
        and set(operation) == {"type", "action", "command_hash", "result"}
        and operation.get("type") == "review_undo"
        and operation.get("action") == "approve"
        and operation.get("command_hash") == _command_hash("approve", operation_id)
    ):
        try:
            result = ReviewUndoResult.model_validate(operation.get("result"))
        except (TypeError, ValueError, ValidationError):
            state = "stale"
        else:
            state = "completed"
    elif (
        journal_state == "voided"
        and proposal is not None
        and set(operation) == {"type", "action", "command_hash"}
        and operation.get("type") == "review_undo"
        and operation.get("action") == "reject"
        and operation.get("command_hash") == _command_hash("reject", operation_id)
    ):
        state = "rejected"
    else:
        state = "stale"
    if _safe_timestamp(created_time) is None:
        state, result = "stale", None
    dto = ReviewOperationDTO(
        operation_id=operation_id,
        state=state,
        created_time=safe_created_time,
        target=proposal.target if proposal is not None else _fallback_target(parent_id, safe_created_time),
        effect=proposal.effect if proposal is not None else _fallback_effect(),
        result=result,
    )
    return dto.model_dump(mode="json")


def _locate_target(
    conn: Connection,
    user_id: str,
    *,
    company: str | None,
    position: str | None,
    journal_id: int | None,
) -> int | dict:
    if journal_id is not None:
        if isinstance(journal_id, bool) or not isinstance(journal_id, int) or journal_id < 1:
            return {"status": "not_found"}
        row = conn.execute(
            "SELECT id FROM journal WHERE user_id = ? AND id = ? "
            "AND kind = 'review' AND state = 'applied'",
            (user_id, journal_id),
        ).fetchone()
        return row[0] if row is not None else {"status": "not_found"}

    if company is None and position is None:
        row = conn.execute(
            "SELECT id FROM journal WHERE user_id = ? AND kind = 'review' "
            "AND state = 'applied' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row[0] if row is not None else {"status": "not_found"}

    conditions = [
        "source.user_id = ?",
        "source.kind = 'review'",
        "source.state = 'applied'",
    ]
    parameters: list[object] = [user_id]
    if company is not None:
        conditions.append(
            "(squash_whitespace(json_extract(CASE WHEN json_valid(source.extraction_json) "
            "THEN source.extraction_json ELSE '{}' END, '$.company')) = "
            "squash_whitespace(?) OR squash_whitespace(application.company) = "
            "squash_whitespace(?) OR NOT json_valid(source.extraction_json) OR "
            "trim(COALESCE(application.company, '')) = '')"
        )
        parameters.extend((company, company))
    if position is not None:
        conditions.append(
            "(squash_whitespace(json_extract(CASE WHEN json_valid(source.extraction_json) "
            "THEN source.extraction_json ELSE '{}' END, '$.position')) = "
            "squash_whitespace(?) OR squash_whitespace(application.position) = "
            "squash_whitespace(?) OR NOT json_valid(source.extraction_json) OR "
            "trim(COALESCE(application.position, '')) = '')"
        )
        parameters.extend((position, position))
    rows = conn.execute(
        "SELECT source.id, "
        "json_extract(CASE WHEN json_valid(source.extraction_json) "
        "THEN source.extraction_json ELSE '{}' END, '$.company'), "
        "json_extract(CASE WHEN json_valid(source.extraction_json) "
        "THEN source.extraction_json ELSE '{}' END, '$.position'), "
        "application.company, application.position FROM journal source "
        "LEFT JOIN timeline_entries entry ON entry.user_id = source.user_id "
        "AND entry.journal_id = source.id LEFT JOIN applications application "
        "ON application.user_id = entry.user_id AND application.id = entry.application_id "
        "WHERE " + " AND ".join(conditions) + " ORDER BY source.id DESC LIMIT ?",
        (*parameters, (MAX_REVIEW_SELECTOR_OPTIONS + 1) * 4),
    ).fetchall()
    if not rows:
        return {"status": "not_found"}

    raw_identities = {
        (
            squash_whitespace(
                row[1] if isinstance(row[1], str) and row[1].strip() else (row[3] or ""),
            ),
            squash_whitespace(
                row[2] if isinstance(row[2], str) and row[2].strip() else (row[4] or ""),
            ),
        )
        for row in rows
    }
    if len(raw_identities) > MAX_REVIEW_SELECTOR_OPTIONS:
        raise ReviewOperationConflict("匹配的复盘过多，请同时提供准确的公司和岗位")

    bundles: list[_Bundle] = []
    seen_sources: set[int] = set()
    seen_identities: set[tuple[str, str]] = set()
    for row in rows:
        if row[0] in seen_sources:
            continue
        seen_sources.add(row[0])
        raw_company = row[1] if isinstance(row[1], str) and row[1].strip() else row[3]
        raw_position = row[2] if isinstance(row[2], str) and row[2].strip() else row[4]
        identity = (
            squash_whitespace(raw_company or ""),
            squash_whitespace(raw_position or ""),
        )
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        try:
            bundles.append(_load_bundle(conn, user_id, row[0]))
        except (ReviewOperationNotFound, _UnsafeDependency, ValidationError) as error:
            raise ReviewOperationConflict(str(error)) from error
        if company is not None and position is not None:
            break
        if len(bundles) > MAX_REVIEW_SELECTOR_OPTIONS:
            raise ReviewOperationConflict("匹配的复盘过多，请同时提供准确的公司和岗位")
    if not bundles:
        return {"status": "not_found"}
    if len(bundles) > 1:
        return {
            "status": "ambiguous",
            "options": sorted(
                f"{bundle.target.company}·{bundle.target.position}"
                for bundle in bundles
            ),
        }
    return bundles[0].target.journal_id


def _mark_stale(
    conn: Connection,
    user_id: str,
    journal_id: int,
    expected_revision: int,
    code: str,
) -> None:
    error = ReviewOperationError(
        code=code,
        message="复盘或其派生记录已变化，请重新生成撤销预览。",
    )
    receipt = {
        "operation": {
            "type": "review_undo",
            "action": "stale",
            "error": error.model_dump(mode="json"),
        },
    }
    changed = conn.execute(
        "UPDATE journal SET state = 'superseded', derivation_json = ?, revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND state = 'awaiting_user' AND revision = ?",
        (_canonical_json(receipt), user_id, journal_id, expected_revision),
    ).rowcount
    if changed != 1:
        raise ReviewOperationConflict("复盘撤销操作状态已变化")


def _prepare_review_undo_operation(
    db_path: str,
    user_id: str,
    *,
    company: str | None = None,
    position: str | None = None,
    journal_id: int | None = None,
    expected_revision: int | None = None,
    timeline_entry_selector: tuple[int, int, str] | None = None,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> dict:
    company = company.strip() if isinstance(company, str) else company
    position = position.strip() if isinstance(position, str) else position
    company = company or None
    position = position or None
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision 必须是非负整数")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if timeline_entry_selector is not None:
            application_id, timeline_entry_id, expected_fingerprint = timeline_entry_selector
            row = conn.execute(
                "SELECT entry.created_time, entry.step, entry.occurred_date, entry.outcome, "
                "entry.summary, entry.from_stage, entry.from_step, entry.to_stage, entry.to_step, "
                "entry.source, source.id, source.revision FROM timeline_entries entry "
                "JOIN journal source ON source.user_id = entry.user_id "
                "AND source.id = entry.journal_id WHERE entry.user_id = ? "
                "AND entry.application_id = ? AND entry.id = ? AND source.kind = 'review'",
                (user_id, application_id, timeline_entry_id),
            ).fetchone()
            if row is None:
                raise ReviewOperationNotFound("找不到这条复盘历程")
            fingerprint = timeline_entry_snapshot_fingerprint(
                created_time=row[0], step=row[1], occurred_date=row[2], outcome=row[3],
                summary=row[4], from_stage=row[5], from_step=row[6], to_stage=row[7],
                to_step=row[8], source=row[9],
            )
            if fingerprint != expected_fingerprint:
                raise ReviewOperationConflict("这条历程已被另一窗口修改，请刷新后重试")
            journal_id, expected_revision = row[10], row[11]

        located = _locate_target(
            conn, user_id, company=company, position=position, journal_id=journal_id,
        )
        if isinstance(located, dict):
            return located
        target_id = located
        if expected_revision is not None:
            target_row = conn.execute(
                "SELECT state, revision FROM journal WHERE user_id = ? AND id = ? "
                "AND kind = 'review'",
                (user_id, target_id),
            ).fetchone()
            if target_row != ("applied", expected_revision):
                raise ReviewOperationConflict("这条复盘已被修正或撤销，请刷新后重试")

        existing = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND parent_journal_id = ? AND state = 'awaiting_user' "
            "AND json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json "
            "ELSE '{}' END, '$.operation_type') = 'review_undo' ORDER BY id DESC LIMIT 1",
            (user_id, target_id),
        ).fetchone()
        if existing is not None:
            dto = _dto(conn, user_id, existing)
            if dto["state"] == "pending":
                if proposal_recorder is not None:
                    proposal_recorder(conn, "review_undo", dto["operation_id"])
                return dto
            _mark_stale(conn, user_id, existing[0], existing[7], "dependency_drifted")

        try:
            bundle = _load_bundle(conn, user_id, target_id)
        except (_UnsafeDependency, ValidationError) as error:
            raise ReviewOperationConflict(str(error)) from error
        if bundle.target_state != "applied":
            raise ReviewOperationConflict("这条复盘已不再是可撤销状态")
        proposal = ReviewUndoProposal(
            operation_type="review_undo",
            contract_version=REVIEW_UNDO_CONTRACT_VERSION,
            dependency_fingerprint=bundle.fingerprint,
            target=bundle.target,
            effect=bundle.effect,
        )
        operation_id = str(uuid4())
        created_time = now_iso()
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, parent_journal_id, operation_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, 'awaiting_user', ?, ?)",
            (
                user_id,
                f"[待确认撤销复盘 #{target_id}]",
                created_time,
                _canonical_json(proposal.model_dump(mode="json")),
                _canonical_json({"operation": {"type": "review_undo"}}),
                target_id,
                operation_id,
            ),
        )
        operation_row = _operation_row(conn, user_id, operation_id)
        if operation_row is None:  # pragma: no cover
            raise RuntimeError("review undo operation insert lost")
        dto = _dto(conn, user_id, operation_row)
        if proposal_recorder is not None:
            proposal_recorder(conn, "review_undo", operation_id)
        return dto


def prepare_review_undo_operation(
    db_path: str,
    user_id: str,
    *,
    company: str | None = None,
    position: str | None = None,
    journal_id: int | None = None,
    expected_revision: int | None = None,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> dict:
    """Freeze a whole-Review undo by exact receipt or by company/position."""
    return _prepare_review_undo_operation(
        db_path,
        user_id,
        company=company,
        position=position,
        journal_id=journal_id,
        expected_revision=expected_revision,
        proposal_recorder=proposal_recorder,
    )


def prepare_review_timeline_entry_undo_operation(
    db_path: str,
    user_id: str,
    application_id: int,
    timeline_entry_id: int,
    *,
    expected_fingerprint: str,
) -> dict:
    """Freeze whole-Review undo from one exact Review-owned timeline entry."""
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (application_id, timeline_entry_id)
    ):
        raise ReviewOperationNotFound("找不到这条复盘历程")
    if (
        not isinstance(expected_fingerprint, str)
        or len(expected_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in expected_fingerprint)
    ):
        raise ValueError("expected_fingerprint 必须是 64 位小写十六进制摘要")
    return _prepare_review_undo_operation(
        db_path,
        user_id,
        timeline_entry_selector=(application_id, timeline_entry_id, expected_fingerprint),
    )


def list_pending_review_operations(db_path: str, user_id: str) -> list[dict]:
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND state = 'awaiting_user' "
            "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'review_undo' "
            "ORDER BY created_time DESC, id DESC LIMIT ?",
            (user_id, MAX_PENDING_REVIEW_UNDOS + 1),
        ).fetchall()
        if len(rows) > MAX_PENDING_REVIEW_UNDOS:
            raise ReviewOperationConflict("待确认复盘撤销超过安全读取上限")
        return [dto for row in rows if (dto := _dto(conn, user_id, row))["state"] == "pending"]


def get_review_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict | None:
    canonical = _canonical_operation_id(operation_id)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _operation_row(conn, user_id, canonical)
        return _dto(conn, user_id, row) if row is not None else None


def _projection_where(projection: ReviewUndoApplicationProjection) -> tuple[str, tuple]:
    next_action = projection.next_action
    values = (
        projection.stage,
        projection.current_step,
        projection.current_state_entry_id,
        next_action.stage if next_action else None,
        next_action.step if next_action else None,
        next_action.date if next_action else None,
        next_action.time if next_action else None,
        next_action.note if next_action else None,
        projection.paused_from_stage,
        projection.pause_reason,
        projection.channel,
        projection.applied_date,
        projection.revision,
    )
    clause = (
        "stage = ? AND current_step IS ? AND current_state_entry_id IS ? "
        "AND next_stage IS ? AND next_step IS ? AND next_date IS ? AND next_time IS ? "
        "AND next_note IS ? AND paused_from_stage IS ? AND pause_reason IS ? "
        "AND channel IS ? AND applied_date IS ? AND revision = ?"
    )
    return clause, values


def _execute_undo(
    conn: Connection,
    user_id: str,
    operation_id: str,
    bundle: _Bundle,
) -> ReviewUndoResult:
    effect = bundle.effect
    application = effect.application
    if application.id is None or application.expected is None:
        raise ReviewOperationConflict("复盘岗位快照已损坏")
    target_revision = journal.void_review_in_transaction(
        conn,
        user_id,
        bundle.target.journal_id,
        expected_revision=bundle.target_revision,
        derivation={**bundle.target_derivation, "undo_operation_id": operation_id},
    )
    if target_revision is None:
        raise ReviewOperationConflict("复盘状态已变化")

    if not application.record_retained:
        try:
            deleted = applications.remove_review_created_application_in_transaction(
                conn,
                user_id,
                application.id,
                source_journal_id=bundle.target.journal_id,
            )
        except RuntimeError as error:
            raise ReviewOperationConflict(str(error)) from error
        entries_removed = deleted.get("timeline_entries_removed", 0)
        application_id = None
        application_stage = None
    else:
        replacement = application.replacement
        if replacement is None:
            raise ReviewOperationConflict("复盘岗位回退快照已损坏")
        where_clause, where_values = _projection_where(application.expected)
        next_action = replacement.next_action
        changed = conn.execute(
            "UPDATE applications SET stage = ?, current_step = ?, current_state_entry_id = ?, "
            "next_stage = ?, next_step = ?, next_date = ?, next_time = ?, next_note = ?, "
            "paused_from_stage = ?, pause_reason = ?, channel = ?, applied_date = ?, "
            "revision = revision + 1, updated_time = ? WHERE user_id = ? AND id = ? AND "
            + where_clause,
            (
                replacement.stage,
                replacement.current_step,
                replacement.current_state_entry_id,
                next_action.stage if next_action else None,
                next_action.step if next_action else None,
                next_action.date if next_action else None,
                next_action.time if next_action else None,
                next_action.note if next_action else None,
                replacement.paused_from_stage,
                replacement.pause_reason,
                replacement.channel,
                replacement.applied_date,
                now_iso(),
                user_id,
                application.id,
                *where_values,
            ),
        ).rowcount
        if changed != 1:
            raise ReviewOperationConflict("岗位投影已变化")
        entries_removed = conn.execute(
            "DELETE FROM timeline_entries WHERE user_id = ? AND journal_id = ?",
            (user_id, bundle.target.journal_id),
        ).rowcount
        application_id = application.id
        application_stage = replacement.stage

    status_removed = conn.execute(
        "DELETE FROM status_log WHERE user_id = ? AND journal_id = ?",
        (user_id, bundle.target.journal_id),
    ).rowcount
    archived = 0
    for question in effect.questions_archived:
        active_review = conn.execute(
            "SELECT 1 FROM review_question_occurrences occurrence JOIN journal source "
            "ON source.user_id = occurrence.user_id AND source.id = occurrence.journal_id "
            "WHERE occurrence.user_id = ? AND occurrence.question_id = ? "
            "AND source.kind = 'review' AND source.state = 'applied' LIMIT 1",
            (user_id, question.id),
        ).fetchone()
        if active_review is None:
            archived += conn.execute(
                "UPDATE questions SET status = 'archived', updated_time = ? "
                "WHERE user_id = ? AND id = ? AND source = 'real' AND status = 'active'",
                (now_iso(), user_id, question.id),
            ).rowcount

    result = ReviewUndoResult(
        status="ok",
        target_revision=target_revision,
        application_id=application_id,
        application_stage=application_stage,
        removed=ReviewUndoRemoved(
            timeline_entries=entries_removed,
            status_logs=status_removed,
            questions_archived=archived,
        ),
    )
    expected_removed = ReviewUndoRemoved(
        timeline_entries=len(effect.timeline_entries),
        status_logs=effect.status_logs_removed,
        questions_archived=len(effect.questions_archived),
    )
    if result.removed != expected_removed or target_revision != bundle.target_revision + 1:
        raise ReviewOperationConflict("复盘撤销的实际影响与冻结预览不一致")
    return result


def approve_review_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict:
    canonical = _canonical_operation_id(operation_id)
    command_hash = _command_hash("approve", canonical)
    conflict_reason: str | None = None
    completed: dict | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _operation_row(conn, user_id, canonical)
        if row is None:
            raise ReviewOperationNotFound("复盘撤销操作不存在")
        operation = _operation_payload(row[5])
        if row[2] != "awaiting_user":
            current = _dto(conn, user_id, row)
            if current["state"] == "completed" and operation.get("command_hash") == command_hash:
                return current
            raise ReviewOperationConflict(f"该操作当前为 {current['state']}，不能批准")
        try:
            if _safe_timestamp(row[3]) is None:
                raise _UnsafeDependency("复盘撤销操作时间戳已损坏")
            proposal = _proposal(row[4])
            bundle = _load_bundle(conn, user_id, proposal.target.journal_id)
        except (ReviewOperationNotFound, _UnsafeDependency, ValidationError, ValueError):
            conflict_reason = "contract_invalid"
            _mark_stale(conn, user_id, row[0], row[7], conflict_reason)
        else:
            matches = (
                row[6] == proposal.target.journal_id
                and bundle.target_state == "applied"
                and bundle.target_revision == proposal.target.expected_revision
                and bundle.fingerprint == proposal.dependency_fingerprint
                and bundle.target == proposal.target
                and bundle.effect == proposal.effect
            )
            if not matches:
                conflict_reason = "dependency_drifted"
                _mark_stale(conn, user_id, row[0], row[7], conflict_reason)
            else:
                result = _execute_undo(conn, user_id, canonical, bundle)
                receipt = {
                    "operation": {
                        "type": "review_undo",
                        "action": "approve",
                        "command_hash": command_hash,
                        "result": result.model_dump(mode="json"),
                    },
                }
                changed = conn.execute(
                    "UPDATE journal SET state = 'applied', processed_time = ?, "
                    "derivation_json = ?, revision = revision + 1 WHERE user_id = ? "
                    "AND id = ? AND kind = 'correction' AND state = 'awaiting_user' "
                    "AND revision = ?",
                    (now_iso(), _canonical_json(receipt), user_id, row[0], row[7]),
                ).rowcount
                if changed != 1:
                    raise ReviewOperationConflict("复盘撤销操作状态已变化")
                terminal = _operation_row(conn, user_id, canonical)
                if terminal is None:  # pragma: no cover
                    raise ReviewOperationNotFound("复盘撤销操作不存在")
                completed = _dto(conn, user_id, terminal)
    if conflict_reason is not None:
        raise ReviewOperationConflict("复盘撤销预览已失效，请重新生成")
    if completed is None:  # pragma: no cover
        raise ReviewOperationConflict("复盘撤销操作未完成")
    return completed


def reject_review_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict:
    canonical = _canonical_operation_id(operation_id)
    command_hash = _command_hash("reject", canonical)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _operation_row(conn, user_id, canonical)
        if row is None:
            raise ReviewOperationNotFound("复盘撤销操作不存在")
        operation = _operation_payload(row[5])
        if row[2] != "awaiting_user":
            current = _dto(conn, user_id, row)
            if current["state"] == "rejected" and operation.get("command_hash") == command_hash:
                return current
            raise ReviewOperationConflict(f"该操作当前为 {current['state']}，不能拒绝")
        try:
            if _safe_timestamp(row[3]) is None:
                raise _UnsafeDependency("复盘撤销操作时间戳已损坏")
            _proposal(row[4])
        except (TypeError, ValueError, ValidationError):
            _mark_stale(conn, user_id, row[0], row[7], "contract_invalid")
            stale = True
        else:
            stale = False
            receipt = {
                "operation": {
                    "type": "review_undo",
                    "action": "reject",
                    "command_hash": command_hash,
                },
            }
            changed = conn.execute(
                "UPDATE journal SET state = 'voided', derivation_json = ?, "
                "revision = revision + 1 WHERE user_id = ? AND id = ? "
                "AND kind = 'correction' AND state = 'awaiting_user' AND revision = ?",
                (_canonical_json(receipt), user_id, row[0], row[7]),
            ).rowcount
            if changed != 1:
                raise ReviewOperationConflict("复盘撤销操作状态已变化")
            terminal = _operation_row(conn, user_id, canonical)
            if terminal is None:  # pragma: no cover
                raise ReviewOperationNotFound("复盘撤销操作不存在")
            rejected = _dto(conn, user_id, terminal)
    if stale:
        raise ReviewOperationConflict("复盘撤销预览已损坏，已安全终结")
    return rejected
