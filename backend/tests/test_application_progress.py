"""Clean-slate application progress state machine regression tests."""

import pytest

from careerdesk.features.applications import repository
from careerdesk.platform.database import init_db, read_connection


@pytest.fixture
def progress_db(tmp_path) -> str:
    path = str(tmp_path / "progress.db")
    init_db(path)
    return path


def create_application(progress_db: str, *, stage: str = "applied") -> int:
    return repository.create_application_profile(
        progress_db,
        "u1",
        company="测试公司",
        position="Agent 工程师",
        department=None,
        channel=None,
        stage=stage,
        current_step="简历筛选",
        next_action={
            "stage": "interviewing",
            "step": "一面",
            "date": "2026-07-23",
            "time": "14:00",
            "note": "腾讯会议",
        },
        jd_text=None,
        timezone_name="Asia/Shanghai",
    )


def test_drag_changes_only_stage_and_closed_stage_clears_next_action(progress_db):
    application_id = create_application(progress_db)

    moved = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        stage="interviewing",
        timezone_name="Asia/Shanghai",
    )
    assert moved is not None
    assert (moved["stage"], moved["current_step"], moved["revision"]) == (
        "interviewing", "简历筛选", 1,
    )
    assert moved["next_action"]["step"] == "一面"
    assert moved["timeline_entries"][-1]["source"] == "drag"

    closed = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=1,
        stage="rejected",
        timezone_name="Asia/Shanghai",
    )
    assert closed is not None
    assert closed["current_step"] == "简历筛选"
    assert closed["next_action"] is None


def test_pooled_preserves_next_action_but_hides_it_from_upcoming(progress_db):
    application_id = create_application(progress_db)

    pooled = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        stage="pooled",
        timezone_name="Asia/Shanghai",
    )
    assert pooled is not None
    assert pooled["paused_from_stage"] == "applied"
    assert pooled["next_action"]["step"] == "一面"
    assert repository.upcoming(
        progress_db, "u1", "2026-07-01", "2026-07-31",
    ) == []

    resumed = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=1,
        stage="applied",
        timezone_name="Asia/Shanghai",
    )
    assert resumed is not None
    assert resumed["paused_from_stage"] is None
    assert [item["id"] for item in repository.upcoming(
        progress_db, "u1", "2026-07-01", "2026-07-31",
    )] == [application_id]


def test_complete_next_action_atomically_promotes_state_and_replaces_plan(progress_db):
    application_id = create_application(progress_db)

    completed = repository.complete_application_next_action(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        occurred_date="2026-07-23",
        outcome="passed",
        summary="一面通过",
        next_action={
            "stage": "interviewing",
            "step": "二面",
            "date": "2026-07-28",
            "time": None,
            "note": None,
        },
        timezone_name="Asia/Shanghai",
    )

    assert completed is not None
    assert (completed["stage"], completed["current_step"]) == ("interviewing", "一面")
    assert completed["next_action"] == {
        "stage": "interviewing",
        "step": "二面",
        "date": "2026-07-28",
        "time": None,
        "note": None,
    }
    assert completed["revision"] == 1
    entry = completed["timeline_entries"][-1]
    assert (entry["step"], entry["outcome"], entry["to_step"]) == (
        "一面", "passed", "一面",
    )


def test_failed_next_action_atomically_moves_to_rejected_and_discards_plan(progress_db):
    application_id = create_application(progress_db)

    completed = repository.complete_application_next_action(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        occurred_date="2026-07-23",
        outcome="failed",
        summary="一面未通过",
        next_action={
            "stage": "interviewing",
            "step": "不应保留的二面",
            "date": "2026-07-28",
            "time": None,
            "note": None,
        },
        timezone_name="Asia/Shanghai",
    )

    assert completed is not None
    assert (completed["stage"], completed["current_step"]) == ("rejected", "一面")
    assert completed["next_action"] is None
    entry = completed["timeline_entries"][-1]
    assert (entry["outcome"], entry["to_stage"], entry["to_step"]) == (
        "failed", "rejected", "一面",
    )


def test_backfilled_history_does_not_overwrite_current_projection(progress_db):
    application_id = create_application(progress_db)

    detail = repository.record_application_progress(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        step="HR 初筛",
        occurred_date="2026-07-01",
        outcome="passed",
        summary="补记早期流程",
        update_current_state=False,
        target_stage=None,
        target_step=None,
        replace_next_action=False,
        next_action=None,
        timezone_name="Asia/Shanghai",
    )

    assert detail is not None
    assert (detail["stage"], detail["current_step"]) == ("applied", "简历筛选")
    assert detail["timeline_entries"][-1]["to_step"] == "简历筛选"


def test_stale_revision_never_overwrites_newer_next_action(progress_db):
    application_id = create_application(progress_db)
    updated = repository.set_application_next_action(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        next_action={
            "stage": "interviewing",
            "step": "一面改期",
            "date": "2026-07-25",
            "time": None,
            "note": None,
        },
        timezone_name="Asia/Shanghai",
    )
    assert updated is not None and updated["revision"] == 1

    with pytest.raises(repository.TimelineMutationConflict):
        repository.move_application_stage(
            progress_db,
            "u1",
            application_id,
            expected_revision=0,
            stage="interviewing",
            timezone_name="Asia/Shanghai",
        )

    authoritative = repository.application_detail(progress_db, "u1", application_id)
    assert authoritative is not None
    assert authoritative["stage"] == "applied"
    assert authoritative["next_action"]["step"] == "一面改期"


def test_editing_current_state_entry_updates_projection_and_delete_restores_from_state(
    progress_db,
):
    application_id = create_application(progress_db)
    detail = repository.application_detail(progress_db, "u1", application_id)
    assert detail is not None
    current_entry = detail["timeline_entries"][-1]

    edited = repository.update_timeline_entry(
        progress_db,
        "u1",
        application_id,
        current_entry["id"],
        expected_revision=0,
        expected_fingerprint=current_entry["snapshot_fingerprint"],
        step="已完成简历筛选",
        occurred_date=current_entry["occurred_date"],
        outcome=None,
        summary=current_entry["summary"],
        timezone_name="Asia/Shanghai",
    )
    assert edited is not None
    assert edited["current_step"] == "已完成简历筛选"

    edited_entry = edited["timeline_entries"][-1]
    restored = repository.delete_timeline_entry(
        progress_db,
        "u1",
        application_id,
        edited_entry["id"],
        expected_revision=1,
        expected_fingerprint=edited_entry["snapshot_fingerprint"],
        timezone_name="Asia/Shanghai",
    )
    assert restored is not None
    assert (restored["stage"], restored["current_step"]) == ("backlog", None)
    assert restored["next_action"]["step"] == "一面"


def test_deleting_current_state_entry_preserves_independently_edited_plan(progress_db):
    application_id = create_application(progress_db)
    moved = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        stage="interviewing",
    )
    assert moved is not None
    transition = moved["timeline_entries"][-1]
    replanned = repository.set_application_next_action(
        progress_db,
        "u1",
        application_id,
        expected_revision=1,
        next_action={
            "stage": "interviewing",
            "step": "二面",
            "date": "2026-07-30",
            "time": None,
            "note": "后来单独调整",
        },
    )
    assert replanned is not None

    restored = repository.delete_timeline_entry(
        progress_db,
        "u1",
        application_id,
        transition["id"],
        expected_revision=2,
        expected_fingerprint=transition["snapshot_fingerprint"],
    )

    assert restored is not None
    assert (restored["stage"], restored["current_step"]) == (
        "applied",
        "简历筛选",
    )
    assert restored["next_action"] == {
        "stage": "interviewing",
        "step": "二面",
        "date": "2026-07-30",
        "time": None,
        "note": "后来单独调整",
    }


def test_deleting_resume_transition_restores_pool_metadata_and_plan(progress_db):
    application_id = create_application(progress_db)
    pooled = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        stage="pooled",
    )
    assert pooled is not None
    resumed = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=1,
        stage="applied",
    )
    assert resumed is not None
    resume_entry = resumed["timeline_entries"][-1]

    restored = repository.delete_timeline_entry(
        progress_db,
        "u1",
        application_id,
        resume_entry["id"],
        expected_revision=2,
        expected_fingerprint=resume_entry["snapshot_fingerprint"],
    )

    assert restored is not None
    assert restored["stage"] == "pooled"
    assert restored["paused_from_stage"] == "applied"
    assert restored["next_action"]["step"] == "一面"


def test_deleting_transition_into_closed_stage_keeps_no_incompatible_plan(progress_db):
    application_id = repository.create_application_profile(
        progress_db,
        "u1",
        company="终态公司",
        position="终态岗位",
        department=None,
        channel=None,
        stage="rejected",
        current_step="未通过",
        next_action=None,
        jd_text=None,
    )
    reopened = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        stage="applied",
    )
    assert reopened is not None
    reopen_entry = reopened["timeline_entries"][-1]
    planned = repository.set_application_next_action(
        progress_db,
        "u1",
        application_id,
        expected_revision=1,
        next_action={
            "stage": "interviewing",
            "step": "补充沟通",
            "date": None,
            "time": None,
            "note": None,
        },
    )
    assert planned is not None

    restored = repository.delete_timeline_entry(
        progress_db,
        "u1",
        application_id,
        reopen_entry["id"],
        expected_revision=2,
        expected_fingerprint=reopen_entry["snapshot_fingerprint"],
    )

    assert restored is not None
    assert restored["stage"] == "rejected"
    assert restored["next_action"] is None


def test_editing_stage_only_drag_entry_never_rewrites_current_step(progress_db):
    application_id = create_application(progress_db)
    moved = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        stage="interviewing",
        timezone_name="Asia/Shanghai",
    )
    assert moved is not None
    drag_entry = moved["timeline_entries"][-1]

    edited = repository.update_timeline_entry(
        progress_db,
        "u1",
        application_id,
        drag_entry["id"],
        expected_revision=1,
        expected_fingerprint=drag_entry["snapshot_fingerprint"],
        step="手动拖到面试",
        occurred_date=drag_entry["occurred_date"],
        outcome=None,
        summary=drag_entry["summary"],
        timezone_name="Asia/Shanghai",
    )

    assert edited is not None
    assert (edited["stage"], edited["current_step"]) == (
        "interviewing",
        "简历筛选",
    )
    assert edited["timeline_entries"][-1]["step"] == "手动拖到面试"
    assert edited["timeline_entries"][-1]["to_step"] == "简历筛选"


def test_deleting_state_chain_never_leaves_projection_pointing_at_wrong_state(progress_db):
    application_id = create_application(progress_db)
    first = repository.application_detail(progress_db, "u1", application_id)
    assert first is not None
    first_entry = first["timeline_entries"][-1]
    second = repository.move_application_stage(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        stage="interviewing",
    )
    assert second is not None
    second_entry = second["timeline_entries"][-1]

    without_first = repository.delete_timeline_entry(
        progress_db,
        "u1",
        application_id,
        first_entry["id"],
        expected_revision=1,
        expected_fingerprint=first_entry["snapshot_fingerprint"],
    )
    assert without_first is not None
    restored = repository.delete_timeline_entry(
        progress_db,
        "u1",
        application_id,
        second_entry["id"],
        expected_revision=2,
        expected_fingerprint=second_entry["snapshot_fingerprint"],
    )

    assert restored is not None
    assert (restored["stage"], restored["current_step"]) == (
        "applied",
        "简历筛选",
    )
    with read_connection(progress_db) as conn:
        (current_state_entry_id,) = conn.execute(
            "SELECT current_state_entry_id FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
    assert current_state_entry_id is None


def test_projection_columns_are_the_only_schedule_truth(progress_db):
    application_id = create_application(progress_db)
    with read_connection(progress_db) as conn:
        row = conn.execute(
            "SELECT stage, current_step, next_stage, next_step, next_date, next_time, "
            "next_note, revision FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
    assert row == (
        "applied", "简历筛选", "interviewing", "一面", "2026-07-23",
        "14:00", "腾讯会议", 0,
    )


def test_profile_save_only_invalidates_prep_when_semantic_inputs_change(progress_db):
    application_id = create_application(progress_db)
    last_success = {"snapshot_version": 1, "semantic_claim_hash": "old"}
    unrelated_artifact = {"input_fingerprint": "old"}
    assert repository.set_prep_status(
        progress_db,
        "u1",
        application_id,
        "ready",
        prep={
            "position_report": {"summary": "保留我"},
            "research_snapshot": last_success,
            "unrelated_artifact": unrelated_artifact,
        },
    )

    assert repository.update_application_profile(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        company="测试公司",
        position="Agent 工程师",
        department=None,
        channel="内推",
        stage="interviewing",
        current_step="一面",
        next_action={
            "stage": "interviewing",
            "step": "二面",
            "date": "2026-07-28",
            "time": None,
            "note": None,
        },
        jd_text=None,
    )
    preserved = repository.application_detail(progress_db, "u1", application_id)
    assert preserved is not None
    assert preserved["prep_status"] == "ready"
    assert preserved["prep"] == {
        "position_report": {"summary": "保留我"},
        "research_snapshot": last_success,
        "unrelated_artifact": unrelated_artifact,
    }

    assert repository.update_application_profile(
        progress_db,
        "u1",
        application_id,
        expected_revision=1,
        company="测试公司",
        position="Agent 工程师",
        department="平台工程",
        channel="内推",
        stage="interviewing",
        current_step="一面",
        next_action=preserved["next_action"],
        jd_text=None,
    )
    invalidated = repository.application_detail(progress_db, "u1", application_id)
    assert invalidated is not None
    assert invalidated["prep_status"] == "none"
    assert invalidated["prep"] == {
        "research_snapshot": last_success,
        "unrelated_artifact": unrelated_artifact,
    }


def test_create_and_profile_rename_keep_company_identity_bound(progress_db):
    application_id = create_application(progress_db)
    with read_connection(progress_db) as conn:
        assert conn.execute(
            "SELECT application.company, company.name "
            "FROM applications application JOIN companies company "
            "ON company.id = application.company_id WHERE application.id = ?",
            (application_id,),
        ).fetchone() == ("测试公司", "测试公司")

    detail = repository.application_detail(progress_db, "u1", application_id)
    assert detail is not None
    assert repository.update_application_profile(
        progress_db,
        "u1",
        application_id,
        expected_revision=0,
        company="新测试公司",
        position=detail["position"],
        department=detail["department"],
        channel=detail["channel"],
        stage=detail["stage"],
        current_step=detail["current_step"],
        next_action=detail["next_action"],
        jd_text=detail["jd_text"],
    )
    with read_connection(progress_db) as conn:
        assert conn.execute(
            "SELECT application.company, company.name "
            "FROM applications application JOIN companies company "
            "ON company.id = application.company_id WHERE application.id = ?",
            (application_id,),
        ).fetchone() == ("新测试公司", "新测试公司")
