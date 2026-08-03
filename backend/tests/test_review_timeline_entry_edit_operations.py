
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from careerdesk.agentic.tools import ManageReviewTool
from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.applications import operations as application_operations
from careerdesk.features.applications.public import (
    apply_application_progress_in_transaction,
    execute_application_update_operation,
)
from careerdesk.features.journal.public import append_review
from careerdesk.features.preferences.public import execute_preference_update_operation
from careerdesk.features.reviews.operations import edit as edit_operations
from careerdesk.features.reviews.operations.edit_models import ReviewTimelineEntryEditOperationDTO
from tests.review_record_test_helpers import derive_review_for_test


def make_db(tmp_path) -> str:
    path = str(tmp_path / "review-timeline-entry-edit.db")
    init_db(path)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client, str(tmp_path / "careerdesk.db")
    get_settings.cache_clear()


def record_review(
    db_path: str,
    *,
    user_id: str = "u1",
    company: str = "A",
    position: str = "P",
    stage: str = "interviewing",
    step: str | None = "一面",
    occurred_date: str | None = "2026-07-01",
    with_details: bool = True,
) -> int:
    extraction = {
        "company": company,
        "position": position,
        "channel": None,
        "history": {
            "step": step,
            "date": occurred_date,
            "outcome": None,
            "summary": f"{company} {position} 复盘",
        },
        "projected_state": {"stage": stage, "current_step": step},
        "next_action": {
            "stage": stage,
            "step": "等待后续",
            "date": "2026-07-20",
            "time": None,
            "note": None,
        },
        "questions": (
            [{"text": "共享题？", "stuck": True, "knowledge_points": ["幂等"]}]
            if with_details
            else []
        ),
        "mood": "紧张" if with_details else None,
        "time_of_day": "afternoon" if with_details else None,
        "factors": ["睡眠"] if with_details else [],
    }
    created = append_review(db_path, user_id, extraction["history"]["summary"])
    derive_review_for_test(db_path, user_id, created["id"], extraction)
    return created["id"]


def execute(db_path: str, *, operation_id=None, turn_id=None, user_id="u1", **kwargs):
    return edit_operations.execute_review_timeline_entry_edit_operation(
        db_path,
        user_id,
        operation_id=operation_id or uuid4(),
        client_turn_id=turn_id or uuid4(),
        company=kwargs.pop("company", "A"),
        position=kwargs.pop("position", "P"),
        changes=kwargs.pop("changes", {"step": "二面"}),
        **kwargs,
    )


def one(db_path: str, sql: str, *params):
    with read_connection(db_path) as conn:
        return conn.execute(sql, params).fetchone()


def test_review_extraction_requires_the_current_canonical_contract():
    with pytest.raises(
        edit_operations.ReviewTimelineEntryEditOperationConflict,
        match="extraction 已损坏",
    ):
        edit_operations.validate._review_extraction(json.dumps({"company": "A"}))


def test_stale_operation_dto_requires_an_explicit_invalid_operation_reason(tmp_path):
    db_path = make_db(tmp_path)
    record_review(db_path, with_details=False)
    completed = execute(db_path)
    stale = {
        **completed,
        "state": "stale",
        "result": None,
        "undo_available": False,
        "undo_block_reason": None,
    }

    with pytest.raises(ValidationError, match="must report invalid operation"):
        ReviewTimelineEntryEditOperationDTO.model_validate(stale)

    stale["undo_block_reason"] = "operation_invalid"
    assert ReviewTimelineEntryEditOperationDTO.model_validate(stale).state == "stale"


def test_apply_is_in_place_audited_and_idempotent(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path)
    before = one(
        db_path,
        "SELECT j.revision, e.id, e.created_time, s.id, q.id, q.updated_time, "
        "a.id, a.revision FROM journal j "
        "JOIN timeline_entries e ON e.user_id=j.user_id AND e.journal_id=j.id "
        "JOIN applications a ON a.user_id=e.user_id AND a.id=e.application_id "
        "JOIN status_log s ON s.user_id=j.user_id AND s.journal_id=j.id "
        "JOIN review_question_occurrences o ON o.user_id=j.user_id AND o.journal_id=j.id "
        "JOIN questions q ON q.user_id=o.user_id AND q.id=o.question_id WHERE j.id=?",
        journal_id,
    )
    operation_id, turn_id = str(uuid4()), str(uuid4())

    completed = execute(
        db_path,
        operation_id=operation_id,
        turn_id=turn_id,
        changes={
            "outcome": "passed",
            "step": "二面",
            "occurred_date": "2026-07-02",
        },
    )
    replay = execute(
        db_path,
        operation_id=operation_id,
        turn_id=turn_id,
        changes={
            "outcome": "passed",
            "step": "二面",
            "occurred_date": "2026-07-02",
        },
    )

    assert replay == completed
    assert completed["operation_type"] == "review_timeline_entry_edit"
    assert completed["state"] == "completed" and completed["undo_available"]
    assert completed["before"]["journal_revision"] == before[0]
    assert completed["final"]["journal_revision"] == before[0] + 1
    assert completed["effect"]["changed_fields"] == [
        "step", "occurred_date", "outcome",
    ]
    after = one(
        db_path,
        "SELECT j.revision, e.id, e.created_time, e.outcome, e.step, e.occurred_date, "
        "s.id, s.log_date, q.id, q.updated_time, o.source_step, o.asked_date, "
        "a.id, a.stage, a.current_step, a.revision FROM journal j "
        "JOIN timeline_entries e ON e.user_id=j.user_id AND e.journal_id=j.id "
        "JOIN applications a ON a.user_id=e.user_id AND a.id=e.application_id "
        "JOIN status_log s ON s.user_id=j.user_id AND s.journal_id=j.id "
        "JOIN review_question_occurrences o ON o.user_id=j.user_id AND o.journal_id=j.id "
        "JOIN questions q ON q.user_id=o.user_id AND q.id=o.question_id WHERE j.id=?",
        journal_id,
    )
    assert after[:8] == (
        before[0] + 1,
        before[1],
        before[2],
        "passed",
        "二面",
        "2026-07-02",
        before[3],
        "2026-07-02",
    )
    assert after[8:12] == (before[4], before[5], "二面", "2026-07-02")
    assert after[12:] == (before[6], "interviewing", "二面", before[7] + 1)
    assert one(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id=?",
        operation_id,
    ) == (1,)
    assert edit_operations.get_review_timeline_entry_edit_operation(
        db_path, "u1", operation_id,
    ) == completed
    assert edit_operations.list_review_timeline_entry_edit_operations_for_turn(
        db_path, "u1", turn_id,
    ) == [completed]


def test_parallel_same_operation_applies_once(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    before_revision = one(db_path, "SELECT revision FROM journal WHERE id=?", journal_id)[0]
    operation_id, turn_id = str(uuid4()), str(uuid4())

    def worker(_index: int):
        return execute(
            db_path,
            operation_id=operation_id,
            turn_id=turn_id,
            changes={"step": "二面"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    assert all(result == results[0] for result in results)
    assert one(db_path, "SELECT revision FROM journal WHERE id=?", journal_id) == (
        before_revision + 1,
    )
    assert one(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id=?",
        operation_id,
    ) == (1,)


def test_tool_hides_ids_reuses_command_identity_and_rejects_bad_direct_inputs(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    before_revision = one(db_path, "SELECT revision FROM journal WHERE id=?", journal_id)[0]
    tool = ManageReviewTool(db_path, "u1", client_turn_id=uuid4())
    parameter_names = {parameter.name for parameter in tool.get_parameters()}
    assert "operation_id" not in parameter_names
    assert "client_turn_id" not in parameter_names

    first = tool.run({"action": "edit_timeline_entry", "new_step": "二面"})
    replay = tool.run({"action": "edit_timeline_entry", "new_step": "二面"})
    invalid_step = tool.run({"action": "edit_timeline_entry", "new_step": True})
    invalid_date = tool.run({
        "action": "edit_timeline_entry", "new_date": "2026-02-30",
    })

    assert first.status == replay.status == "success"
    assert first.data == replay.data
    assert invalid_step.status == invalid_date.status == "error"
    assert one(db_path, "SELECT revision FROM journal WHERE id=?", journal_id) == (
        before_revision + 1,
    )
    assert one(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' "
        "AND operation_id IS NOT NULL",
    ) == (1,)


def test_edit_locates_review_across_internal_space(tmp_path):
    db_path = make_db(tmp_path)
    record_review(
        db_path, company="字节", position="AI应用工程师",
        step="一面", with_details=False,
    )
    result = execute(
        db_path,
        company="字节",
        position="AI 应用工程师",
        changes={"step": "二面"},
    )
    assert result.get("status") != "not_found"


def test_not_found_ambiguous_and_no_change_are_zero_write(tmp_path):
    db_path = make_db(tmp_path)
    first = record_review(
        db_path,
        company="A",
        position="P1",
        stage="applied",
        step="提交申请",
        with_details=False,
    )
    record_review(
        db_path,
        company="A",
        position="P2",
        stage="applied",
        step="提交申请",
        with_details=False,
    )
    first_revision = one(db_path, "SELECT revision FROM journal WHERE id=?", first)[0]

    assert execute(db_path, company="missing")["status"] == "not_found"
    assert execute(db_path, company="A", position=None)["status"] == "ambiguous"
    assert execute(
        db_path,
        company="A",
        position="P1",
        changes={"step": "提交申请"},
    )["status"] == "no_change"
    assert one(db_path, "SELECT revision FROM journal WHERE id=?", first) == (first_revision,)
    assert one(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id IS NOT NULL",
    ) == (0,)


def test_operation_identity_conflicts_and_does_not_leak_other_tenant(tmp_path):
    db_path = make_db(tmp_path)
    record_review(db_path, user_id="u1", company="A", position="P", with_details=False)
    record_review(db_path, user_id="u2", company="B", position="P", with_details=False)
    operation_id, turn_id = str(uuid4()), str(uuid4())
    execute(db_path, operation_id=operation_id, turn_id=turn_id)

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(
            db_path,
            operation_id=operation_id,
            turn_id=turn_id,
            changes={"step": "三面"},
        )
    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationNotFound):
        execute(
            db_path,
            operation_id=operation_id,
            turn_id=turn_id,
            user_id="u2",
            company="B",
        )
    assert edit_operations.get_review_timeline_entry_edit_operation(
        db_path, "u2", operation_id,
    ) is None
    assert edit_operations.list_review_timeline_entry_edit_operations_for_turn(
        db_path, "u2", turn_id,
    ) == []


def test_operation_id_rejects_turn_rebinding_and_other_operation_type(tmp_path):
    db_path = make_db(tmp_path)
    record_review(db_path, with_details=False)
    operation_id, turn_id = str(uuid4()), str(uuid4())
    execute(db_path, operation_id=operation_id, turn_id=turn_id)

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(
            db_path,
            operation_id=operation_id,
            turn_id=str(uuid4()),
            changes={"step": "二面"},
        )

    application_operation_id = str(uuid4())
    execute_application_update_operation(
        db_path,
        "u1",
        operation_id=application_operation_id,
        client_turn_id=uuid4(),
        company="A",
        position="P",
        changes={"stage": "offer"},
    )
    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(
            db_path,
            operation_id=application_operation_id,
            changes={"step": "三面"},
        )


def test_turn_locator_ignores_a_canonical_other_operation_family(tmp_path):
    db_path = make_db(tmp_path)
    record_review(db_path, with_details=False)
    turn_id = str(uuid4())
    execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=turn_id,
        company="A",
        position="P",
        changes={"stage": "offer"},
    )

    completed = execute(db_path, turn_id=turn_id, changes={"step": "二面"})

    assert edit_operations.list_review_timeline_entry_edit_operations_for_turn(
        db_path,
        "u1",
        turn_id,
    ) == [completed]


@pytest.mark.parametrize("damage", ["dual_family_key", "derivation_turn"])
def test_damaged_preference_turn_candidate_blocks_timeline_edit_without_writes(
    tmp_path,
    damage,
):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    turn_id = str(uuid4())
    preference_operation_id = str(uuid4())
    execute_preference_update_operation(
        db_path,
        "u1",
        operation_id=preference_operation_id,
        client_turn_id=turn_id,
        changes=[{"op": "set", "key": "城市", "value": "哥本哈根"}],
    )
    with transaction(db_path) as conn:
        if damage == "dual_family_key":
            conn.execute(
                "UPDATE journal SET derivation_json=json_set(derivation_json, "
                "'$.operation.type', 'preference_update') WHERE operation_id=?",
                (preference_operation_id,),
            )
        else:
            conn.execute(
                "UPDATE journal SET derivation_json=json_set(derivation_json, "
                "'$.operation.client_turn_id', ?) WHERE operation_id=?",
                (str(uuid4()), preference_operation_id),
            )
    before = one(
        db_path,
        "SELECT (SELECT revision FROM journal WHERE id=?), "
        "(SELECT step FROM timeline_entries WHERE journal_id=?), COUNT(*) FROM journal",
        journal_id,
        journal_id,
    )

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(db_path, turn_id=turn_id, changes={"step": "二面"})

    assert one(
        db_path,
        "SELECT (SELECT revision FROM journal WHERE id=?), "
        "(SELECT step FROM timeline_entries WHERE journal_id=?), COUNT(*) FROM journal",
        journal_id,
        journal_id,
    ) == before


def test_turn_candidate_budget_blocks_timeline_edit_write(tmp_path, monkeypatch):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    turn_id = str(uuid4())
    for changes in ({"stage": "offer"}, {"stage": "rejected"}):
        execute_application_update_operation(
            db_path,
            "u1",
            operation_id=uuid4(),
            client_turn_id=turn_id,
            company="A",
            position="P",
            changes=changes,
        )
    monkeypatch.setattr(edit_operations.validate, "MAX_TURN_OPERATION_CANDIDATES", 1)
    before = one(
        db_path,
        "SELECT (SELECT revision FROM journal WHERE id=?), "
        "(SELECT step FROM timeline_entries WHERE journal_id=?), COUNT(*) FROM journal",
        journal_id,
        journal_id,
    )

    with pytest.raises(
        edit_operations.ReviewTimelineEntryEditOperationConflict,
        match="候选超过安全上限",
    ):
        execute(db_path, turn_id=turn_id, changes={"step": "三面"})

    assert one(
        db_path,
        "SELECT (SELECT revision FROM journal WHERE id=?), "
        "(SELECT step FROM timeline_entries WHERE journal_id=?), COUNT(*) FROM journal",
        journal_id,
        journal_id,
    ) == before


@pytest.mark.parametrize(
    "damage",
    [
        "kind",
        "extraction_family",
        "derivation_family",
        "both_families",
        "extraction_turn",
        "derivation_turn",
    ],
)
def test_operation_locator_damage_fails_closed_without_second_turn_write(
    tmp_path,
    damage,
):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    operation_id, turn_id = str(uuid4()), str(uuid4())
    execute(
        db_path,
        operation_id=operation_id,
        turn_id=turn_id,
        changes={"step": "二面"},
    )
    with transaction(db_path) as conn:
        if damage == "kind":
            conn.execute(
                "UPDATE journal SET kind='review' WHERE operation_id=?",
                (operation_id,),
            )
        elif damage == "extraction_family":
            conn.execute(
                "UPDATE journal SET extraction_json=json_set(extraction_json, "
                "'$.operation_type', 'review_record') WHERE operation_id=?",
                (operation_id,),
            )
        elif damage == "derivation_family":
            conn.execute(
                "UPDATE journal SET derivation_json=json_set(derivation_json, "
                "'$.operation.type', 'review_record') WHERE operation_id=?",
                (operation_id,),
            )
        elif damage == "both_families":
            conn.execute(
                "UPDATE journal SET "
                "extraction_json=json_set(extraction_json, '$.operation_type', 'unknown'), "
                "derivation_json=json_set(derivation_json, '$.operation.type', 'unknown') "
                "WHERE operation_id=?",
                (operation_id,),
            )
        elif damage == "extraction_turn":
            conn.execute(
                "UPDATE journal SET extraction_json=json_set(extraction_json, "
                "'$.client_turn_id', ?) WHERE operation_id=?",
                (str(uuid4()), operation_id),
            )
        else:
            conn.execute(
                "UPDATE journal SET derivation_json=json_set(derivation_json, "
                "'$.operation.client_turn_id', ?) WHERE operation_id=?",
                (str(uuid4()), operation_id),
            )

    before = one(
        db_path,
        "SELECT (SELECT revision FROM journal WHERE id=?), "
        "(SELECT step FROM timeline_entries WHERE journal_id=?), "
        "COUNT(*) FROM journal",
        journal_id,
        journal_id,
    )
    if damage == "kind":
        assert edit_operations.get_review_timeline_entry_edit_operation(
            db_path, "u1", operation_id,
        ) is None
    else:
        with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
            edit_operations.get_review_timeline_entry_edit_operation(
                db_path, "u1", operation_id,
            )
    assert edit_operations.get_review_timeline_entry_edit_operation(
        db_path,
        "other",
        operation_id,
    ) is None
    if damage in {"kind", "extraction_family", "both_families", "extraction_turn"}:
        assert edit_operations.list_review_timeline_entry_edit_operations_for_turn(
            db_path, "u1", turn_id,
        ) == []
    else:
        with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
            edit_operations.list_review_timeline_entry_edit_operations_for_turn(
                db_path,
                "u1",
                turn_id,
            )
    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(
            db_path,
            operation_id=str(uuid4()),
            turn_id=turn_id,
            changes={"step": "三面"},
        )
    assert one(
        db_path,
        "SELECT (SELECT revision FROM journal WHERE id=?), "
        "(SELECT step FROM timeline_entries WHERE journal_id=?), "
        "COUNT(*) FROM journal",
        journal_id,
        journal_id,
    ) == before


@pytest.mark.parametrize("damage", ["application_identity", "journal_created_time"])
def test_default_locator_never_skips_a_damaged_latest_review(tmp_path, damage):
    db_path = make_db(tmp_path)
    older = record_review(
        db_path, company="A", position="P", with_details=False,
    )
    latest = record_review(
        db_path, company="B", position="P", with_details=False,
    )
    with transaction(db_path) as conn:
        if damage == "application_identity":
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "UPDATE applications SET position='   ' "
                "WHERE user_id='u1' AND company='B'",
            )
        else:
            conn.execute("UPDATE journal SET created_time='' WHERE id=?", (latest,))

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(db_path, company=None, position=None, changes={"step": "二面"})
    assert one(db_path, "SELECT step FROM timeline_entries WHERE journal_id=?", older) == ("一面",)
    assert one(db_path, "SELECT step FROM timeline_entries WHERE journal_id=?", latest) == ("一面",)


def test_partial_locator_never_skips_a_corrupt_matching_latest_review(tmp_path):
    db_path = make_db(tmp_path)
    older = record_review(
        db_path, company="B", position="old", with_details=False,
    )
    latest = record_review(
        db_path, company="B", position="new", with_details=False,
    )
    with transaction(db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE applications SET company='   ' "
            "WHERE id=(SELECT application_id FROM timeline_entries WHERE journal_id=?)",
            (latest,),
        )

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(db_path, company="B", position=None, changes={"step": "二面"})
    assert one(db_path, "SELECT step FROM timeline_entries WHERE journal_id=?", older) == ("一面",)
    assert one(db_path, "SELECT step FROM timeline_entries WHERE journal_id=?", latest) == ("一面",)


@pytest.mark.parametrize(
    ("company", "position"),
    [(None, None), ("A", "P"), ("A", None)],
    ids=["no-selector", "full-selector", "partial-selector"],
)
def test_locator_bounds_identities_not_same_identity_review_history(
    tmp_path,
    company,
    position,
):
    db_path = make_db(tmp_path)
    journal_ids = [
        record_review(db_path, with_details=False)
        for _index in range(101)
    ]

    completed = execute(
        db_path,
        company=company,
        position=position,
        changes={"step": "二面"},
    )

    assert completed["state"] == "completed"
    assert completed["target"]["journal_id"] == journal_ids[-1]
    assert one(
        db_path,
        "SELECT COUNT(*) FROM timeline_entries WHERE journal_id != ? AND step = '一面'",
        journal_ids[-1],
    ) == (100,)


def test_later_manual_entry_blocks_implicit_current_step_rewrite(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    application_id = one(
        db_path, "SELECT application_id FROM timeline_entries WHERE journal_id=?", journal_id,
    )[0]
    with transaction(db_path) as conn:
        revision = conn.execute(
            "SELECT revision FROM applications WHERE user_id='u1' AND id=?",
            (application_id,),
        ).fetchone()[0]
        apply_application_progress_in_transaction(
            conn,
            "u1",
            application_id,
            expected_revision=revision,
            step="三面",
            occurred_date="2026-07-03",
            outcome=None,
            summary="后来手工记录",
            update_current_state=True,
            target_stage="interviewing",
            target_step="三面",
            timestamp=now_iso(),
        )

    completed = execute(db_path, changes={"step": "二面"})

    assert completed["state"] == "completed"
    assert completed["effect"]["application_before"]["stage"] == "interviewing"
    assert completed["effect"]["application_before"]["current_step"] == "三面"
    assert completed["effect"]["application_final"]["current_step"] == "三面"
    assert one(
        db_path, "SELECT stage, current_step FROM applications WHERE id=?", application_id,
    ) == ("interviewing", "三面")


def test_editing_stage_only_review_fact_never_promotes_it_to_current_step(tmp_path):
    db_path = make_db(tmp_path)
    extraction = {
        "company": "A",
        "position": "P",
        "channel": None,
        "history": {
            "step": "参加宣讲会",
            "date": "2026-07-01",
            "outcome": None,
            "summary": "已参加宣讲会",
        },
        "projected_state": {"stage": "applied", "current_step": None},
        "clear_next_action": False,
        "next_action": None,
        "questions": [],
        "mood": None,
        "time_of_day": None,
        "factors": [],
    }
    journal = append_review(db_path, "u1", "A P 已参加宣讲会")
    derive_review_for_test(db_path, "u1", journal["id"], extraction)

    completed = execute(db_path, changes={"step": "参加线上宣讲会"})

    assert completed["state"] == "completed"
    assert completed["effect"]["application_before"]["current_step"] is None
    assert completed["effect"]["application_final"]["current_step"] is None
    assert one(
        db_path,
        "SELECT e.step, e.to_step, a.current_step FROM timeline_entries e "
        "JOIN applications a ON a.id=e.application_id WHERE e.journal_id=?",
        journal["id"],
    ) == ("参加线上宣讲会", None, None)


def test_edit_never_repairs_a_drifted_application_step_projection(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET current_step='异常环节' "
            "WHERE id=(SELECT application_id FROM timeline_entries WHERE journal_id=?)",
            (journal_id,),
        )

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(db_path, changes={"occurred_date": "2026-07-02"})
    assert one(
        db_path,
        "SELECT e.occurred_date, a.current_step FROM timeline_entries e JOIN applications a "
        "ON a.user_id=e.user_id AND a.id=e.application_id WHERE e.journal_id=?",
        journal_id,
    ) == ("2026-07-01", "异常环节")


def test_shared_knowledge_and_reverse_id_order_are_valid_dependencies(tmp_path):
    db_path = make_db(tmp_path)
    stamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO knowledge_points (user_id, name, created_time, updated_time) "
            "VALUES ('u1', '先创建', ?, ?), ('u1', '后创建', ?, ?)",
            (stamp, stamp, stamp, stamp),
        )
    extraction = {
        "company": "A",
        "position": "P",
        "channel": None,
        "history": {
            "step": "一面",
            "date": "2026-07-01",
            "outcome": None,
            "summary": "共享知识点",
        },
        "projected_state": {"stage": "interviewing", "current_step": "一面"},
        "next_action": None,
        "questions": [
            {
                "text": "第一题？",
                "stuck": False,
                "knowledge_points": ["后创建", "先创建"],
            },
            {
                "text": "第二题？",
                "stuck": True,
                "knowledge_points": ["先创建"],
            },
        ],
        "mood": None,
        "time_of_day": None,
        "factors": [],
    }
    created = append_review(db_path, "u1", extraction["history"]["summary"])
    derive_review_for_test(db_path, "u1", created["id"], extraction)

    completed = execute(db_path, changes={"step": "二面"})

    assert completed["state"] == "completed"
    assert completed["result"]["apply"]["occurrences_updated"] == 2
    assert one(db_path, "SELECT COUNT(*) FROM knowledge_points") == (2,)
    assert one(db_path, "SELECT COUNT(*) FROM question_knowledge") == (3,)


def test_approved_application_merge_keeps_review_editable_in_place(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(
        db_path,
        company="Source",
        position="P1",
        with_details=True,
    )
    source_application_id = one(
        db_path,
        "SELECT application_id FROM timeline_entries WHERE journal_id=?",
        journal_id,
    )[0]
    timestamp = now_iso()
    with transaction(db_path) as conn:
        destination_application_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'Destination', 'P2', ?, ?)",
            (timestamp, timestamp),
        ).lastrowid
    merge = application_operations.prepare_application_merge_operation(
        db_path,
        "u1",
        source_application_id=source_application_id,
        source_company="Source",
        source_position="P1",
        destination_application_id=destination_application_id,
        destination_company="Destination",
        destination_position="P2",
    )
    approved = application_operations.approve_application_merge_operation(
        db_path,
        "u1",
        merge["operation_id"],
    )
    assert approved["state"] == "completed"
    assert one(
        db_path,
        "SELECT json_extract(derivation_json, '$.application_id'), "
        "(SELECT application_id FROM timeline_entries WHERE journal_id=journal.id) "
        "FROM journal WHERE id=?",
        journal_id,
    ) == (source_application_id, destination_application_id)

    completed = execute(
        db_path,
        company="Destination",
        position="P2",
        changes={"step": "二面"},
    )

    assert completed["state"] == "completed"
    assert completed["target"]["application_id"] == destination_application_id
    assert one(
        db_path,
        "SELECT e.step, o.source_step, o.application_id, o.company "
        "FROM timeline_entries e JOIN review_question_occurrences o "
        "ON o.user_id=e.user_id AND o.journal_id=e.journal_id "
        "WHERE e.journal_id=?",
        journal_id,
    ) == ("二面", "二面", destination_application_id, "Destination")


def test_profile_rename_keeps_review_timeline_entry_editable(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, company="A", position="P", with_details=True)
    application_id = one(
        db_path,
        "SELECT application_id FROM timeline_entries WHERE journal_id=?",
        journal_id,
    )[0]
    renamed = execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company="A",
        position="P",
        changes={"company": "A 新名", "position": "P 高级"},
    )
    assert renamed["state"] == "completed"

    completed = execute(
        db_path,
        company="A 新名",
        position="P 高级",
        changes={"step": "二面"},
    )

    assert completed["state"] == "completed"
    assert completed["target"]["application_id"] == application_id
    assert completed["target"]["company"] == "A 新名"
    assert completed["target"]["position"] == "P 高级"
    assert one(
        db_path,
        "SELECT e.step, a.company, a.position, o.company, o.source_step "
        "FROM timeline_entries e JOIN applications a "
        "ON a.user_id=e.user_id AND a.id=e.application_id "
        "JOIN review_question_occurrences o "
        "ON o.user_id=e.user_id AND o.journal_id=e.journal_id "
        "WHERE e.journal_id=?",
        journal_id,
    ) == ("二面", "A 新名", "P 高级", "A 新名", "二面")


def test_undo_is_in_place_idempotent_and_command_is_bound(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path)
    entry_id = one(db_path, "SELECT id FROM timeline_entries WHERE journal_id=?", journal_id)[0]
    operation_id = str(uuid4())
    completed = execute(
        db_path,
        operation_id=operation_id,
        changes={"step": "二面", "occurred_date": "2026-07-02"},
    )
    command_id = str(uuid4())

    undone = edit_operations.undo_review_timeline_entry_edit_operation(
        db_path, "u1", operation_id, command_id=command_id,
    )
    replay = edit_operations.undo_review_timeline_entry_edit_operation(
        db_path, "u1", operation_id, command_id=command_id,
    )

    assert replay == undone
    assert undone["state"] == "undone"
    assert undone["result"]["apply"] == completed["result"]["apply"]
    assert undone["result"]["undo"] is not None
    assert one(
        db_path,
        "SELECT id, step, occurred_date FROM timeline_entries WHERE journal_id=?",
        journal_id,
    ) == (entry_id, "一面", "2026-07-01")
    status = edit_operations.get_review_timeline_entry_edit_undo_command_status(
        db_path, "u1", command_id,
    )
    assert status["state"] == "completed" and status["operation_id"] == operation_id

    second = execute(db_path, changes={"step": "三面"})
    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        edit_operations.undo_review_timeline_entry_edit_operation(
            db_path,
            "u1",
            second["operation_id"],
            command_id=command_id,
        )


def test_parallel_undo_has_one_business_effect_and_one_command_receipt(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    operation = execute(db_path, changes={"step": "二面"})
    command_id = str(uuid4())

    def worker(_index: int):
        return edit_operations.undo_review_timeline_entry_edit_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    assert all(result == results[0] for result in results)
    assert results[0]["state"] == "undone"
    assert one(db_path, "SELECT step FROM timeline_entries WHERE journal_id=?", journal_id) == ("一面",)
    assert one(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE operation_id=? AND kind='correction' "
        "AND json_extract(extraction_json, '$.operation_type')="
        "'review_timeline_entry_edit_undo'",
        command_id,
    ) == (1,)


def test_corrupt_completed_undo_command_envelope_fails_closed(tmp_path):
    db_path = make_db(tmp_path)
    record_review(db_path, with_details=False)
    operation = execute(db_path, changes={"step": "二面"})
    command_id = str(uuid4())
    edit_operations.undo_review_timeline_entry_edit_operation(
        db_path,
        "u1",
        operation["operation_id"],
        command_id=command_id,
    )
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET derivation_json='{}' "
            "WHERE user_id='u1' AND operation_id=?",
            (command_id,),
        )

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        edit_operations.get_review_timeline_entry_edit_undo_command_status(
            db_path,
            "u1",
            command_id,
        )
    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        edit_operations.undo_review_timeline_entry_edit_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )


def test_drift_rejection_is_persisted_and_replayed(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    operation = execute(db_path, changes={"step": "二面"})
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE timeline_entries SET summary='后来补充的新事实' WHERE user_id='u1' AND journal_id=?",
            (journal_id,),
        )
    command_id = str(uuid4())

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        edit_operations.undo_review_timeline_entry_edit_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
    rejected = edit_operations.get_review_timeline_entry_edit_undo_command_status(
        db_path, "u1", command_id,
    )
    assert rejected["state"] == "rejected" and rejected["terminal"] is True
    assert rejected["error"]["code"] == "target_changed"
    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        edit_operations.undo_review_timeline_entry_edit_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
    assert edit_operations.get_review_timeline_entry_edit_undo_command_status(
        db_path, "u1", command_id,
    ) == rejected


@pytest.mark.parametrize("drift", ["missing", "entry_aba", "cross_tenant"])
def test_target_identity_drift_never_allows_old_undo(tmp_path, drift):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    operation = execute(db_path, changes={"step": "二面"})
    entry = one(
        db_path,
        "SELECT id, user_id, application_id, step, occurred_date, outcome, summary, "
        "from_stage, from_step, to_stage, to_step, source, journal_id "
        "FROM timeline_entries WHERE user_id='u1' AND journal_id=?",
        journal_id,
    )
    with transaction(db_path) as conn:
        if drift == "missing":
            conn.execute(
                "UPDATE applications SET current_state_entry_id=NULL WHERE id=?",
                (entry[2],),
            )
            conn.execute("DELETE FROM timeline_entries WHERE id=?", (entry[0],))
        elif drift == "cross_tenant":
            conn.execute("UPDATE timeline_entries SET user_id='u2' WHERE id=?", (entry[0],))
        else:
            conn.execute(
                "UPDATE applications SET current_state_entry_id=NULL WHERE id=?",
                (entry[2],),
            )
            conn.execute("DELETE FROM timeline_entries WHERE id=?", (entry[0],))
            conn.execute(
                "INSERT INTO timeline_entries (id, user_id, application_id, step, "
                "occurred_date, outcome, summary, from_stage, from_step, to_stage, to_step, "
                "source, journal_id, created_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'recreated-entry')",
                entry,
            )
            conn.execute(
                "UPDATE applications SET current_state_entry_id=? WHERE id=?",
                (entry[0], entry[2]),
            )
    command_id = str(uuid4())

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        edit_operations.undo_review_timeline_entry_edit_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
    status = edit_operations.get_review_timeline_entry_edit_undo_command_status(
        db_path, "u1", command_id,
    )
    assert status["state"] == "rejected"
    assert status["error"]["code"] in {
        "target_missing", "target_changed", "provenance_changed",
    }


def test_corrupt_operation_receipt_persists_operation_invalid_rejection(tmp_path):
    db_path = make_db(tmp_path)
    record_review(db_path, with_details=False)
    operation = execute(db_path, changes={"step": "二面"})
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET derivation_json='{}' WHERE operation_id=?",
            (operation["operation_id"],),
        )
    command_id = str(uuid4())

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        edit_operations.undo_review_timeline_entry_edit_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
    rejected = edit_operations.get_review_timeline_entry_edit_undo_command_status(
        db_path, "u1", command_id,
    )
    assert rejected["state"] == "rejected"
    assert rejected["error"]["code"] == "operation_invalid"


def test_per_turn_operation_budget_is_hard_bounded(tmp_path):
    db_path = make_db(tmp_path)
    record_review(db_path, with_details=False)
    turn_id = str(uuid4())
    current_step = "一面"
    for _index in range(20):
        current_step = "二面" if current_step == "一面" else "一面"
        completed = execute(
            db_path,
            turn_id=turn_id,
            changes={"step": current_step},
        )
        assert completed["state"] == "completed"

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict, match="安全上限"):
        execute(
            db_path,
            turn_id=turn_id,
            changes={"step": "二面" if current_step == "一面" else "一面"},
        )
    assert len(edit_operations.list_review_timeline_entry_edit_operations_for_turn(
        db_path, "u1", turn_id,
    )) == 20


def test_unexpected_trigger_write_rolls_back_everything(tmp_path):
    db_path = make_db(tmp_path)
    journal_id = record_review(db_path, with_details=False)
    before = one(db_path, "SELECT revision FROM journal WHERE id=?", journal_id)[0]
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER review_edit_collateral AFTER UPDATE OF step ON timeline_entries "
            "BEGIN INSERT OR REPLACE INTO meta(key, value) VALUES "
            "('unexpected_review_edit_write', '1'); END",
        )

    with pytest.raises(edit_operations.ReviewTimelineEntryEditOperationConflict):
        execute(db_path, changes={"step": "二面"})
    assert one(db_path, "SELECT revision FROM journal WHERE id=?", journal_id) == (before,)
    assert one(db_path, "SELECT step FROM timeline_entries WHERE journal_id=?", journal_id) == ("一面",)
    assert one(
        db_path,
        "SELECT value FROM meta WHERE key='unexpected_review_edit_write'",
    ) is None
    assert one(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id IS NOT NULL",
    ) == (0,)


def test_http_recovery_and_stable_undo_contract(client):
    test_client, db_path = client
    record_review(db_path, user_id="me", with_details=False)
    operation_id, turn_id = str(uuid4()), str(uuid4())
    operation = execute(
        db_path,
        user_id="me",
        operation_id=operation_id,
        turn_id=turn_id,
    )
    base = f"/api/reviews/timeline-entry-edit-operations/{operation_id}"

    assert test_client.get(base).json() == operation
    assert test_client.get(
        f"/api/reviews/timeline-entry-edit-operations/by-client-turn/{turn_id}",
    ).json() == {"operations": [operation]}
    assert test_client.get(base, headers={"Remote-User": "other"}).status_code == 404
    assert test_client.post(base + "/undo").status_code == 422
    assert test_client.post(base + "/undo", json={"confirmed": True}).status_code == 422

    command_id = str(uuid4())
    command_base = f"/api/reviews/timeline-entry-edit-undo-commands/{command_id}"
    assert test_client.get(command_base).json()["state"] == "absent"
    undone = test_client.post(base + "/undo", json={"command_id": command_id})
    assert undone.status_code == 200 and undone.json()["state"] == "undone"
    assert test_client.post(
        base + "/undo", json={"command_id": command_id},
    ).json() == undone.json()
    assert test_client.get(command_base).json()["state"] == "completed"


def test_http_contract_publishes_versioned_openapi_models(client):
    test_client, _ = client
    openapi = test_client.app.openapi()
    paths = openapi["paths"]
    list_schema = paths[
        "/api/reviews/timeline-entry-edit-operations/by-client-turn/{client_turn_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    item_schema = paths[
        "/api/reviews/timeline-entry-edit-operations/{operation_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    undo_schema = paths[
        "/api/reviews/timeline-entry-edit-operations/{operation_id}/undo"
    ]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    command_schema = paths[
        "/api/reviews/timeline-entry-edit-undo-commands/{command_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert list_schema == {
        "$ref": "#/components/schemas/ReviewTimelineEntryEditOperationsResponse",
    }
    expected_item = {"$ref": "#/components/schemas/ReviewTimelineEntryEditOperationDTO"}
    assert item_schema == expected_item
    assert undo_schema == expected_item
    assert command_schema == {
        "$ref": "#/components/schemas/ReviewTimelineEntryEditUndoCommandStatus",
    }
    operation_schema = openapi["components"]["schemas"]["ReviewTimelineEntryEditOperationDTO"]
    assert operation_schema["properties"]["contract_version"]["const"] == 1
    undo_request = openapi["components"]["schemas"]["ReviewTimelineEntryEditUndoRequest"]
    assert undo_request["required"] == ["command_id"]
    assert undo_request["additionalProperties"] is False
