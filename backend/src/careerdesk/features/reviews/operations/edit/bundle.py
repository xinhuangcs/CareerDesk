"""Load and freeze one Review timeline-entry dependency bundle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from sqlite3 import Connection

from pydantic import ValidationError

from ....applications.public import (
    has_completed_application_merge_lineage_in_transaction,
    timeline_entry_snapshot_fingerprint as application_timeline_entry_fingerprint,
)
from ...ai_models import ReviewExtraction
from ..edit_models import (
    ReviewTimelineEntryApplicationProjection,
    ReviewTimelineEntryEditTarget,
    ReviewTimelineEntryProjection,
)
from ..record_models import ReviewRecordDerivation
from .errors import _EditTargetMissing, _UnsafeEditDependency

MAX_OCCURRENCES = 50


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Bundle:
    journal_id: int
    journal_created_time: str
    journal_revision: int
    journal_content: str
    extraction_raw: str
    derivation_raw: str
    extraction: ReviewExtraction
    target: ReviewTimelineEntryEditTarget
    entry: ReviewTimelineEntryProjection
    entry_row: dict
    application: ReviewTimelineEntryApplicationProjection
    application_row: dict
    occurrences: tuple[dict, ...]
    status_logs: tuple[dict, ...]
    fingerprint: str


def _load_bundle(conn: Connection, user_id: str, journal_id: int) -> _Bundle:
    source = conn.execute(
        "SELECT id, created_time, revision, content, extraction_json, derivation_json "
        "FROM journal WHERE user_id = ? AND id = ? AND kind = 'review' AND state = 'applied'",
        (user_id, journal_id),
    ).fetchone()
    if source is None:
        raise _EditTargetMissing("复盘不存在或已不在 applied 状态")
    if not isinstance(source[4], str) or not isinstance(source[5], str):
        raise _UnsafeEditDependency("复盘结构化快照缺失")
    try:
        extraction = ReviewExtraction.model_validate(json.loads(source[4]))
        derivation = ReviewRecordDerivation.model_validate({
            **json.loads(source[5]),
            "revision": source[2],
        })
    except (json.JSONDecodeError, TypeError, ValidationError) as error:
        raise _UnsafeEditDependency("复盘结构化快照损坏") from error

    entries = conn.execute(
        "SELECT id, application_id, step, occurred_date, outcome, summary, from_stage, "
        "from_step, to_stage, to_step, source, created_time FROM timeline_entries "
        "WHERE user_id = ? AND journal_id = ? ORDER BY id LIMIT 2",
        (user_id, journal_id),
    ).fetchall()
    if len(entries) != 1:
        raise _UnsafeEditDependency("复盘必须精确对应一条历程")
    entry = entries[0]
    application = conn.execute(
        "SELECT id, company, position, created_time, stage, current_step, "
        "current_state_entry_id, revision FROM applications WHERE user_id = ? AND id = ?",
        (user_id, entry[1]),
    ).fetchone()
    if application is None:
        raise _EditTargetMissing("复盘岗位已不存在")
    if derivation.timeline_entry_ids != [entry[0]]:
        raise _UnsafeEditDependency("复盘历程身份与派生快照不一致")
    if entry[1] != application[0]:
        raise _UnsafeEditDependency("复盘岗位身份与派生快照不一致")
    merged_application = derivation.application_id != entry[1]
    if merged_application and not has_completed_application_merge_lineage_in_transaction(
        conn,
        user_id,
        derivation.application_id,
        entry[1],
    ):
        raise _UnsafeEditDependency("复盘岗位身份缺少可验证的合并链路")
    if entry[10] != "review":
        raise _UnsafeEditDependency("复盘历程来源不一致")
    # Company and position in the extraction are historical facts, not live
    # foreign keys.  A legitimate profile rename must not make the Review
    # timeline entry permanently uneditable.  Ownership is established by the
    # stable entry/application ids (or a verified completed merge lineage), and
    # the current created_time is frozen in the edit proposal for approval CAS.

    before_projection = derivation.application_before
    after_projection = derivation.application_after
    if (
        (entry[6], entry[7])
        != (before_projection.stage, before_projection.current_step)
        or (entry[8], entry[9])
        != (after_projection.stage, after_projection.current_step)
    ):
        raise _UnsafeEditDependency("复盘历程跳转与岗位派生快照不一致")

    history = extraction.history
    expected_history = (
        (
            history.step,
            history.date,
            history.outcome,
            history.summary,
        )
        if history is not None
        else (
            None,
            None,
            None,
            f"已安排下一步：{extraction.next_action.step}"
            if extraction.next_action is not None
            else "已清除下一步安排" if extraction.clear_next_action else None,
        )
    )
    if tuple(entry[2:6]) != expected_history:
        raise _UnsafeEditDependency("复盘历程与结构化事实不一致")

    entry_row = {
        "id": entry[0],
        "application_id": entry[1],
        "step": entry[2],
        "occurred_date": entry[3],
        "outcome": entry[4],
        "summary": entry[5],
        "from_stage": entry[6],
        "from_step": entry[7],
        "to_stage": entry[8],
        "to_step": entry[9],
        "source": entry[10],
        "created_time": entry[11],
    }
    application_row = {
        "id": application[0],
        "company": application[1],
        "position": application[2],
        "created_time": application[3],
        "stage": application[4],
        "current_step": application[5],
        "current_state_entry_id": application[6],
        "revision": application[7],
    }
    occurrence_rows = conn.execute(
        "SELECT question_id, application_id, company, source_step, asked_date "
        "FROM review_question_occurrences WHERE user_id = ? AND journal_id = ? "
        "ORDER BY question_id LIMIT ?",
        (user_id, journal_id, MAX_OCCURRENCES + 1),
    ).fetchall()
    if len(occurrence_rows) > MAX_OCCURRENCES:
        raise _UnsafeEditDependency("复盘题目出处超过安全上限")
    occurrences = tuple({
        "question_id": row[0],
        "application_id": row[1],
        "company": row[2],
        "source_step": row[3],
        "asked_date": row[4],
    } for row in occurrence_rows)
    question_ids = {row[0] for row in occurrence_rows}
    if question_ids:
        placeholders = ",".join("?" for _ in question_ids)
        owned_question_ids = {
            row[0] for row in conn.execute(
                f"SELECT id FROM questions WHERE user_id = ? AND id IN ({placeholders})",
                (user_id, *sorted(question_ids)),
            ).fetchall()
        }
        if owned_question_ids != question_ids:
            raise _UnsafeEditDependency("复盘题目出处缺少同租户 question 绑定")
    if any(
        row[1] != application[0] or row[2] != application[1]
        for row in occurrence_rows
    ):
        raise _UnsafeEditDependency("复盘题目出处与岗位身份不一致")
    status_rows = conn.execute(
        "SELECT id, log_date, created_time FROM status_log "
        "WHERE user_id = ? AND journal_id = ? ORDER BY id LIMIT 2",
        (user_id, journal_id),
    ).fetchall()
    if len(status_rows) > 1:
        raise _UnsafeEditDependency("复盘状态日志超过安全上限")
    if status_rows and status_rows[0][1] != entry[3]:
        raise _UnsafeEditDependency("复盘状态日志日期与历程不一致")
    status_logs = tuple({
        "id": row[0], "log_date": row[1], "created_time": row[2],
    } for row in status_rows)

    try:
        target = ReviewTimelineEntryEditTarget(
            journal_id=source[0],
            journal_created_time=source[1],
            application_id=application[0],
            application_created_time=application[3],
            timeline_entry_id=entry[0],
            timeline_entry_created_time=entry[11],
            company=application[1],
            position=application[2],
        )
        entry_projection = ReviewTimelineEntryProjection(
            step=entry[2],
            occurred_date=entry[3],
            outcome=entry[4],
            summary=entry[5],
            from_stage=entry[6],
            from_step=entry[7],
            to_stage=entry[8],
            to_step=entry[9],
            journal_revision=source[2],
        )
        application_projection = ReviewTimelineEntryApplicationProjection(
            stage=application[4],
            current_step=application[5],
            current_state_entry_id=application[6],
            revision=application[7],
        )
        expected_application_projection = ReviewTimelineEntryApplicationProjection(
            stage=after_projection.stage,
            current_step=after_projection.current_step,
            current_state_entry_id=after_projection.current_state_entry_id,
            revision=application[7],
        )
    except ValidationError as error:
        raise _UnsafeEditDependency("复盘历程或岗位投影已损坏") from error
    if application_projection != expected_application_projection:
        state_provenance = conn.execute(
            "SELECT 1 FROM timeline_entries WHERE user_id = ? AND application_id = ? "
            "AND id = ? AND (from_stage != to_stage OR from_step IS NOT to_step) "
            "AND to_stage = ? AND to_step IS ? AND (? OR id > ?) LIMIT 1",
            (
                user_id,
                application[0],
                application[6],
                application[4],
                application[5],
                merged_application,
                entry[0],
            ),
        ).fetchone()
        base_projection = (
            application[4] == "backlog"
            and application[5] is None
            and application[6] is None
        )
        if state_provenance is None and not (merged_application and base_projection):
            raise _UnsafeEditDependency("岗位当前投影已偏离该复盘快照")
    dependency = {
        "journal": tuple(source),
        "entry": entry_row,
        "application": application_row,
        "occurrences": occurrences,
        "status_logs": status_logs,
    }
    return _Bundle(
        journal_id=source[0],
        journal_created_time=source[1],
        journal_revision=source[2],
        journal_content=source[3],
        extraction_raw=source[4],
        derivation_raw=source[5],
        extraction=extraction,
        target=target,
        entry=entry_projection,
        entry_row=entry_row,
        application=application_projection,
        application_row=application_row,
        occurrences=occurrences,
        status_logs=status_logs,
        fingerprint=_fingerprint(dependency),
    )


def timeline_entry_snapshot_fingerprint(bundle: _Bundle) -> str:
    return application_timeline_entry_fingerprint(
        created_time=bundle.entry_row["created_time"],
        step=bundle.entry.step,
        occurred_date=bundle.entry.occurred_date,
        outcome=bundle.entry.outcome,
        summary=bundle.entry.summary,
        from_stage=bundle.entry.from_stage,
        from_step=bundle.entry.from_step,
        to_stage=bundle.entry.to_stage,
        to_step=bundle.entry.to_step,
        source="review",
    )
