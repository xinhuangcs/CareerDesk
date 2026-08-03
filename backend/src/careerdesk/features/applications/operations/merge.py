"""Trusted application merge operations with frozen dependencies and receipts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from sqlite3 import Connection
from uuid import UUID, uuid4

from pydantic import ValidationError

from ....platform.database import now_iso, read_connection, transaction
from .. import repository
from . import delete as delete_operation
from .merge_models import (
    APPLICATION_MERGE_CONTRACT_VERSION,
    MAX_MERGE_OCCURRENCES,
    ApplicationMergeApplication,
    ApplicationMergeCounts,
    ApplicationMergeEffect,
    ApplicationMergeFieldResolution,
    ApplicationMergeFinalDestination,
    ApplicationMergeOccurrence,
    ApplicationMergeOperationDTO,
    ApplicationMergeOperationError,
    ApplicationMergeProposal,
    ApplicationMergeQuestion,
    ApplicationMergeResult,
    ApplicationMergeResume,
    ApplicationMergeResumeRef,
    ApplicationMergeTimelineEntry,
)
from .models import (
    MAX_DELETE_JD_PREVIEW_CHARS,
    MAX_DELETE_QUESTIONS,
    MAX_DELETE_QUESTION_PREVIEW_CHARS,
    MAX_DELETE_RESUMES,
    MAX_DELETE_TIMELINE_ENTRIES,
)

MAX_APPLICATION_SOURCE_CHARS = 500_000
MAX_PERSISTED_JSON_CHARS = 1_500_000
MAX_DEPENDENCY_JSON_CHARS = 2_000_000
MAX_PENDING_MERGE_OPERATIONS = 100
SAFE_FALLBACK_TIMESTAMP = "1970-01-01T00:00:00+00:00"

_OPERATION_COLUMNS = (
    "id, operation_id, state, created_time, extraction_json, derivation_json, revision"
)
_APPLICATION_FIELDS = (
    "id", "user_id", "company", "company_id", "position", "department", "channel",
    "jd_text", "jd_parsed_json", "stage", "current_step", "current_state_entry_id",
    "next_stage", "next_step", "next_date", "next_time", "next_note",
    "paused_from_stage", "pause_reason", "application_note", "priority", "resume_id",
    "applied_date", "prep_status", "prep_generation", "prep_heartbeat_time", "prep_json",
    "revision", "created_time", "updated_time",
)
_APPLICATION_COLUMNS = ", ".join(_APPLICATION_FIELDS)


class ApplicationMergeOperationNotFound(LookupError):
    """Merge operation is absent or belongs to another user."""


class ApplicationMergeOperationConflict(RuntimeError):
    """Terminal operation, dependency drift, or corrupt durable contract."""


class _UnsafeMergeDependency(ValueError):
    """Application or related data violates safe-merge invariants."""


@dataclass(frozen=True, slots=True)
class _MergeBundle:
    source: ApplicationMergeApplication
    destination: ApplicationMergeApplication
    effect: ApplicationMergeEffect
    fingerprint: str
    final_projection: dict


def _bounded_text(value, limit: int, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise _UnsafeMergeDependency(f"{label} 超过安全边界或类型损坏")
    return value


def _canonical_fingerprint(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_DEPENDENCY_JSON_CHARS:
        raise _UnsafeMergeDependency("岗位合并完整依赖超过安全预算")
    return sha256(encoded.encode("utf-8")).hexdigest()


def _json_object(raw: str | None, label: str) -> dict:
    if raw is None:
        return {}
    _bounded_text(raw, MAX_APPLICATION_SOURCE_CHARS, label)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise _UnsafeMergeDependency(f"{label} 不是有效 JSON object") from error
    if not isinstance(value, dict):
        raise _UnsafeMergeDependency(f"{label} 不是 JSON object")
    return value


def _raw_application(conn: Connection, user_id: str, application_id: int) -> dict:
    row = conn.execute(
        f"SELECT {_APPLICATION_COLUMNS} FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if row is None:
        raise ApplicationMergeOperationNotFound("找不到合并中的岗位记录")
    return dict(zip(_APPLICATION_FIELDS, tuple(row), strict=True))


def _selected_resume_ref(conn: Connection, user_id: str,
                         application: dict) -> tuple[ApplicationMergeResumeRef | None, list | None]:
    resume_id = application["resume_id"]
    if resume_id is None:
        return None, None
    row = conn.execute(
        "SELECT id, user_id, name, binding, application_id, archived, updated_time "
        "FROM resumes WHERE id = ?",
        (resume_id,),
    ).fetchone()
    if row is None or row[1] != user_id:
        raise _UnsafeMergeDependency("岗位选用简历缺少同租户绑定")
    if row[5] not in {0, 1}:
        raise _UnsafeMergeDependency("岗位选用简历 archived 类型损坏")
    if row[3] == "family":
        if row[4] is not None:
            raise _UnsafeMergeDependency("通用简历错误绑定了岗位")
    elif row[3] == "application":
        if row[4] != application["id"]:
            raise _UnsafeMergeDependency("岗位专属选用简历属于其它岗位")
    else:
        raise _UnsafeMergeDependency("岗位选用简历 binding 类型损坏")
    return ApplicationMergeResumeRef(
        id=row[0],
        name=row[2],
        binding=row[3],
        application_id=row[4],
        archived=row[5] == 1,
    ), list(row)


def _validate_cross_table_ownership(conn: Connection, user_id: str,
                                    application_ids: tuple[int, int]) -> dict:
    placeholders = ", ".join("?" for _ in application_ids)
    params = (*application_ids, user_id)
    for table in (
        "timeline_entries", "questions", "review_question_occurrences",
        "resumes",
    ):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE application_id IN ({placeholders}) "
            "AND user_id != ? LIMIT 1",
            params,
        ).fetchone()
        if row is not None:
            raise _UnsafeMergeDependency(f"{table} 存在跨租户岗位引用")

    journal_links = conn.execute(
        "SELECT 'timeline_entry', entry.id, entry.journal_id, source.id, source.user_id "
        "FROM timeline_entries entry LEFT JOIN journal source ON source.id = entry.journal_id "
        f"WHERE entry.application_id IN ({placeholders}) AND entry.journal_id IS NOT NULL "
        "UNION ALL "
        "SELECT 'question', question.id, question.journal_id, source.id, source.user_id "
        "FROM questions question LEFT JOIN journal source ON source.id = question.journal_id "
        f"WHERE question.application_id IN ({placeholders}) AND question.journal_id IS NOT NULL",
        (*application_ids, *application_ids),
    ).fetchall()
    for _kind, _row_id, journal_id, linked_id, linked_user_id in journal_links:
        if linked_id != journal_id or linked_user_id != user_id:
            raise _UnsafeMergeDependency("岗位关联历程或题目缺少同租户 journal")

    bound_resume_rows = conn.execute(
        "SELECT id, user_id, application_id, binding FROM resumes "
        f"WHERE application_id IN ({placeholders}) ORDER BY id",
        application_ids,
    ).fetchall()
    if any(row[1] != user_id or row[3] != "application" for row in bound_resume_rows):
        raise _UnsafeMergeDependency("岗位绑定简历存在跨租户或 binding 不一致")
    _ensure_no_indirect_cross_tenant_reference(conn, user_id, application_ids[0])
    return {
        "journal_links": [list(row) for row in journal_links],
        "bound_resume_bindings": [list(row) for row in bound_resume_rows],
    }


def _ensure_no_indirect_cross_tenant_reference(conn: Connection, user_id: str,
                                               source_id: int) -> None:
    """Validate reverse-reference tenant closure, not only direct application edges."""
    moved_resume_cross_ref = conn.execute(
        "SELECT 1 FROM resumes moved "
        "JOIN applications application ON application.resume_id = moved.id "
        "WHERE moved.user_id = ? AND moved.application_id = ? "
        "AND (application.user_id != ? OR application.id != ?) "
        "LIMIT 1",
        (user_id, source_id, user_id, source_id),
    ).fetchone()
    if moved_resume_cross_ref is not None:
        raise _UnsafeMergeDependency("待改挂简历存在跨租户反向引用")

    moved_question_occurrence_cross_ref = conn.execute(
        "SELECT 1 FROM questions moved "
        "JOIN review_question_occurrences occurrence ON occurrence.question_id = moved.id "
        "LEFT JOIN journal source ON source.id = occurrence.journal_id "
        "LEFT JOIN applications owner ON owner.id = occurrence.application_id "
        "WHERE moved.user_id = ? AND moved.application_id = ? "
        "AND (occurrence.user_id != ? OR source.id IS NULL OR source.user_id != ? "
        "OR (occurrence.application_id IS NOT NULL "
        "AND (owner.id IS NULL OR owner.user_id != occurrence.user_id))) LIMIT 1",
        (user_id, source_id, user_id, user_id),
    ).fetchone()
    if moved_question_occurrence_cross_ref is not None:
        raise _UnsafeMergeDependency("待改挂题目存在跨租户复盘出处引用")

    moved_question_knowledge_cross_ref = conn.execute(
        "SELECT 1 FROM questions moved "
        "JOIN question_knowledge link ON link.question_id = moved.id "
        "LEFT JOIN knowledge_points knowledge ON knowledge.id = link.knowledge_point_id "
        "WHERE moved.user_id = ? AND moved.application_id = ? "
        "AND (knowledge.id IS NULL OR knowledge.user_id != ?) LIMIT 1",
        (user_id, source_id, user_id),
    ).fetchone()
    if moved_question_knowledge_cross_ref is not None:
        raise _UnsafeMergeDependency("待改挂题目存在跨租户知识点引用")


def _application_snapshot(raw: dict, delete_bundle,
                          selected_resume: ApplicationMergeResumeRef | None) -> ApplicationMergeApplication:
    target = delete_bundle.target
    return ApplicationMergeApplication(
        application_id=target.application_id,
        company=target.company,
        position=target.position,
        department=target.department,
        channel=target.channel,
        stage=target.stage,
        current_step=target.current_step,
        priority=target.priority,
        selected_resume=selected_resume,
        applied_date=target.applied_date,
        next_action=target.next_action,
        paused_from_stage=target.paused_from_stage,
        pause_reason=target.pause_reason,
        application_note=target.application_note,
        jd_preview=target.jd_preview,
        jd_truncated=target.jd_truncated,
        skills=target.skills,
        highlights=target.highlights,
        prep_status=target.prep_status,
        prep_artifact_present=target.prep_artifact_present,
        revision=raw["revision"],
        application_created_time=raw["created_time"],
        application_updated_time=raw["updated_time"],
    )


def _summary(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _resume_summary(value: ApplicationMergeResumeRef | None) -> str | None:
    if value is None:
        return None
    scope = "岗位专属" if value.binding == "application" else "岗位族通用"
    return f"#{value.id} · {value.name} · {scope} · {'已归档' if value.archived else '未归档'}"


def _next_action_summary(action) -> str | None:
    if action is None:
        return None
    return (
        f"阶段：{action.stage}；环节：{action.step}；"
        f"日期：{action.date or '未填写'}；时间：{action.time or '未填写'}；"
        f"说明：{action.note or '未填写'}"
    )


def _pause_summary(stage: str | None, reason: str | None) -> str | None:
    if stage is None and reason is None:
        return None
    return f"暂停前阶段：{stage or '未填写'}；原因：{reason or '未填写'}"


def _jd_summary(snapshot: ApplicationMergeApplication) -> str | None:
    if not (snapshot.jd_preview or snapshot.skills or snapshot.highlights
            or snapshot.jd_truncated):
        return None
    suffix = "（预览已截断）" if snapshot.jd_truncated else ""
    parts = [snapshot.jd_preview + suffix] if snapshot.jd_preview else []
    if snapshot.skills:
        parts.append("技能：" + "、".join(snapshot.skills))
    if snapshot.highlights:
        parts.append("亮点：" + "、".join(snapshot.highlights))
    value = "\n".join(parts)
    return value[:MAX_DELETE_JD_PREVIEW_CHARS]


def _resolution(field: str, strategy: str, source_value, destination_value,
                final_value, *, carried: bool) -> ApplicationMergeFieldResolution:
    return ApplicationMergeFieldResolution(
        field=field,
        strategy=strategy,
        source_value=_summary(source_value),
        destination_value=_summary(destination_value),
        final_value=_summary(final_value),
        source_value_carried_forward=carried,
    )


def _counts(effect) -> ApplicationMergeCounts:
    return ApplicationMergeCounts(
        timeline_entries=len(effect.timeline_entries),
        questions=len(effect.questions_detached),
        question_occurrences=effect.question_occurrences_detached,
        resumes=len(effect.resumes_detached),
    )


def _load_merge_bundle(conn: Connection, user_id: str, source_id: int,
                       destination_id: int) -> _MergeBundle:
    if source_id == destination_id:
        raise _UnsafeMergeDependency("合并源岗位与保留岗位不能是同一条记录")
    try:
        source_delete = delete_operation._load_bundle(conn, user_id, source_id)
        destination_delete = delete_operation._load_bundle(conn, user_id, destination_id)
    except delete_operation.ApplicationDeleteOperationNotFound as error:
        raise ApplicationMergeOperationNotFound("找不到合并中的岗位记录") from error
    except delete_operation._UnsafeDependency as error:
        raise _UnsafeMergeDependency(str(error)) from error

    source_raw = _raw_application(conn, user_id, source_id)
    destination_raw = _raw_application(conn, user_id, destination_id)
    source_resume, source_resume_dependency = _selected_resume_ref(
        conn, user_id, source_raw,
    )
    destination_resume, destination_resume_dependency = _selected_resume_ref(
        conn, user_id, destination_raw,
    )
    ownership = _validate_cross_table_ownership(
        conn, user_id, (source_id, destination_id),
    )
    source = _application_snapshot(source_raw, source_delete, source_resume)
    destination = _application_snapshot(
        destination_raw, destination_delete, destination_resume,
    )

    source_parsed = _json_object(source_raw["jd_parsed_json"], "source JD parsed")
    destination_parsed = _json_object(
        destination_raw["jd_parsed_json"], "destination JD parsed",
    )
    destination_has_jd = (
        destination_raw["jd_text"] is not None
        or destination_raw["jd_parsed_json"] is not None
    )
    source_has_jd = (
        source_raw["jd_text"] is not None or source_raw["jd_parsed_json"] is not None
    )
    if destination_has_jd:
        jd_source = "destination"
        final_jd_text = destination_raw["jd_text"]
        final_jd_parsed_json = destination_raw["jd_parsed_json"]
        final_parsed = destination_parsed
        final_jd_public = destination
    elif source_has_jd:
        jd_source = "source"
        final_jd_text = source_raw["jd_text"]
        final_jd_parsed_json = source_raw["jd_parsed_json"]
        final_parsed = source_parsed
        final_jd_public = source
    else:
        jd_source = "none"
        final_jd_text = None
        final_jd_parsed_json = None
        final_parsed = {}
        final_jd_public = None
    final_skills = final_parsed.get("skills", [])
    final_highlights = final_parsed.get("highlights", [])

    if destination.next_action is not None:
        final_next_action = destination.next_action
        next_strategy = "destination_preferred"
    elif destination.stage not in {"rejected", "withdrawn"}:
        final_next_action = source.next_action
        next_strategy = "source_fallback"
    else:
        final_next_action = None
        next_strategy = "cleared_for_safety"

    chosen_resume = destination_resume or source_resume
    final_resume = chosen_resume
    if (final_resume is not None and final_resume.binding == "application"
            and final_resume.application_id == source_id):
        final_resume = final_resume.model_copy(update={"application_id": destination_id})

    final_department = destination_raw["department"] or source_raw["department"]
    final_channel = destination_raw["channel"] or source_raw["channel"]
    final_applied_date = destination_raw["applied_date"] or source_raw["applied_date"]
    final_application_note = (
        destination.application_note
        if destination.application_note is not None
        else source.application_note
    )
    final_paused_from_stage = (
        destination.paused_from_stage if destination.stage == "pooled" else None
    )
    final_pause_reason = destination.pause_reason if destination.stage == "pooled" else None
    priority_rank = {None: 0, "low": 1, "medium": 2, "high": 3}
    final_priority = max(
        (destination_raw["priority"], source_raw["priority"]),
        key=lambda value: priority_rank[value],
    )
    final_destination = ApplicationMergeFinalDestination(
        application_id=destination_id,
        company=destination.company,
        position=destination.position,
        department=final_department,
        channel=final_channel,
        stage=destination.stage,
        current_step=destination.current_step,
        priority=final_priority,
        selected_resume=final_resume,
        applied_date=final_applied_date,
        next_action=final_next_action,
        paused_from_stage=final_paused_from_stage,
        pause_reason=final_pause_reason,
        application_note=final_application_note,
        jd_source=jd_source,
        jd_preview=(final_jd_public.jd_preview if final_jd_public is not None else ""),
        jd_truncated=(final_jd_public.jd_truncated if final_jd_public is not None else False),
        skills=final_skills,
        highlights=final_highlights,
        prep_status="none",
        prep_artifact_present=False,
    )

    source_next = _next_action_summary(source.next_action)
    destination_next = _next_action_summary(destination.next_action)
    final_next = _next_action_summary(final_next_action)
    source_jd = _jd_summary(source)
    destination_jd = _jd_summary(destination)
    final_jd = _jd_summary(final_destination)  # type: ignore[arg-type]
    source_complete_jd_carried = (
        not source_has_jd
        or (
            source_raw["jd_text"] == final_jd_text
            and json.dumps(
                source_parsed,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) == json.dumps(
                final_parsed,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    )
    resolutions = [
        _resolution("company", "destination_identity", source.company, destination.company,
                    destination.company, carried=source.company == destination.company),
        _resolution("position", "destination_identity", source.position, destination.position,
                    destination.position, carried=source.position == destination.position),
        _resolution(
            "department",
            "destination_preferred" if destination.department is not None else "source_fallback",
            source.department,
            destination.department,
            final_department,
            carried=source.department is None or source.department == final_department,
        ),
        _resolution(
            "channel",
            "destination_preferred" if destination.channel is not None else "source_fallback",
            source.channel,
            destination.channel,
            final_channel,
            carried=source.channel is None or source.channel == final_channel,
        ),
        _resolution("stage", "destination_preferred", source.stage, destination.stage,
                    destination.stage, carried=source.stage == destination.stage),
        _resolution(
            "current_step",
            "destination_preferred",
            source.current_step,
            destination.current_step,
            destination.current_step,
            carried=source.current_step is None or source.current_step == destination.current_step,
        ),
        _resolution("priority", "highest_priority", source.priority, destination.priority,
                    final_priority, carried=source.priority in {None, final_priority}),
        _resolution(
            "selected_resume",
            "destination_preferred" if destination_resume is not None else "source_fallback",
            _resume_summary(source_resume),
            _resume_summary(destination_resume),
            _resume_summary(final_resume),
            carried=source_resume is None or (
                final_resume is not None and source_resume.id == final_resume.id
            ),
        ),
        _resolution(
            "applied_date",
            "destination_preferred" if destination.applied_date is not None else "source_fallback",
            source.applied_date,
            destination.applied_date,
            final_applied_date,
            carried=source.applied_date is None or source.applied_date == final_applied_date,
        ),
        _resolution(
            "next_action",
            next_strategy,
            source_next,
            destination_next,
            final_next,
            carried=source_next is None or source_next == final_next,
        ),
        _resolution(
            "pause",
            "destination_preferred" if destination.stage == "pooled" else "cleared_for_safety",
            _pause_summary(source.paused_from_stage, source.pause_reason),
            _pause_summary(destination.paused_from_stage, destination.pause_reason),
            _pause_summary(final_paused_from_stage, final_pause_reason),
            carried=(
                source.paused_from_stage is None and source.pause_reason is None
            ) or (
                source.paused_from_stage == final_paused_from_stage
                and source.pause_reason == final_pause_reason
            ),
        ),
        _resolution(
            "application_note",
            (
                "destination_preferred"
                if destination.application_note is not None
                else "source_fallback"
            ),
            source.application_note,
            destination.application_note,
            final_application_note,
            carried=(
                source.application_note is None
                or source.application_note == final_application_note
            ),
        ),
        _resolution(
            "jd",
            "destination_preferred" if jd_source == "destination" else "source_fallback",
            source_jd,
            destination_jd,
            final_jd,
            carried=source_complete_jd_carried,
        ),
        _resolution(
            "prep",
            "cleared_for_safety",
            f"{source.prep_status}；{'有产物' if source.prep_artifact_present else '无产物'}",
            f"{destination.prep_status}；{'有产物' if destination.prep_artifact_present else '无产物'}",
            "none；无产物",
            carried=not source.prep_artifact_present and source.prep_status == "none",
        ),
    ]

    source_timeline_rows = conn.execute(
        "SELECT id, step, occurred_date, outcome, summary, from_stage, from_step, "
        "to_stage, to_step, source, journal_id, created_time FROM timeline_entries "
        "WHERE user_id = ? AND application_id = ? ORDER BY id LIMIT ?",
        (user_id, source_id, MAX_DELETE_TIMELINE_ENTRIES + 1),
    ).fetchall()
    source_question_rows = conn.execute(
        "SELECT id, text, source, company, journal_id FROM questions "
        "WHERE user_id = ? AND application_id = ? ORDER BY id LIMIT ?",
        (user_id, source_id, MAX_DELETE_QUESTIONS + 1),
    ).fetchall()
    source_occurrence_rows = conn.execute(
        "SELECT journal_id, question_id, company FROM review_question_occurrences "
        "WHERE user_id = ? AND application_id = ? "
        "ORDER BY journal_id, question_id LIMIT ?",
        (user_id, source_id, MAX_MERGE_OCCURRENCES + 1),
    ).fetchall()
    source_resume_rows = conn.execute(
        "SELECT id, name, binding, archived FROM resumes "
        "WHERE user_id = ? AND application_id = ? ORDER BY id LIMIT ?",
        (user_id, source_id, MAX_DELETE_RESUMES + 1),
    ).fetchall()
    if (len(source_timeline_rows) > MAX_DELETE_TIMELINE_ENTRIES
            or len(source_question_rows) > MAX_DELETE_QUESTIONS
            or len(source_occurrence_rows) > MAX_MERGE_OCCURRENCES
            or len(source_resume_rows) > MAX_DELETE_RESUMES):
        raise _UnsafeMergeDependency("岗位合并影响超过安全行数上限")

    effect = ApplicationMergeEffect(
        final_destination=final_destination,
        field_resolutions=resolutions,
        timeline_entries_rebound=[ApplicationMergeTimelineEntry(
            id=row[0],
            step=row[1],
            occurred_date=row[2],
            outcome=row[3],
            summary=row[4],
            from_stage=row[5],
            from_step=row[6],
            to_stage=row[7],
            to_step=row[8],
            source=row[9],
            journal_id=row[10],
            created_time=row[11],
        ) for row in source_timeline_rows],
        questions_rebound=[ApplicationMergeQuestion(
            id=row[0],
            text_preview=row[1][:MAX_DELETE_QUESTION_PREVIEW_CHARS],
            text_truncated=len(row[1]) > MAX_DELETE_QUESTION_PREVIEW_CHARS,
            source=row[2],
            company_before=row[3],
            company_after=destination.company,
            journal_id=row[4],
        ) for row in source_question_rows],
        question_occurrences_rebound=[ApplicationMergeOccurrence(
            journal_id=row[0],
            question_id=row[1],
            company_before=row[2],
            company_after=destination.company,
        ) for row in source_occurrence_rows],
        resumes_rebound=[ApplicationMergeResume(
            id=row[0], name=row[1], binding=row[2], archived=row[3] == 1,
        ) for row in source_resume_rows],
        destination_existing=_counts(destination_delete.effect),
        source_application_removed=True,
        destination_prep_reset=True,
        source_prep_removed_with_application=True,
        company_records_untouched=True,
        journal_records_untouched=True,
        external_logs_untouched=True,
    )
    final_projection = {
        "department": final_department,
        "channel": final_channel,
        "jd_text": final_jd_text,
        "jd_parsed_json": final_jd_parsed_json,
        "stage": destination.stage,
        "current_step": destination.current_step,
        "current_state_entry_id": destination_raw["current_state_entry_id"],
        "priority": final_priority,
        "resume_id": final_resume.id if final_resume is not None else None,
        "applied_date": final_applied_date,
        "next_stage": final_next_action.stage if final_next_action is not None else None,
        "next_step": final_next_action.step if final_next_action is not None else None,
        "next_date": final_next_action.date if final_next_action is not None else None,
        "next_time": final_next_action.time if final_next_action is not None else None,
        "next_note": final_next_action.note if final_next_action is not None else None,
        "paused_from_stage": final_paused_from_stage,
        "pause_reason": final_pause_reason,
        "application_note": final_application_note,
    }
    fingerprint = _canonical_fingerprint({
        "source": source_delete.fingerprint,
        "destination": destination_delete.fingerprint,
        "source_selected_resume": source_resume_dependency,
        "destination_selected_resume": destination_resume_dependency,
        "ownership": ownership,
    })
    return _MergeBundle(
        source=source,
        destination=destination,
        effect=effect,
        fingerprint=fingerprint,
        final_projection=final_projection,
    )


def _canonical_operation_id(operation_id: str | UUID) -> str:
    try:
        return str(UUID(str(operation_id)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ApplicationMergeOperationNotFound("岗位合并操作不存在") from error


def _proposal_digest(proposal: ApplicationMergeProposal) -> str:
    encoded = json.dumps(
        proposal.model_dump(), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    _bounded_text(encoded, MAX_PERSISTED_JSON_CHARS, "application merge proposal digest input")
    return sha256(encoded.encode("utf-8")).hexdigest()


def _command_hash(action: str, operation_id: str, proposal_digest: str) -> str:
    encoded = json.dumps({
        "action": action,
        "operation_id": operation_id,
        "proposal_digest": proposal_digest,
        "version": 1,
    }, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? AND operation_id = ? "
        "AND kind = 'correction' "
        "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.operation.type') = 'application_merge'",
        (user_id, operation_id),
    ).fetchone()


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


def _proposal(raw: str | None) -> ApplicationMergeProposal:
    if not isinstance(raw, str) or len(raw) > MAX_PERSISTED_JSON_CHARS:
        raise ValueError("invalid merge proposal")
    loaded = json.loads(raw)
    return ApplicationMergeProposal.model_validate(loaded)


def _safe_timestamp(value) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    value = value.strip()
    return value or None


def _fallback_application(application_id: int, created_time: str) -> ApplicationMergeApplication:
    return ApplicationMergeApplication(
        application_id=application_id,
        company="未知岗位",
        position="损坏的合并预览",
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
        revision=0,
        application_created_time=created_time,
        application_updated_time=created_time,
    )


def _fallback_effect(destination_id: int) -> ApplicationMergeEffect:
    final = ApplicationMergeFinalDestination(
        application_id=destination_id,
        company="未知岗位",
        position="损坏的合并预览",
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
        jd_source="none",
        jd_preview="",
        jd_truncated=False,
        skills=[],
        highlights=[],
        prep_status="none",
        prep_artifact_present=False,
    )
    fields = (
        "company", "position", "department", "channel", "stage", "current_step",
        "priority", "selected_resume", "applied_date", "next_action", "pause",
        "application_note", "jd", "prep",
    )
    return ApplicationMergeEffect(
        final_destination=final,
        field_resolutions=[ApplicationMergeFieldResolution(
            field=field,
            strategy="cleared_for_safety" if field == "prep" else "destination_preferred",
            source_value=None,
            destination_value=None,
            final_value=None,
            source_value_carried_forward=False,
        ) for field in fields],
        timeline_entries_rebound=[],
        questions_rebound=[],
        question_occurrences_rebound=[],
        resumes_rebound=[],
        destination_existing=ApplicationMergeCounts(
            timeline_entries=0,
            questions=0,
            question_occurrences=0,
            resumes=0,
        ),
        source_application_removed=True,
        destination_prep_reset=True,
        source_prep_removed_with_application=True,
        company_records_untouched=True,
        journal_records_untouched=True,
        external_logs_untouched=True,
    )


def _moved_counts(effect: ApplicationMergeEffect) -> ApplicationMergeCounts:
    return ApplicationMergeCounts(
        timeline_entries=len(effect.timeline_entries_rebound),
        questions=len(effect.questions_rebound),
        question_occurrences=len(effect.question_occurrences_rebound),
        resumes=len(effect.resumes_rebound),
    )


def _total_counts(effect: ApplicationMergeEffect) -> ApplicationMergeCounts:
    moved = _moved_counts(effect)
    existing = effect.destination_existing
    return ApplicationMergeCounts(
        timeline_entries=existing.timeline_entries + moved.timeline_entries,
        questions=existing.questions + moved.questions,
        question_occurrences=(
            existing.question_occurrences + moved.question_occurrences
        ),
        resumes=existing.resumes + moved.resumes,
    )


def _result_matches_proposal(result: ApplicationMergeResult,
                             proposal: ApplicationMergeProposal) -> bool:
    return (
        result.source_application_id == proposal.source.application_id
        and result.destination_application_id == proposal.destination.application_id
        and result.source_deleted
        and result.moved == _moved_counts(proposal.effect)
        and result.destination_totals == _total_counts(proposal.effect)
        and result.destination_prep_reset
        and result.final_destination == proposal.effect.final_destination
    )


def _dto(conn: Connection, user_id: str, row) -> dict:
    journal_id, operation_id, journal_state, created_time, extraction_json, derivation_json, _ = row
    del journal_id
    safe_created_time = _safe_timestamp(created_time)
    operation = _operation_payload(derivation_json)
    proposal = None
    proposal_digest = None
    try:
        proposal = _proposal(extraction_json)
        proposal_digest = _proposal_digest(proposal)
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        pass
    state = "stale"
    result = None
    if (journal_state == "awaiting_user" and proposal is not None
            and proposal_digest is not None
            and operation == {"type": "application_merge", "proposal_digest": proposal_digest}):
        try:
            bundle = _load_merge_bundle(
                conn, user_id, proposal.source.application_id,
                proposal.destination.application_id,
            )
        except (
            ApplicationMergeOperationNotFound,
            _UnsafeMergeDependency,
            ValidationError,
            ValueError,
        ):
            bundle = None
        if (bundle is not None
                and bundle.fingerprint == proposal.dependency_fingerprint
                and bundle.source == proposal.source
                and bundle.destination == proposal.destination
                and bundle.effect == proposal.effect):
            state = "pending"
    elif (journal_state == "applied" and proposal is not None
          and proposal_digest is not None
          and set(operation) == {
              "type", "proposal_digest", "action", "command_hash", "result",
          }
          and operation.get("type") == "application_merge"
          and operation.get("proposal_digest") == proposal_digest
          and operation.get("action") == "approve"):
        try:
            parsed_result = ApplicationMergeResult.model_validate(operation.get("result"))
        except (TypeError, ValueError):
            state = "stale"
        else:
            if (operation.get("command_hash") == _command_hash(
                    "approve", operation_id, proposal_digest,
                ) and _result_matches_proposal(parsed_result, proposal)):
                result = parsed_result.model_dump()
                state = "completed"
    elif (journal_state == "voided" and proposal is not None
          and proposal_digest is not None
          and set(operation) == {"type", "proposal_digest", "action", "command_hash"}
          and operation.get("type") == "application_merge"
          and operation.get("proposal_digest") == proposal_digest
          and operation.get("action") == "reject"
          and operation.get("command_hash") == _command_hash(
              "reject", operation_id, proposal_digest,
          )):
        state = "rejected"
    if safe_created_time is None:
        state = "stale"
        result = None
    source = proposal.source if proposal is not None else _fallback_application(
        1, safe_created_time or SAFE_FALLBACK_TIMESTAMP,
    )
    destination = proposal.destination if proposal is not None else _fallback_application(
        2, safe_created_time or SAFE_FALLBACK_TIMESTAMP,
    )
    effect = proposal.effect if proposal is not None else _fallback_effect(2)
    return ApplicationMergeOperationDTO(
        operation_id=operation_id,
        operation_type="application_merge",
        state=state,
        created_time=safe_created_time or SAFE_FALLBACK_TIMESTAMP,
        source=source,
        destination=destination,
        effect=effect,
        result=result,
    ).model_dump()


def _mark_stale(conn: Connection, user_id: str, journal_id: int,
                expected_revision: int, code: str) -> None:
    error = ApplicationMergeOperationError(
        code=code,
        message="合并中的岗位或关联数据已变化，请重新生成预览。",
    )
    changed = conn.execute(
        "UPDATE journal SET state = 'superseded', processed_time = NULL, "
        "derivation_json = ?, revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND state = 'awaiting_user' AND revision = ?",
        (json.dumps({"operation": {
            "type": "application_merge",
            "action": "stale",
            "error": error.model_dump(),
        }}, ensure_ascii=False), user_id, journal_id, expected_revision),
    ).rowcount
    if changed != 1:
        raise ApplicationMergeOperationConflict("岗位合并操作状态已变化")


def _validate_selector(value, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"岗位合并需要准确的{label}")
    return value.strip()


def _locate_pair(conn: Connection, user_id: str, *, source_application_id: int,
                 source_company: str, source_position: str,
                 destination_application_id: int, destination_company: str,
                 destination_position: str) -> tuple[int, int] | dict:
    source = conn.execute(
        "SELECT id FROM applications WHERE user_id = ? AND id = ? "
        "AND company = ? AND position = ?",
        (user_id, source_application_id, source_company, source_position),
    ).fetchone()
    destination = conn.execute(
        "SELECT id FROM applications WHERE user_id = ? AND id = ? "
        "AND company = ? AND position = ?",
        (user_id, destination_application_id, destination_company, destination_position),
    ).fetchone()
    if source is None or destination is None:
        return {"status": "not_found"}
    if source[0] == destination[0]:
        return {"status": "same_application"}
    return source[0], destination[0]


def prepare_application_merge_operation(
    db_path: str,
    user_id: str,
    *,
    source_application_id: int,
    source_company: str,
    source_position: str,
    destination_application_id: int,
    destination_company: str,
    destination_position: str,
    proposal_recorder: Callable[[Connection, str, str], object] | None = None,
) -> dict:
    """Atomically resolve a directed pair and freeze a reusable live preview."""
    if (
        isinstance(source_application_id, bool)
        or not isinstance(source_application_id, int)
        or source_application_id < 1
        or isinstance(destination_application_id, bool)
        or not isinstance(destination_application_id, int)
        or destination_application_id < 1
    ):
        raise ValueError("岗位合并需要稳定的源/目标记录 ID")
    source_company = _validate_selector(source_company, "源公司名", 200)
    source_position = _validate_selector(source_position, "源岗位名", 300)
    destination_company = _validate_selector(destination_company, "目标公司名", 200)
    destination_position = _validate_selector(destination_position, "目标岗位名", 300)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        located = _locate_pair(
            conn,
            user_id,
            source_application_id=source_application_id,
            source_company=source_company,
            source_position=source_position,
            destination_application_id=destination_application_id,
            destination_company=destination_company,
            destination_position=destination_position,
        )
        if isinstance(located, dict):
            return located
        source_id, destination_id = located

        overlap_rows = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND operation_id IS NOT NULL "
            "AND state = 'awaiting_user' "
            "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'application_merge' "
            "AND (CAST(json_extract(CASE WHEN json_valid(extraction_json) "
            "THEN extraction_json ELSE '{}' END, '$.source.application_id') AS INTEGER) "
            "IN (?, ?) OR CAST(json_extract(CASE WHEN json_valid(extraction_json) "
            "THEN extraction_json ELSE '{}' END, '$.destination.application_id') AS INTEGER) "
            "IN (?, ?)) ORDER BY id LIMIT ?",
            (
                user_id, source_id, destination_id, source_id, destination_id,
                MAX_PENDING_MERGE_OPERATIONS + 1,
            ),
        ).fetchall()
        for existing in overlap_rows:
            dto = _dto(conn, user_id, existing)
            if dto["state"] == "pending":
                if (dto["source"]["application_id"] == source_id
                        and dto["destination"]["application_id"] == destination_id):
                    if proposal_recorder is not None:
                        proposal_recorder(
                            conn,
                            "application_merge",
                            dto["operation_id"],
                        )
                    return dto
                raise ApplicationMergeOperationConflict(
                    "其中一条岗位已有其它待确认合并，请先处理原确认卡",
                )
            _mark_stale(conn, user_id, existing[0], existing[6], "dependency_drifted")

        pending_rows = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND operation_id IS NOT NULL "
            "AND state = 'awaiting_user' "
            "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'application_merge' "
            "ORDER BY id LIMIT ?",
            (user_id, MAX_PENDING_MERGE_OPERATIONS + 1),
        ).fetchall()
        live_count = 0
        for pending_row in pending_rows:
            dto = _dto(conn, user_id, pending_row)
            if dto["state"] == "pending":
                live_count += 1
            else:
                _mark_stale(
                    conn, user_id, pending_row[0], pending_row[6], "dependency_drifted",
                )
        if live_count >= MAX_PENDING_MERGE_OPERATIONS:
            raise ApplicationMergeOperationConflict("待确认岗位合并操作已达安全上限")
        try:
            bundle = _load_merge_bundle(conn, user_id, source_id, destination_id)
        except _UnsafeMergeDependency as error:
            raise ApplicationMergeOperationConflict(str(error)) from error
        except ValidationError as error:
            raise ApplicationMergeOperationConflict(
                "岗位或其关联记录不符合安全合并契约",
            ) from error
        proposal = ApplicationMergeProposal(
            operation_type="application_merge",
            contract_version=APPLICATION_MERGE_CONTRACT_VERSION,
            dependency_fingerprint=bundle.fingerprint,
            source=bundle.source,
            destination=bundle.destination,
            effect=bundle.effect,
        )
        proposal_json = json.dumps(proposal.model_dump(), ensure_ascii=False)
        _bounded_text(proposal_json, MAX_PERSISTED_JSON_CHARS, "application merge proposal")
        proposal_digest = _proposal_digest(proposal)
        operation_id = str(uuid4())
        created_time = now_iso()
        cursor = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, 'awaiting_user', ?)",
            (
                user_id,
                f"[待确认合并岗位 #{source_id} → #{destination_id}]",
                created_time,
                proposal_json,
                json.dumps({"operation": {
                    "type": "application_merge",
                    "proposal_digest": proposal_digest,
                }}, ensure_ascii=False),
                operation_id,
            ),
        )
        row = _operation_row(conn, user_id, operation_id)
        if row is None:  # pragma: no cover
            raise RuntimeError(f"application merge operation insert lost: {cursor.lastrowid}")
        pending = _dto(conn, user_id, row)
        if pending["state"] != "pending":  # pragma: no cover
            raise RuntimeError("application merge proposal receipt is not pending")
        if proposal_recorder is not None:
            proposal_recorder(
                conn,
                "application_merge",
                pending["operation_id"],
            )
        return pending


def list_pending_application_merge_operations(db_path: str, user_id: str) -> list[dict]:
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND operation_id IS NOT NULL "
            "AND state = 'awaiting_user' "
            "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'application_merge' "
            "ORDER BY created_time DESC, id DESC LIMIT ?",
            (user_id, MAX_PENDING_MERGE_OPERATIONS + 1),
        ).fetchall()
        operations = [_dto(conn, user_id, row) for row in rows]
    live = [operation for operation in operations if operation["state"] == "pending"]
    if len(live) > MAX_PENDING_MERGE_OPERATIONS:
        raise ApplicationMergeOperationConflict("待确认岗位合并操作超过安全上限")
    return live


def get_application_merge_operation(db_path: str, user_id: str,
                                    operation_id: str | UUID) -> dict | None:
    canonical = _canonical_operation_id(operation_id)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _operation_row(conn, user_id, canonical)
        return _dto(conn, user_id, row) if row is not None else None


def has_completed_application_merge_lineage_in_transaction(
    conn: Connection,
    user_id: str,
    source_application_id: int,
    destination_application_id: int,
) -> bool:
    """Return whether strict completed merge receipts connect source to destination."""
    if not conn.in_transaction or source_application_id == destination_application_id:
        return False
    rows = conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
        "AND kind = 'correction' AND state = 'applied' "
        "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.operation.type') = 'application_merge' "
        "ORDER BY id LIMIT ?",
        (user_id, MAX_PENDING_MERGE_OPERATIONS + 1),
    ).fetchall()
    if len(rows) > MAX_PENDING_MERGE_OPERATIONS:
        return False
    edges: dict[int, int] = {}
    for row in rows:
        try:
            dto = _dto(conn, user_id, row)
        except (TypeError, ValueError, ValidationError):
            return False
        result = dto.get("result") if dto.get("state") == "completed" else None
        if not isinstance(result, dict):
            continue
        source_id = result.get("source_application_id")
        destination_id = result.get("destination_application_id")
        if not isinstance(source_id, int) or not isinstance(destination_id, int):
            return False
        existing = edges.get(source_id)
        if existing is not None and existing != destination_id:
            return False
        edges[source_id] = destination_id
    current = source_application_id
    visited: set[int] = set()
    while current not in visited and current in edges:
        visited.add(current)
        current = edges[current]
        if current == destination_application_id:
            return True
    return False


def _reference_identities(conn: Connection, user_id: str, application_id: int) -> dict:
    """Freeze stable identities for four reference classes; counts alone are insufficient."""
    return {
        "timeline_entries": tuple(row[0] for row in conn.execute(
            "SELECT id FROM timeline_entries "
            "WHERE user_id = ? AND application_id = ? ORDER BY id",
            (user_id, application_id),
        ).fetchall()),
        "questions": tuple(row[0] for row in conn.execute(
            "SELECT id FROM questions WHERE user_id = ? AND application_id = ? ORDER BY id",
            (user_id, application_id),
        ).fetchall()),
        "question_occurrences": tuple(tuple(row) for row in conn.execute(
            "SELECT journal_id, question_id FROM review_question_occurrences "
            "WHERE user_id = ? AND application_id = ? ORDER BY journal_id, question_id",
            (user_id, application_id),
        ).fetchall()),
        "resumes": tuple(row[0] for row in conn.execute(
            "SELECT id FROM resumes WHERE user_id = ? AND application_id = ? ORDER BY id",
            (user_id, application_id),
        ).fetchall()),
    }


def _assert_merge_postconditions(conn: Connection, user_id: str,
                                 proposal: ApplicationMergeProposal,
                                 result: ApplicationMergeResult, *,
                                 final_projection: dict,
                                 expected_destination_identities: dict) -> None:
    source_id = proposal.source.application_id
    destination_id = proposal.destination.application_id
    if conn.execute(
        "SELECT 1 FROM applications WHERE user_id = ? AND id = ?",
        (user_id, source_id),
    ).fetchone() is not None:
        raise ApplicationMergeOperationConflict("岗位合并后源记录仍然存在")
    for table in (
        "timeline_entries", "questions", "review_question_occurrences",
        "resumes",
    ):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE user_id = ? AND application_id = ? LIMIT 1",
            (user_id, source_id),
        ).fetchone() is not None:
            raise ApplicationMergeOperationConflict(f"岗位合并后 {table} 仍指向源记录")
    table_fields = (
        ("timeline_entries", "timeline_entries"),
        ("questions", "questions"),
        ("review_question_occurrences", "question_occurrences"),
        ("resumes", "resumes"),
    )
    for table, field in table_fields:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ? AND application_id = ?",
            (user_id, destination_id),
        ).fetchone()[0]
        if count != getattr(result.destination_totals, field):
            raise ApplicationMergeOperationConflict(f"岗位合并后 {table} 总数与预览不一致")
    if _reference_identities(conn, user_id, destination_id) != expected_destination_identities:
        raise ApplicationMergeOperationConflict("岗位合并后关联记录身份集合与冻结预览不一致")
    destination = conn.execute(
        "SELECT company, position, department, channel, jd_text, jd_parsed_json, "
        "stage, current_step, current_state_entry_id, next_stage, next_step, next_date, "
        "next_time, next_note, paused_from_stage, pause_reason, application_note, "
        "priority, resume_id, applied_date, prep_status, prep_generation, "
        "prep_heartbeat_time, prep_json, revision "
        "FROM applications WHERE user_id = ? AND id = ?",
        (user_id, destination_id),
    ).fetchone()
    if destination is None:
        raise ApplicationMergeOperationConflict("岗位合并后保留记录不存在")
    final = proposal.effect.final_destination
    expected = (
        final.company,
        final.position,
        final.department,
        final.channel,
        final_projection["jd_text"],
        final_projection["jd_parsed_json"],
        final.stage,
        final.current_step,
        final_projection["current_state_entry_id"],
        final.next_action.stage if final.next_action is not None else None,
        final.next_action.step if final.next_action is not None else None,
        final.next_action.date if final.next_action is not None else None,
        final.next_action.time if final.next_action is not None else None,
        final.next_action.note if final.next_action is not None else None,
        final.paused_from_stage,
        final.pause_reason,
        final.application_note,
        final.priority,
        final.selected_resume.id if final.selected_resume is not None else None,
        final.applied_date,
        "none",
        None,
        None,
        None,
        proposal.destination.revision + 1,
    )
    if tuple(destination) != expected:
        raise ApplicationMergeOperationConflict("岗位合并后保留记录投影与冻结预览不一致")


def approve_application_merge_operation(db_path: str, user_id: str,
                                        operation_id: str | UUID) -> dict:
    canonical = _canonical_operation_id(operation_id)
    conflict_reason: str | None = None
    completed: dict | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _operation_row(conn, user_id, canonical)
        if row is None:
            raise ApplicationMergeOperationNotFound("岗位合并操作不存在")
        operation = _operation_payload(row[5])
        if row[2] != "awaiting_user":
            current = _dto(conn, user_id, row)
            if current["state"] == "completed":
                return current
            raise ApplicationMergeOperationConflict(
                f"该操作当前为 {current['state']}，不能批准",
            )
        if _safe_timestamp(row[3]) is None:
            conflict_reason = "envelope_invalid"
            _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
        else:
            try:
                proposal = _proposal(row[4])
            except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
                conflict_reason = "contract_invalid"
                _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
            else:
                proposal_digest = _proposal_digest(proposal)
                if operation != {
                    "type": "application_merge", "proposal_digest": proposal_digest,
                }:
                    conflict_reason = "envelope_invalid"
                    _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
                else:
                    try:
                        bundle = _load_merge_bundle(
                            conn,
                            user_id,
                            proposal.source.application_id,
                            proposal.destination.application_id,
                        )
                    except (
                        ApplicationMergeOperationNotFound,
                        _UnsafeMergeDependency,
                        ValidationError,
                        ValueError,
                    ):
                        bundle = None
                    if (bundle is None
                            or bundle.fingerprint != proposal.dependency_fingerprint
                            or bundle.source != proposal.source
                            or bundle.destination != proposal.destination
                            or bundle.effect != proposal.effect):
                        conflict_reason = "dependency_drifted"
                        _mark_stale(conn, user_id, row[0], row[6], conflict_reason)
                    else:
                        source_identities = _reference_identities(
                            conn, user_id, proposal.source.application_id,
                        )
                        destination_identities = _reference_identities(
                            conn, user_id, proposal.destination.application_id,
                        )
                        expected_destination_identities = {
                            key: tuple(sorted((*destination_identities[key], *source_identities[key])))
                            for key in destination_identities
                        }
                        destination_jd_text = conn.execute(
                            "SELECT jd_text FROM applications WHERE user_id = ? AND id = ?",
                            (user_id, proposal.destination.application_id),
                        ).fetchone()[0]
                        changes_before_executor = conn.total_changes
                        raw_result = repository._merge_applications_in_transaction(
                            conn,
                            user_id,
                            proposal.source.application_id,
                            proposal.destination.application_id,
                            final_projection=bundle.final_projection,
                            destination_company=proposal.destination.company,
                        )
                        moved_values = raw_result["moved"]
                        # A changed JD intentionally fires the schema receipt-invalidation
                        # trigger, whose UPDATE is included in sqlite total_changes.
                        jd_receipt_invalidated = int(
                            destination_jd_text != bundle.final_projection["jd_text"]
                        )
                        expected_direct_changes = (
                            sum(moved_values.values()) + 2 + jd_receipt_invalidated
                        )
                        if conn.total_changes - changes_before_executor != expected_direct_changes:
                            raise ApplicationMergeOperationConflict(
                                "岗位合并触发了冻结影响之外的数据写入，已回滚",
                            )
                        result = ApplicationMergeResult(
                            status="ok",
                            source_application_id=proposal.source.application_id,
                            destination_application_id=proposal.destination.application_id,
                            source_deleted=True,
                            moved=ApplicationMergeCounts.model_validate(raw_result["moved"]),
                            destination_totals=_total_counts(proposal.effect),
                            destination_prep_reset=True,
                            final_destination=proposal.effect.final_destination,
                        )
                        if not _result_matches_proposal(result, proposal):
                            raise ApplicationMergeOperationConflict(
                                "岗位合并的实际影响与冻结预览不一致",
                            )
                        _assert_merge_postconditions(
                            conn,
                            user_id,
                            proposal,
                            result,
                            final_projection=bundle.final_projection,
                            expected_destination_identities=expected_destination_identities,
                        )
                        command_hash = _command_hash("approve", canonical, proposal_digest)
                        derivation = {"operation": {
                            "type": "application_merge",
                            "proposal_digest": proposal_digest,
                            "action": "approve",
                            "command_hash": command_hash,
                            "result": result.model_dump(),
                        }}
                        changed = conn.execute(
                            "UPDATE journal SET state = 'applied', processed_time = ?, "
                            "derivation_json = ?, revision = revision + 1 "
                            "WHERE user_id = ? AND id = ? AND kind = 'correction' "
                            "AND state = 'awaiting_user' AND revision = ?",
                            (
                                now_iso(), json.dumps(derivation, ensure_ascii=False),
                                user_id, row[0], row[6],
                            ),
                        ).rowcount
                        if changed != 1:
                            raise ApplicationMergeOperationConflict("岗位合并操作状态已变化")
                        terminal = _operation_row(conn, user_id, canonical)
                        if terminal is None:  # pragma: no cover
                            raise ApplicationMergeOperationNotFound("岗位合并操作不存在")
                        completed = _dto(conn, user_id, terminal)
                        if completed["state"] != "completed":
                            raise ApplicationMergeOperationConflict(
                                "岗位合并完成回执无法与冻结预览核对",
                            )
    if conflict_reason is not None:
        raise ApplicationMergeOperationConflict("岗位合并预览已失效，请重新生成")
    if completed is None:  # pragma: no cover
        raise ApplicationMergeOperationConflict("岗位合并操作未完成")
    return completed


def reject_application_merge_operation(db_path: str, user_id: str,
                                       operation_id: str | UUID) -> dict:
    canonical = _canonical_operation_id(operation_id)
    rejected: dict | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _operation_row(conn, user_id, canonical)
        if row is None:
            raise ApplicationMergeOperationNotFound("岗位合并操作不存在")
        operation = _operation_payload(row[5])
        if row[2] != "awaiting_user":
            current = _dto(conn, user_id, row)
            if current["state"] == "rejected":
                return current
            raise ApplicationMergeOperationConflict(
                f"该操作当前为 {current['state']}，不能拒绝",
            )
        try:
            if _safe_timestamp(row[3]) is None:
                raise ValueError("invalid operation timestamp")
            proposal = _proposal(row[4])
            proposal_digest = _proposal_digest(proposal)
            if operation != {
                "type": "application_merge", "proposal_digest": proposal_digest,
            }:
                raise ValueError("proposal envelope digest mismatch")
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            _mark_stale(conn, user_id, row[0], row[6], "contract_invalid")
            stale = True
        else:
            stale = False
            derivation = {"operation": {
                "type": "application_merge",
                "proposal_digest": proposal_digest,
                "action": "reject",
                "command_hash": _command_hash("reject", canonical, proposal_digest),
            }}
            changed = conn.execute(
                "UPDATE journal SET state = 'voided', processed_time = NULL, "
                "derivation_json = ?, revision = revision + 1 "
                "WHERE user_id = ? AND id = ? AND kind = 'correction' "
                "AND state = 'awaiting_user' AND revision = ?",
                (
                    json.dumps(derivation, ensure_ascii=False), user_id, row[0], row[6],
                ),
            ).rowcount
            if changed != 1:
                raise ApplicationMergeOperationConflict("岗位合并操作状态已变化")
            terminal = _operation_row(conn, user_id, canonical)
            if terminal is None:  # pragma: no cover
                raise ApplicationMergeOperationNotFound("岗位合并操作不存在")
            rejected = _dto(conn, user_id, terminal)
            if rejected["state"] != "rejected":
                raise ApplicationMergeOperationConflict("岗位合并保留回执无法与冻结预览核对")
    if stale:
        raise ApplicationMergeOperationConflict("岗位合并预览已损坏，已安全终结")
    if rejected is None:  # pragma: no cover
        raise ApplicationMergeOperationConflict("岗位合并保留操作未完成")
    return rejected
