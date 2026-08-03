"""Projection planning, target plans and terminal receipt publication."""

from __future__ import annotations

from sqlite3 import Connection

from pydantic import ValidationError

from .....platform.database import (
    application_identity_key,
    loads_json,
    normalize_application_identity_part,
    now_iso,
    transaction,
)
from ....applications.public import ApplicationNextAction
from ... import repository
from ...ai_models import ReviewExtraction, infer_completed_next_action_clear
from ..record_models import (
    ReviewRecordApplication,
    ReviewRecordDerivation,
    ReviewRecordExistingTargetPlan,
    ReviewRecordMissingField,
    ReviewRecordNewTargetPlan,
    ReviewRecordOperationError,
    ReviewRecordPreview,
    ReviewRecordProposal,
    ReviewRecordResult,
    ReviewRecordTargetPlan,
)
from .dto import _dto, _pending_confirmation_preview
from .errors import (
    ReviewRecordOperationConflict,
    ReviewRecordOperationNotFound,
    _UnsafeRecordDependency,
)
from .rows import (
    MAX_AMBIGUOUS_POSITION_OPTIONS,
    _bounded_combined,
    _canonical_json,
    _object_json,
    _operation_row,
    _proposal,
    _proposal_digest,
    _supplement_rows,
    _text_digest,
)


def _projected_application_state(
    current_stage: str,
    current_step: str | None,
    extraction: ReviewExtraction,
) -> tuple[str, str | None]:
    projected = extraction.projected_state
    if projected is None:
        return current_stage, current_step
    return projected.stage or current_stage, projected.current_step or current_step


def _projected_next_action(
    current: ApplicationNextAction | None,
    projected_stage: str,
    extraction: ReviewExtraction,
) -> ApplicationNextAction | None:
    if projected_stage in {"withdrawn", "rejected"}:
        return None
    if extraction.clear_next_action:
        return None
    return extraction.next_action or current


def _projected_applied_date(
    current_stage: str,
    current_date: str | None,
    extraction: ReviewExtraction,
) -> str | None:
    if (
        current_stage != "applied"
        and extraction.projected_state is not None
        and extraction.projected_state.stage == "applied"
        and current_date is None
    ):
        return extraction.history.date if extraction.history is not None else None
    return current_date


def _resolve_target_plan(
    conn: Connection,
    user_id: str,
    extraction: ReviewExtraction,
) -> ReviewRecordTargetPlan | None:
    """Resolve the exact application effect without mutating any business table."""
    if extraction.company is None:
        return None
    columns = (
        "id, company, position, created_time, stage, current_step, revision, "
        "next_stage, next_step, next_date, next_time, next_note, applied_date, channel"
    )
    resolved = None
    if extraction.position is not None:
        identity_key = application_identity_key(extraction.company, extraction.position)
        resolved = conn.execute(
            f"SELECT {columns} FROM applications WHERE user_id = ? "
            "AND company_key = ? AND position_key = ?",
            (user_id, *identity_key),
        ).fetchone()
    # A company-only fallback is safe only when the user/model did not provide a
    # position at all.  An explicit, different position is a distinct identity:
    # falling back to the company's sole existing application would mix one job's
    # interview or result into another job.
    if resolved is None and extraction.position is None:
        company_key = normalize_application_identity_part(extraction.company)
        rows = conn.execute(
            f"SELECT {columns} FROM applications WHERE user_id = ? "
            "AND company_key = ? "
            "ORDER BY id LIMIT 2",
            (user_id, company_key),
        ).fetchall()
        if len(rows) == 1:
            resolved = rows[0]
    try:
        if resolved is not None:
            current_next_action = (
                ApplicationNextAction(
                    stage=resolved[7],
                    step=resolved[8],
                    date=resolved[9],
                    time=resolved[10],
                    note=resolved[11],
                )
                if resolved[8] is not None
                else None
            )
            projected_stage, projected_step = _projected_application_state(
                resolved[4],
                resolved[5],
                extraction,
            )
            return ReviewRecordExistingTargetPlan(
                kind="existing",
                application_id=resolved[0],
                company=resolved[1],
                position=resolved[2],
                created_time=resolved[3],
                revision=resolved[6],
                current_stage=resolved[4],
                current_step=resolved[5],
                projected_stage=projected_stage,
                projected_step=projected_step,
                current_next_action=current_next_action,
                projected_next_action=_projected_next_action(
                    current_next_action,
                    projected_stage,
                    extraction,
                ),
                current_applied_date=resolved[12],
                projected_applied_date=_projected_applied_date(
                    resolved[4],
                    resolved[12],
                    extraction,
                ),
                current_channel=resolved[13],
                projected_channel=extraction.channel or resolved[13],
            )
        if extraction.position is None:
            return None
        projected_stage, projected_step = _projected_application_state(
            "backlog",
            None,
            extraction,
        )
        return ReviewRecordNewTargetPlan(
            kind="new",
            company=extraction.company,
            position=extraction.position,
            current_stage="backlog",
            current_step=None,
            projected_stage=projected_stage,
            projected_step=projected_step,
            current_next_action=None,
            projected_next_action=_projected_next_action(
                None,
                projected_stage,
                extraction,
            ),
            current_applied_date=None,
            projected_applied_date=_projected_applied_date("backlog", None, extraction),
            current_channel=None,
            projected_channel=extraction.channel,
        )
    except ValidationError as error:
        raise ReviewRecordOperationConflict("复盘方案的岗位解析结果已损坏") from error


def _infer_target_next_action_clear(
    extraction: ReviewExtraction,
    target_plan: ReviewRecordTargetPlan | None,
) -> ReviewExtraction:
    """Resolve completion against the frozen live plan before user confirmation."""
    if target_plan is None:
        return extraction
    if target_plan.current_next_action is None:
        # The model cannot know whether a plan exists.  Drop an impossible clear
        # before showing the preview so we never claim a no-op was applied.
        return (
            extraction.model_copy(update={"clear_next_action": False})
            if extraction.clear_next_action
            else extraction
        )
    projected_stage = (
        extraction.projected_state.stage
        if extraction.projected_state is not None
        and extraction.projected_state.stage is not None
        else target_plan.current_stage
    )
    if (
        projected_stage in {"withdrawn", "rejected"}
        and extraction.next_action is None
        and not extraction.clear_next_action
    ):
        # Closing a flow necessarily removes its live plan.  Make that effect
        # explicit in the editable preview instead of showing “preserve” while
        # the projection silently clears it.
        return extraction.model_copy(update={"clear_next_action": True})
    return infer_completed_next_action_clear(extraction, target_plan.current_next_action)


def _missing_fields(
    conn: Connection,
    user_id: str,
    extraction: ReviewExtraction,
    target_plan: ReviewRecordTargetPlan | None,
) -> list[ReviewRecordMissingField]:
    """Block only unsafe role identity; leave other omitted facts empty."""
    missing: list[ReviewRecordMissingField] = []
    if not extraction.company:
        missing.append(ReviewRecordMissingField(field="company", ask="这场是哪家公司的？"))
    elif target_plan is None:
        company_key = normalize_application_identity_part(extraction.company)
        rows = conn.execute(
            "SELECT position FROM applications WHERE user_id = ? "
            "AND company_key = ? "
            "ORDER BY id LIMIT ?",
            (user_id, company_key, MAX_AMBIGUOUS_POSITION_OPTIONS + 1),
        ).fetchall()
        if len(rows) == 0:
            ask = f"请补充 {extraction.company} 对应的岗位名称。"
            missing.append(ReviewRecordMissingField(field="position", ask=ask))
        elif len(rows) > 1:
            if len(rows) > MAX_AMBIGUOUS_POSITION_OPTIONS:
                ask = (
                    f"{extraction.company}有超过 {MAX_AMBIGUOUS_POSITION_OPTIONS} 个岗位，"
                    "请说出时间线里的准确岗位名。"
                )
            else:
                names = " / ".join(position for (position,) in rows)
                ask = f"{extraction.company}你投了多个岗位（{names}），这场是哪个？"
            missing.append(ReviewRecordMissingField(field="position", ask=ask))
    return missing


def _source_and_combined(
    conn: Connection,
    user_id: str,
    proposal: ReviewRecordProposal,
) -> tuple[tuple, str] | None:
    target = conn.execute(
        "SELECT content, state, revision FROM journal WHERE user_id = ? AND id = ? "
        "AND kind = 'review'",
        (user_id, proposal.target_journal_id),
    ).fetchone()
    if target is None:
        return None
    if target[1:] != (
        proposal.target_expected_state,
        proposal.target_expected_revision,
    ):
        return None
    if proposal.mode == "initial":
        if proposal.source_journal_id != proposal.target_journal_id:
            return None
        source_text = target[0]
    else:
        source = conn.execute(
            "SELECT content FROM journal WHERE user_id = ? AND id = ? "
            "AND kind = 'correction' AND parent_journal_id = ? "
            "AND operation_id IS NULL AND state = 'applied' "
            "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.source_type') = 'review_supplement'",
            (user_id, proposal.source_journal_id, proposal.target_journal_id),
        ).fetchone()
        if source is None:
            return None
        source_text = source[0]
    if not isinstance(source_text, str) or _text_digest(source_text) != proposal.source_digest:
        return None
    supplements = _supplement_rows(conn, user_id, proposal.target_journal_id)
    combined = _bounded_combined(target[0], [content for _id, content in supplements])
    if _text_digest(combined) != proposal.combined_digest:
        return None
    return target, combined


def _terminal_derivation(
    proposal: ReviewRecordProposal,
    *,
    action: str,
    finished_time: str,
    result: ReviewRecordResult | None = None,
    error: ReviewRecordOperationError | None = None,
) -> dict:
    operation = {
        "type": "review_record",
        "action": action,
        "client_turn_id": proposal.client_turn_id,
        "proposal_digest": _proposal_digest(proposal),
        "finished_time": finished_time,
    }
    if result is not None:
        operation["result"] = result.model_dump(mode="json")
    if error is not None:
        operation["error"] = error.model_dump(mode="json")
    return {"operation": operation}


def _pending_confirmation_derivation(
    proposal: ReviewRecordProposal,
    preview: ReviewRecordPreview,
) -> dict:
    return {"operation": {
        "type": "review_record",
        "action": "pending_confirmation",
        "client_turn_id": proposal.client_turn_id,
        "proposal_digest": _proposal_digest(proposal),
        **preview.model_dump(mode="json"),
    }}


def _finish_operation_in_transaction(
    conn: Connection,
    user_id: str,
    operation_row,
    proposal: ReviewRecordProposal,
    result: ReviewRecordResult,
) -> dict:
    finished = now_iso()
    changed = conn.execute(
        "UPDATE journal SET processed_time = ?, derivation_json = ?, state = 'applied', "
        "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND operation_id = ? AND state = 'awaiting_user' AND revision = 1",
        (
            finished,
            _canonical_json(_terminal_derivation(
                proposal,
                action="complete",
                finished_time=finished,
                result=result,
            )),
            user_id,
            operation_row[0],
            proposal.operation_id,
        ),
    ).rowcount
    if changed != 1:
        raise ReviewRecordOperationConflict("复盘操作终态发布发生竞争")
    terminal = _operation_row(conn, user_id, proposal.operation_id)
    return _dto(conn, user_id, terminal)


def _supersede_operation_in_transaction(
    conn: Connection,
    user_id: str,
    operation_row,
    proposal: ReviewRecordProposal,
    *,
    code: str = "target_changed",
    message: str = "复盘或补充在提取期间已变化，本次旧结果没有发布。",
) -> dict:
    expected_state = operation_row[2]
    expected_revision = operation_row[8]
    if expected_state not in {"pending", "awaiting_user"}:
        return _dto(conn, user_id, operation_row)
    error = ReviewRecordOperationError(code=code, message=message)
    finished = now_iso()
    changed = conn.execute(
        "UPDATE journal SET processed_time = ?, derivation_json = ?, state = 'superseded', "
        "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND operation_id = ? AND state = ? AND revision = ?",
        (
            finished,
            _canonical_json(_terminal_derivation(
                proposal,
                action="superseded",
                finished_time=finished,
                error=error,
            )),
            user_id,
            operation_row[0],
            proposal.operation_id,
            expected_state,
            expected_revision,
        ),
    ).rowcount
    if changed != 1:
        raise ReviewRecordOperationConflict("复盘操作 superseded 终态发布发生竞争")
    return _dto(conn, user_id, _operation_row(conn, user_id, proposal.operation_id))


def _validate_applied_projection(
    conn: Connection,
    user_id: str,
    proposal: ReviewRecordProposal,
    extraction: ReviewExtraction,
    raw_derivation: dict,
    target_plan: ReviewRecordTargetPlan,
) -> tuple[ReviewRecordDerivation, ReviewRecordApplication]:
    try:
        derivation = ReviewRecordDerivation.model_validate(raw_derivation)
    except (TypeError, ValueError, ValidationError) as error:
        raise ReviewRecordOperationConflict("复盘派生回执不符合严格契约") from error
    target = conn.execute(
        "SELECT state, revision, extraction_json, derivation_json FROM journal "
        "WHERE user_id = ? AND id = ? AND kind = 'review'",
        (user_id, proposal.target_journal_id),
    ).fetchone()
    if target is None or target[0] != "applied" or target[1] != derivation.revision:
        raise ReviewRecordOperationConflict("复盘目标没有原子进入 applied 终态")
    persisted_extraction = _object_json(target[2], "Review extraction")
    expected_extraction = extraction.model_dump(mode="json")
    if _canonical_json(persisted_extraction) != _canonical_json(expected_extraction):
        raise ReviewRecordOperationConflict("Review extraction 与提取结果不一致")
    persisted_derivation = _object_json(target[3], "Review derivation")
    expected_derivation = derivation.model_dump(mode="json", exclude={"revision"})
    if _canonical_json(persisted_derivation) != _canonical_json(expected_derivation):
        raise ReviewRecordOperationConflict("Review derivation 与派生回执不一致")

    application_row = conn.execute(
        "SELECT id, company, position, created_time, stage, current_step, revision, "
        "next_stage, next_step, next_date, next_time, next_note, applied_date, channel "
        "FROM applications "
        "WHERE user_id = ? AND id = ?",
        (user_id, derivation.application_id),
    ).fetchone()
    if application_row is None:
        raise ReviewRecordOperationConflict("复盘派生的岗位身份不存在或跨租户")
    projected_next = target_plan.projected_next_action
    next_columns = (
        (None, None, None, None, None)
        if projected_next is None
        else (
            projected_next.stage,
            projected_next.step,
            projected_next.date,
            projected_next.time,
            projected_next.note,
        )
    )
    if target_plan.kind == "existing":
        expected_identity = (
            target_plan.application_id,
            target_plan.company,
            target_plan.position,
            target_plan.created_time,
            target_plan.projected_stage,
            target_plan.projected_step,
            target_plan.revision + 1,
            *next_columns,
            target_plan.projected_applied_date,
            target_plan.projected_channel,
        )
    else:
        expected_identity = (
            application_row[0],
            target_plan.company,
            target_plan.position,
            application_row[3],
            target_plan.projected_stage,
            target_plan.projected_step,
            1,
            *next_columns,
            target_plan.projected_applied_date,
            target_plan.projected_channel,
        )
    if tuple(application_row) != expected_identity:
        raise ReviewRecordOperationConflict("复盘实际岗位投影与确认方案不一致")
    if derivation.application_created != (target_plan.kind == "new"):
        raise ReviewRecordOperationConflict("复盘岗位创建来源与确认方案不一致")
    application = ReviewRecordApplication(
        id=application_row[0],
        company=application_row[1],
        position=application_row[2],
    )

    entry_rows = conn.execute(
        "SELECT id, application_id, step, occurred_date, outcome, summary, "
        "from_stage, from_step, to_stage, to_step, source FROM timeline_entries "
        "WHERE user_id = ? AND journal_id = ? ORDER BY id",
        (user_id, proposal.target_journal_id),
    ).fetchall()
    history = extraction.history
    expected_step = history.step if history is not None else None
    expected_summary = history.summary if history is not None else (
        f"已安排下一步：{extraction.next_action.step}"
        if extraction.next_action is not None
        else "已清除下一步安排" if extraction.clear_next_action else None
    )
    if entry_rows != [(
        derivation.timeline_entry_ids[0],
        derivation.application_id,
        expected_step,
        history.date if history is not None else None,
        history.outcome if history is not None else None,
        expected_summary,
        target_plan.current_stage,
        target_plan.current_step,
        target_plan.projected_stage,
        target_plan.projected_step,
        "review",
    )]:
        raise ReviewRecordOperationConflict("复盘历程投影与提取结果不一致")

    question_rows: list = []
    if derivation.question_ids:
        placeholders = ",".join("?" for _ in derivation.question_ids)
        question_rows = conn.execute(
            f"SELECT id, text, source FROM questions WHERE user_id = ? "
            f"AND id IN ({placeholders}) ORDER BY CASE id "
            + " ".join(
                f"WHEN {question_id:d} THEN {index:d}"
                for index, question_id in enumerate(derivation.question_ids)
            )
            + " END",
            (user_id, *derivation.question_ids),
        ).fetchall()
    expected_questions = [
        (question_id, question.text, "real")
        for question_id, question in zip(
            derivation.question_ids,
            extraction.questions,
            strict=True,
        )
    ]
    if question_rows != expected_questions:
        raise ReviewRecordOperationConflict("复盘 question 投影与提取结果不一致")

    occurrence_rows = conn.execute(
        "SELECT question_id, application_id, company, source_step, asked_date "
        "FROM review_question_occurrences WHERE user_id = ? AND journal_id = ? "
        "ORDER BY question_id",
        (user_id, proposal.target_journal_id),
    ).fetchall()
    expected_occurrences = sorted(
        (
            question_id,
            derivation.application_id,
            application.company,
            history.step if history is not None else (
                extraction.projected_state.current_step
                if extraction.projected_state is not None
                else None
            ),
            history.date if history is not None else None,
        )
        for question_id in derivation.question_ids
    )
    if occurrence_rows != expected_occurrences:
        raise ReviewRecordOperationConflict("复盘 occurrence 投影与提取结果不一致")

    expected_knowledge_names = sorted({
        point
        for question in extraction.questions
        for point in question.knowledge_points
    })
    knowledge_rows: list = []
    if derivation.knowledge_point_ids:
        placeholders = ",".join("?" for _ in derivation.knowledge_point_ids)
        knowledge_rows = conn.execute(
            f"SELECT id, name FROM knowledge_points WHERE user_id = ? "
            f"AND id IN ({placeholders}) ORDER BY id",
            (user_id, *derivation.knowledge_point_ids),
        ).fetchall()
    if (
        [row[0] for row in knowledge_rows] != derivation.knowledge_point_ids
        or sorted(row[1] for row in knowledge_rows) != expected_knowledge_names
    ):
        raise ReviewRecordOperationConflict("复盘 knowledge 投影与提取结果不一致")
    for question_id, question in zip(
        derivation.question_ids,
        extraction.questions,
        strict=True,
    ):
        linked_names = {
            name
            for (name,) in conn.execute(
                "SELECT knowledge.name FROM question_knowledge link "
                "JOIN knowledge_points knowledge ON knowledge.id = link.knowledge_point_id "
                "WHERE link.question_id = ? AND knowledge.user_id = ?",
                (question_id, user_id),
            ).fetchall()
        }
        if not set(question.knowledge_points).issubset(linked_names):
            raise ReviewRecordOperationConflict("复盘 question/knowledge 关联缺失")

    status_rows = conn.execute(
        "SELECT id, log_date, time_of_day, mood, factors_json FROM status_log "
        "WHERE user_id = ? AND journal_id = ? ORDER BY id",
        (user_id, proposal.target_journal_id),
    ).fetchall()
    status_expected = bool(
        history is not None
        and history.date
        and (extraction.mood or extraction.factors or extraction.time_of_day)
    )
    if len(status_rows) != int(status_expected):
        raise ReviewRecordOperationConflict("复盘 status log 数量与提取结果不一致")
    if status_expected:
        status = status_rows[0]
        if (
            [status[0]] != derivation.status_log_ids
            or status[2] != extraction.time_of_day
            or status[3] != extraction.mood
            or _canonical_json(loads_json(status[4], None))
            != _canonical_json(extraction.factors)
            or (history is not None and history.date is not None and status[1] != history.date)
        ):
            raise ReviewRecordOperationConflict("复盘 status log 与提取结果不一致")
    elif derivation.status_log_ids:
        raise ReviewRecordOperationConflict("复盘回执包含不存在的 status log")
    return derivation, application


def _stage_operation_for_confirmation_in_transaction(
    conn: Connection,
    user_id: str,
    proposal: ReviewRecordProposal,
    extraction: ReviewExtraction,
) -> dict:
    """Persist one bounded preview inside the caller's write transaction."""
    operation_row = _operation_row(conn, user_id, proposal.operation_id)
    if operation_row is None:
        raise ReviewRecordOperationNotFound("复盘记录操作不存在")
    if operation_row[2] != "pending":
        return _dto(conn, user_id, operation_row)
    live_proposal = _proposal(operation_row[5])
    if live_proposal != proposal:
        raise ReviewRecordOperationConflict("复盘操作 owner 或命令已变化")
    try:
        source = _source_and_combined(conn, user_id, proposal)
    except _UnsafeRecordDependency:
        source = None
    if source is None:
        return _supersede_operation_in_transaction(
            conn,
            user_id,
            operation_row,
            proposal,
            code="source_changed",
            message="复盘原文、补充或目标 revision 已变化，旧提取结果未发布。",
        )

    target_plan = _resolve_target_plan(conn, user_id, extraction)
    extraction = _infer_target_next_action_clear(extraction, target_plan)
    target_plan = _resolve_target_plan(conn, user_id, extraction)
    try:
        preview = ReviewRecordPreview(
            extraction=extraction,
            target_plan=target_plan,
            missing=_missing_fields(conn, user_id, extraction, target_plan),
        )
    except ValidationError as error:
        raise ReviewRecordOperationConflict("复盘提取结果没有可发布的有效进展") from error
    changed = conn.execute(
        "UPDATE journal SET derivation_json = ?, state = 'awaiting_user', "
        "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND operation_id = ? AND state = 'pending' AND revision = 0",
        (
            _canonical_json(_pending_confirmation_derivation(proposal, preview)),
            user_id,
            operation_row[0],
            proposal.operation_id,
        ),
    ).rowcount
    if changed != 1:
        raise ReviewRecordOperationConflict("复盘确认预览发布发生竞争")
    return _dto(conn, user_id, _operation_row(conn, user_id, proposal.operation_id))


def _stage_operation_for_confirmation(
    db_path: str,
    user_id: str,
    proposal: ReviewRecordProposal,
    extraction: ReviewExtraction,
) -> dict:
    """Persist a bounded preview without publishing any business projection."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _stage_operation_for_confirmation_in_transaction(
            conn,
            user_id,
            proposal,
            extraction,
        )


def _finalize_operation_in_transaction(
    conn: Connection,
    user_id: str,
    proposal: ReviewRecordProposal,
    extraction: ReviewExtraction,
    *,
    frozen_target_plan: ReviewRecordTargetPlan | None = None,
    target_plan_prevalidated: bool = False,
) -> dict:
    """Finalize one preview inside the caller's write transaction."""
    operation_row = _operation_row(conn, user_id, proposal.operation_id)
    if operation_row is None:
        raise ReviewRecordOperationNotFound("复盘记录操作不存在")
    if operation_row[2] != "awaiting_user":
        if operation_row[2] == "pending":
            raise ReviewRecordOperationConflict("复盘仍在提取，暂时不能确认")
        return _dto(conn, user_id, operation_row)
    live_proposal = _proposal(operation_row[5])
    if live_proposal != proposal:
        raise ReviewRecordOperationConflict("复盘操作 owner 或命令已变化")
    preview = _pending_confirmation_preview(operation_row[6], proposal)
    if preview.extraction != extraction:
        raise ReviewRecordOperationConflict("批准的复盘提取预览与持久快照不一致")
    try:
        source = _source_and_combined(conn, user_id, proposal)
    except _UnsafeRecordDependency:
        source = None
    if source is None:
        return _supersede_operation_in_transaction(
            conn,
            user_id,
            operation_row,
            proposal,
            code="source_changed",
            message="复盘原文、补充或目标 revision 已变化，待确认预览未发布。",
        )

    target_plan = (
        frozen_target_plan
        if target_plan_prevalidated
        else _resolve_target_plan(conn, user_id, extraction)
    )
    if target_plan != preview.target_plan:
        return _supersede_operation_in_transaction(
            conn,
            user_id,
            operation_row,
            proposal,
            code="target_changed",
            message="岗位归属或状态在确认前已变化，旧复盘方案没有发布。",
        )
    missing = _missing_fields(conn, user_id, extraction, target_plan)
    if missing:
        target = conn.execute(
            "UPDATE journal SET extraction_json = ?, state = 'awaiting_user', "
            "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'review' "
            "AND state = ? AND revision = ? RETURNING revision",
            (
                _canonical_json(extraction.model_dump(mode="json")),
                user_id,
                proposal.target_journal_id,
                proposal.target_expected_state,
                proposal.target_expected_revision,
            ),
        ).fetchone()
        if target is None:  # pragma: no cover - source check in same write snapshot
            return _supersede_operation_in_transaction(
                conn,
                user_id,
                operation_row,
                proposal,
            )
        result = ReviewRecordResult(
            outcome="needs_clarification",
            review_reference=proposal.review_reference,
            source_journal_id=proposal.source_journal_id,
            target_journal_id=proposal.target_journal_id,
            target_revision=target[0],
            extraction=extraction,
            missing=missing,
        )
        return _finish_operation_in_transaction(
            conn,
            user_id,
            operation_row,
            proposal,
            result,
        )

    if target_plan is None:  # pragma: no cover - missing policy above is exhaustive
        raise ReviewRecordOperationConflict("复盘确认方案缺少岗位归属")
    raw_derivation = repository._derive_review_in_transaction(
        conn,
        user_id,
        proposal.target_journal_id,
        extraction.model_dump(mode="json"),
        replay=False,
        expected_state=proposal.target_expected_state,
        expected_revision=proposal.target_expected_revision,
        frozen_application_id=(
            target_plan.application_id
            if target_plan.kind == "existing"
            else None
        ),
        force_exact_new_application=(
            target_plan.kind == "new"
        ),
    )
    derivation, application = _validate_applied_projection(
        conn,
        user_id,
        proposal,
        extraction,
        raw_derivation,
        target_plan,
    )
    result = ReviewRecordResult(
        outcome="applied",
        review_reference=proposal.review_reference,
        source_journal_id=proposal.source_journal_id,
        target_journal_id=proposal.target_journal_id,
        target_revision=derivation.revision,
        extraction=extraction,
        derivation=derivation,
        application=application,
    )
    return _finish_operation_in_transaction(
        conn,
        user_id,
        operation_row,
        proposal,
        result,
    )


def _finalize_operation(
    db_path: str,
    user_id: str,
    proposal: ReviewRecordProposal,
    extraction: ReviewExtraction,
) -> dict:
    """Approve one persisted preview after atomically revalidating every dependency."""
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _finalize_operation_in_transaction(
            conn,
            user_id,
            proposal,
            extraction,
        )


def _fail_processing_operation(
    db_path: str,
    user_id: str,
    proposal: ReviewRecordProposal,
    *,
    code: str,
    message: str,
) -> dict | None:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _operation_row(conn, user_id, proposal.operation_id)
        if row is None:
            return None
        if row[2] != "pending":
            return _dto(conn, user_id, row)
        live = _proposal(row[5])
        if live != proposal:
            raise ReviewRecordOperationConflict("复盘操作 owner 或命令已变化")
        terminal_state = "failed"
        action = "failed"
        error_code = code
        error_message = message
        if proposal.mode == "initial":
            target_error = {
                "extract_failed": True,
                "operation_id": proposal.operation_id,
                "reason": code,
            }
            changed = conn.execute(
                "UPDATE journal SET derivation_json = ?, state = 'failed', "
                "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'review' "
                "AND state = 'pending' AND revision = ?",
                (
                    _canonical_json(target_error),
                    user_id,
                    proposal.target_journal_id,
                    proposal.target_expected_revision,
                ),
            ).rowcount
            if changed != 1:
                terminal_state = "superseded"
                action = "superseded"
                error_code = "target_changed"
                error_message = "复盘目标在提取失败收口前已变化。"
        error = ReviewRecordOperationError(code=error_code, message=error_message)
        finished = now_iso()
        changed = conn.execute(
            "UPDATE journal SET processed_time = ?, derivation_json = ?, state = ?, "
            "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'correction' "
            "AND operation_id = ? AND state = 'pending' AND revision = 0",
            (
                finished,
                _canonical_json(_terminal_derivation(
                    proposal,
                    action=action,
                    finished_time=finished,
                    error=error,
                )),
                terminal_state,
                user_id,
                row[0],
                proposal.operation_id,
            ),
        ).rowcount
        if changed != 1:
            raise ReviewRecordOperationConflict("复盘操作失败终态发布发生竞争")
        return _dto(conn, user_id, _operation_row(conn, user_id, proposal.operation_id))
