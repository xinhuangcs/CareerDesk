
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.agentic.tools.manage_timeline import UpdateApplicationTool
from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.features.applications import operations
from careerdesk.features.applications.operations import merge as merge_operations


NOW = "2026-07-13T20:00:00+00:00"
UPDATE_TURN_ID = "00000000-0000-4000-8000-000000000106"


def make_db(tmp_path) -> str:
    path = str(tmp_path / "application-merge.db")
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


def add_company(conn, user_id: str, name: str) -> int:
    return conn.execute(
        "INSERT INTO companies (user_id, name, created_time, updated_time) VALUES (?, ?, ?, ?)",
        (user_id, name, NOW, NOW),
    ).lastrowid


def add_application(conn, user_id: str, company: str, position: str, **values) -> int:
    defaults = {
        "company_id": None,
        "department": None,
        "channel": None,
        "jd_text": None,
        "jd_parsed_json": None,
        "stage": "backlog",
        "current_step": None,
        "current_state_entry_id": None,
        "priority": None,
        "resume_id": None,
        "applied_date": None,
        "next_stage": None,
        "next_step": None,
        "next_date": None,
        "next_time": None,
        "next_note": None,
        "paused_from_stage": None,
        "pause_reason": None,
        "application_note": None,
        "prep_status": "none",
        "prep_generation": None,
        "prep_heartbeat_time": None,
        "prep_json": None,
        "revision": 0,
        "created_time": NOW,
        "updated_time": NOW,
    }
    defaults.update(values)
    return conn.execute(
        "INSERT INTO applications (user_id, company, company_id, position, department, "
        "channel, jd_text, jd_parsed_json, stage, current_step, current_state_entry_id, "
        "priority, resume_id, applied_date, next_stage, next_step, next_date, next_time, "
        "next_note, paused_from_stage, pause_reason, application_note, prep_status, "
        "prep_generation, prep_heartbeat_time, prep_json, revision, created_time, updated_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)",
        (
            user_id, company, defaults["company_id"], position, defaults["department"],
            defaults["channel"], defaults["jd_text"], defaults["jd_parsed_json"],
            defaults["stage"], defaults["current_step"],
            defaults["current_state_entry_id"], defaults["priority"],
            defaults["resume_id"], defaults["applied_date"], defaults["next_stage"],
            defaults["next_step"], defaults["next_date"], defaults["next_time"],
            defaults["next_note"], defaults["paused_from_stage"],
            defaults["pause_reason"], defaults["application_note"], defaults["prep_status"],
            defaults["prep_generation"], defaults["prep_heartbeat_time"],
            defaults["prep_json"], defaults["revision"], defaults["created_time"],
            defaults["updated_time"],
        ),
    ).lastrowid


def add_journal(conn, user_id: str = "u1", content: str = "复盘") -> int:
    return conn.execute(
        "INSERT INTO journal (user_id, kind, content, created_time, state) "
        "VALUES (?, 'review', ?, ?, 'applied')",
        (user_id, content, NOW),
    ).lastrowid


def add_resume(
    conn,
    user_id: str,
    name: str,
    application_id: int,
    *,
    archived: int = 0,
) -> int:
    """Insert a terminal-schema resume without invoking a second transaction."""
    return conn.execute(
        "INSERT INTO resumes (user_id, name, binding, application_id, content_text, "
        "content_hash, extraction_receipt_json, segments_json, archived, created_time, "
        "updated_time) VALUES (?, ?, 'application', ?, 'fixture resume', ?, '{}', '[]', ?, ?, ?)",
        (user_id, name, application_id, "0" * 64, archived, NOW, NOW),
    ).lastrowid


def read_rows(db_path: str, sql: str, *params) -> list[tuple]:
    with read_connection(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def prepare(
    db_path: str,
    source=("A", "P1"),
    destination=("B", "P2"),
    *,
    proposal_recorder=None,
) -> dict:
    with read_connection(db_path) as conn:
        source_row = conn.execute(
            "SELECT id FROM applications WHERE user_id='u1' AND company=? AND position=?",
            source,
        ).fetchone()
        destination_row = conn.execute(
            "SELECT id FROM applications WHERE user_id='u1' AND company=? AND position=?",
            destination,
        ).fetchone()
    return operations.prepare_application_merge_operation(
        db_path,
        "u1",
        source_application_id=source_row[0] if source_row else 999_001,
        source_company=source[0],
        source_position=source[1],
        destination_application_id=destination_row[0] if destination_row else 999_002,
        destination_company=destination[0],
        destination_position=destination[1],
        proposal_recorder=proposal_recorder,
    )


def test_prepare_is_zero_business_write_and_same_direction_reuses(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(conn, "u1", "A", "P1")
        destination_id = add_application(conn, "u1", "B", "P2")
        conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, summary, "
            "from_stage, to_stage, source, created_time) "
            "VALUES ('u1', ?, '备注', '源岗位历程', 'backlog', 'backlog', 'manual', ?)",
            (source_id, NOW),
        )

    proposal_links: list[tuple[str, str]] = []

    def record_proposal(conn, surface: str, operation_id: str) -> None:
        assert conn.in_transaction
        proposal_links.append((surface, operation_id))

    first = prepare(db_path, proposal_recorder=record_proposal)
    replay = prepare(db_path, proposal_recorder=record_proposal)

    assert first == replay
    assert proposal_links == [
        ("application_merge", first["operation_id"]),
        ("application_merge", first["operation_id"]),
    ]
    assert first["state"] == "pending"
    assert first["source"]["application_id"] == source_id
    assert first["destination"]["application_id"] == destination_id
    assert read_rows(
        db_path, "SELECT id, company, position FROM applications ORDER BY id",
    ) == [(source_id, "A", "P1"), (destination_id, "B", "P2")]
    assert read_rows(db_path, "SELECT application_id FROM timeline_entries") == [(source_id,)]
    assert read_rows(
        db_path, "SELECT state, COUNT(*) FROM journal GROUP BY state",
    ) == [("awaiting_user", 1)]
    assert operations.list_pending_application_merge_operations(db_path, "u1") == [first]


@pytest.mark.parametrize(
    ("source_stage", "destination_stage", "expected_stage"),
    [
        ("backlog", "backlog", "backlog"),
        ("offer", "applied", "applied"),
    ],
)
def test_merge_advances_destination_revision_once_for_any_stage_survivorship(
    tmp_path, source_stage, destination_stage, expected_stage,
):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1", stage=source_stage)
        destination_id = add_application(
            conn, "u1", "B", "P2", stage=destination_stage,
        )
    proposal = prepare(db_path)
    assert read_rows(
        db_path,
        "SELECT json_extract(extraction_json, '$.contract_version') FROM journal "
        "WHERE operation_id=?",
        proposal["operation_id"],
    ) == [(2,)]
    assert proposal["destination"]["revision"] == 0

    completed = operations.approve_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert completed["state"] == "completed"
    assert read_rows(
        db_path,
        "SELECT stage, revision FROM applications WHERE id=?",
        destination_id,
    ) == [(expected_stage, 1)]


def test_merge_stage_survivorship_advances_only_the_destination_revision(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1", stage="offer")
        destination_id = add_application(conn, "u1", "B", "P2", stage="applied")
    proposal = prepare(db_path)
    assert proposal["effect"]["final_destination"]["stage"] == "applied"

    completed = operations.approve_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert completed["state"] == "completed"
    assert read_rows(
        db_path,
        "SELECT stage, revision FROM applications WHERE id=?",
        destination_id,
    ) == [("applied", 1)]


def test_v1_pending_merge_contract_is_stale_without_compatibility_path(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)
    with transaction(db_path) as conn:
        extraction = json.loads(conn.execute(
            "SELECT extraction_json FROM journal WHERE operation_id=?",
            (proposal["operation_id"],),
        ).fetchone()[0])
        extraction["contract_version"] = 1
        conn.execute(
            "UPDATE journal SET extraction_json=? WHERE operation_id=?",
            (json.dumps(extraction, ensure_ascii=False), proposal["operation_id"]),
        )
    assert operations.get_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "stale"
    with pytest.raises(operations.ApplicationMergeOperationConflict):
        operations.approve_application_merge_operation(
            db_path, "u1", proposal["operation_id"],
        )
    assert read_rows(db_path, "SELECT COUNT(*) FROM applications") == [(2,)]


def test_approve_rebinds_every_asset_and_applies_frozen_survivorship(tmp_path):
    db_path = make_db(tmp_path)
    source_jd = json.dumps({"skills": ["Python"], "highlights": ["Agent"]})
    with transaction(db_path) as conn:
        source_company_id = add_company(conn, "u1", "A")
        destination_company_id = add_company(conn, "u1", "B")
        source_id = add_application(
            conn,
            "u1",
            "A",
            "P1",
            company_id=source_company_id,
            department="源部门",
            channel="内推",
            jd_text="源 JD",
            jd_parsed_json=source_jd,
            stage="offer",
            current_step="终面",
            priority="high",
            applied_date="2026-06-01",
            next_stage="offer",
            next_step="谈薪",
            next_date="2026-07-20",
            next_note="源下一步",
            prep_status="ready",
            prep_generation="source-generation",
            prep_json="{}",
        )
        destination_id = add_application(
            conn,
            "u1",
            "B",
            "P2",
            company_id=destination_company_id,
            stage="applied",
            current_step="简历筛选",
            prep_status="ready",
            prep_generation="destination-generation",
            prep_json="{}",
        )
        resume_id = add_resume(conn, "u1", "源专属", source_id, archived=1)
        conn.execute(
            "UPDATE applications SET resume_id = ? WHERE id = ?", (resume_id, source_id),
        )
        review_id = add_journal(conn)
        conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, occurred_date, "
            "summary, from_stage, from_step, to_stage, to_step, source, journal_id, "
            "created_time) VALUES ('u1', ?, '二面', '2026-07-01', '源二面', "
            "'interviewing', '一面', 'interviewing', '二面', 'review', ?, ?)",
            (source_id, review_id, NOW),
        )
        conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, occurred_date, "
            "summary, from_stage, from_step, to_stage, to_step, source, created_time) "
            "VALUES ('u1', ?, '一面', '2026-06-20', '目标一面', 'applied', "
            "'简历筛选', 'interviewing', '一面', 'manual', ?)",
            (destination_id, NOW),
        )
        question_id = conn.execute(
            "INSERT INTO questions (user_id, text, source, company, application_id, "
            "journal_id, created_time, updated_time) "
            "VALUES ('u1', '源真题', 'real', 'A', ?, ?, ?, ?)",
            (source_id, review_id, NOW, NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO review_question_occurrences "
            "(user_id, journal_id, question_id, application_id, company) "
            "VALUES ('u1', ?, ?, ?, 'A')",
            (review_id, question_id, source_id),
        )
        company_count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        journal_count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

    proposal = prepare(db_path)
    final = proposal["effect"]["final_destination"]
    assert final == {
        "application_id": destination_id,
        "company": "B",
        "position": "P2",
        "department": "源部门",
        "channel": "内推",
        "stage": "applied",
        "current_step": "简历筛选",
        "priority": "high",
        "selected_resume": {
            "id": resume_id,
            "name": "源专属",
            "binding": "application",
            "application_id": destination_id,
            "archived": True,
        },
        "applied_date": "2026-06-01",
        "next_action": {
            "stage": "offer",
            "step": "谈薪",
            "date": "2026-07-20",
            "time": None,
            "note": "源下一步",
        },
        "paused_from_stage": None,
        "pause_reason": None,
        "application_note": None,
        "jd_source": "source",
        "jd_preview": "源 JD",
        "jd_truncated": False,
        "skills": ["Python"],
        "highlights": ["Agent"],
        "prep_status": "none",
        "prep_artifact_present": False,
    }

    completed = operations.approve_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    replay = operations.approve_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )

    assert completed == replay
    assert completed["state"] == "completed"
    assert completed["result"]["moved"] == {
        "timeline_entries": 1,
        "questions": 1,
        "question_occurrences": 1,
        "resumes": 1,
    }
    assert read_rows(db_path, "SELECT id FROM applications WHERE id = ?", source_id) == []
    assert read_rows(
        db_path,
        "SELECT company, position, company_id, department, channel, stage, current_step, "
        "priority, resume_id, applied_date, next_stage, next_step, next_date, next_note, "
        "prep_status, prep_generation, prep_json, revision FROM applications WHERE id = ?",
        destination_id,
    ) == [(
        "B", "P2", destination_company_id, "源部门", "内推", "applied", "简历筛选", "high",
        resume_id, "2026-06-01", "offer", "谈薪", "2026-07-20", "源下一步",
        "none", None, None, 1,
    )]
    assert read_rows(
        db_path, "SELECT id, application_id FROM timeline_entries ORDER BY id",
    ) == [(1, destination_id), (2, destination_id)]
    assert read_rows(
        db_path, "SELECT application_id, company FROM questions WHERE id = ?", question_id,
    ) == [(destination_id, "B")]
    assert read_rows(
        db_path,
        "SELECT application_id, company FROM review_question_occurrences "
        "WHERE journal_id = ? AND question_id = ?",
        review_id,
        question_id,
    ) == [(destination_id, "B")]
    assert read_rows(
        db_path, "SELECT binding, application_id, archived FROM resumes WHERE id = ?", resume_id,
    ) == [("application", destination_id, 1)]
    assert read_rows(db_path, "SELECT COUNT(*) FROM companies") == [(company_count,)]
    assert read_rows(db_path, "SELECT COUNT(*) FROM journal") == [(journal_count + 1,)]


def test_destination_values_win_but_discarded_source_values_are_explicit(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(
            conn,
            "u1",
            "A",
            "P1",
            department="源部门",
            channel="源渠道",
            jd_text="源 JD",
            jd_parsed_json=json.dumps({"skills": ["源技能"], "highlights": []}),
            stage="offer",
            current_step="终面",
            priority="high",
            applied_date="2026-01-01",
            next_stage="offer",
            next_step="谈薪",
            next_date="2026-08-01",
            next_note="源安排",
        )
        destination_id = add_application(
            conn,
            "u1",
            "B",
            "P2",
            department="目标部门",
            channel="目标渠道",
            jd_text="目标 JD",
            jd_parsed_json=json.dumps({"skills": ["目标技能"], "highlights": []}),
            stage="interviewing",
            current_step="二面",
            applied_date="2026-02-01",
            next_stage="interviewing",
            next_step="终面",
            next_date="2026-09-01",
            next_note="目标安排",
        )
        source_resume = add_resume(conn, "u1", "源简历", source_id)
        destination_resume = add_resume(conn, "u1", "目标简历", destination_id)
        conn.execute("UPDATE applications SET resume_id = ? WHERE id = ?", (source_resume, source_id))
        conn.execute(
            "UPDATE applications SET resume_id = ? WHERE id = ?",
            (destination_resume, destination_id),
        )

    proposal = prepare(db_path)
    final = proposal["effect"]["final_destination"]
    assert final["department"] == "目标部门"
    assert final["channel"] == "目标渠道"
    assert final["jd_preview"] == "目标 JD"
    assert final["stage"] == "interviewing"
    assert final["applied_date"] == "2026-02-01"
    assert final["next_action"]["note"] == "目标安排"
    assert final["selected_resume"]["id"] == destination_resume
    assert final["priority"] == "high"
    resolutions = {item["field"]: item for item in proposal["effect"]["field_resolutions"]}
    for field in ("department", "channel", "jd", "stage", "current_step",
                  "selected_resume", "applied_date", "next_action"):
        assert resolutions[field]["source_value_carried_forward"] is False
        assert resolutions[field]["source_value"] is not None
        assert resolutions[field]["destination_value"] is not None
        assert resolutions[field]["final_value"] == resolutions[field]["destination_value"]


@pytest.mark.parametrize("difference", ["truncated_tail", "parsed_only"])
def test_jd_carried_contract_compares_complete_frozen_values(tmp_path, difference):
    db_path = make_db(tmp_path)
    common_parsed = {"skills": ["Python"], "highlights": ["Agent"]}
    if difference == "truncated_tail":
        common_prefix = "同" * merge_operations.MAX_DELETE_JD_PREVIEW_CHARS
        source_jd = common_prefix + "源记录独有尾部"
        destination_jd = common_prefix + "保留记录独有尾部"
        source_parsed = common_parsed
        destination_parsed = common_parsed
    else:
        source_jd = destination_jd = "相同的公开 JD"
        source_parsed = {**common_parsed, "keywords": ["源记录独有关键词"]}
        destination_parsed = {**common_parsed, "keywords": ["保留记录独有关键词"]}

    with transaction(db_path) as conn:
        add_application(
            conn,
            "u1",
            "A",
            "P1",
            jd_text=source_jd,
            jd_parsed_json=json.dumps(source_parsed, ensure_ascii=False),
        )
        add_application(
            conn,
            "u1",
            "B",
            "P2",
            jd_text=destination_jd,
            jd_parsed_json=json.dumps(destination_parsed, ensure_ascii=False),
        )

    proposal = prepare(db_path)
    resolutions = {item["field"]: item for item in proposal["effect"]["field_resolutions"]}
    jd_resolution = resolutions["jd"]

    assert jd_resolution["source_value"] == jd_resolution["destination_value"]
    assert jd_resolution["source_value_carried_forward"] is False
    assert proposal["effect"]["final_destination"]["jd_source"] == "destination"


def test_destination_current_step_marks_unpreserved_source_scalar(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1", current_step="七面")
        add_application(conn, "u1", "B", "P2", current_step="简历筛选")

    proposal = prepare(db_path)
    resolutions = {item["field"]: item for item in proposal["effect"]["field_resolutions"]}
    step_resolution = resolutions["current_step"]

    assert proposal["effect"]["final_destination"]["current_step"] == "简历筛选"
    assert step_resolution["source_value"] == "七面"
    assert step_resolution["final_value"] == "简历筛选"
    assert step_resolution["source_value_carried_forward"] is False


def test_dependency_drift_persists_stale_and_keeps_both_applications(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(conn, "u1", "A", "P1")
        destination_id = add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET department = '并发修改', updated_time = ? WHERE id = ?",
            ("2026-07-13T20:01:00+00:00", source_id),
        )

    with pytest.raises(operations.ApplicationMergeOperationConflict, match="失效"):
        operations.approve_application_merge_operation(
            db_path, "u1", proposal["operation_id"],
        )

    assert read_rows(
        db_path, "SELECT id FROM applications ORDER BY id",
    ) == [(source_id,), (destination_id,)]
    assert operations.get_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "stale"


@pytest.mark.parametrize(
    "dependency, mutation",
    [
        (
            "source_timeline_entry",
            "UPDATE timeline_entries SET summary='drift' WHERE summary='source-entry'",
        ),
        (
            "destination_timeline_entry",
            "UPDATE timeline_entries SET step='三面' WHERE summary='destination-entry'",
        ),
        ("source_question", "UPDATE questions SET text='drift' WHERE text='source-question'"),
        ("source_occurrence", "UPDATE review_question_occurrences SET company='drift'"),
        ("source_resume", "UPDATE resumes SET name='drift' WHERE name='source-resume'"),
        ("destination_company", "UPDATE companies SET name='drift' WHERE name='B'"),
    ],
)
def test_each_frozen_dependency_family_drifts_fail_closed(
    tmp_path, dependency, mutation,
):
    del dependency
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_company(conn, "u1", "A")
        destination_company_id = add_company(conn, "u1", "B")
        source_id = add_application(conn, "u1", "A", "P1")
        destination_id = add_application(
            conn, "u1", "B", "P2", company_id=destination_company_id,
        )
        review_id = add_journal(conn)
        conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, summary, "
            "from_stage, to_stage, source, created_time) "
            "VALUES ('u1', ?, '一面', 'source-entry', 'interviewing', "
            "'interviewing', 'manual', ?)",
            (source_id, NOW),
        )
        conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, summary, "
            "from_stage, to_stage, source, created_time) "
            "VALUES ('u1', ?, '二面', 'destination-entry', 'interviewing', "
            "'interviewing', 'manual', ?)",
            (destination_id, NOW),
        )
        question_id = conn.execute(
            "INSERT INTO questions (user_id, text, source, company, application_id, "
            "journal_id, created_time, updated_time) "
            "VALUES ('u1', 'source-question', 'real', 'A', ?, ?, ?, ?)",
            (source_id, review_id, NOW, NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO review_question_occurrences "
            "(user_id, journal_id, question_id, application_id, company) "
            "VALUES ('u1', ?, ?, ?, 'A')",
            (review_id, question_id, source_id),
        )
        add_resume(conn, "u1", "source-resume", source_id)
    proposal = prepare(db_path)
    with transaction(db_path) as conn:
        conn.execute(mutation)

    with pytest.raises(operations.ApplicationMergeOperationConflict, match="失效"):
        operations.approve_application_merge_operation(
            db_path, "u1", proposal["operation_id"],
        )
    assert read_rows(db_path, "SELECT COUNT(*) FROM applications") == [(2,)]


def test_heartbeat_only_drift_does_not_invalidate_frozen_business_effect(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(
            conn,
            "u1",
            "A",
            "P1",
            prep_status="running",
            prep_generation="source-generation",
            prep_heartbeat_time=NOW,
        )
        add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_heartbeat_time = ? WHERE id = ?",
            ("2026-07-13T20:02:00+00:00", source_id),
        )

    completed = operations.approve_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert completed["state"] == "completed"


def test_reject_is_idempotent_and_opposite_command_conflicts(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)

    first = operations.reject_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    replay = operations.reject_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert first == replay
    assert first["state"] == "rejected"
    with pytest.raises(operations.ApplicationMergeOperationConflict):
        operations.approve_application_merge_operation(
            db_path, "u1", proposal["operation_id"],
        )
    assert read_rows(db_path, "SELECT COUNT(*) FROM applications") == [(2,)]


def test_reverse_and_cross_role_pending_merges_are_rejected(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
        add_application(conn, "u1", "C", "P3")
    first = prepare(db_path)
    assert prepare(db_path)["operation_id"] == first["operation_id"]
    with pytest.raises(operations.ApplicationMergeOperationConflict, match="已有其它"):
        prepare(db_path, source=("B", "P2"), destination=("A", "P1"))
    with pytest.raises(operations.ApplicationMergeOperationConflict, match="已有其它"):
        prepare(db_path, source=("A", "P1"), destination=("C", "P3"))
    with pytest.raises(operations.ApplicationMergeOperationConflict, match="已有其它"):
        prepare(db_path, source=("C", "P3"), destination=("B", "P2"))


def test_pending_budget_is_enforced_at_write_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_operations, "MAX_PENDING_MERGE_OPERATIONS", 2)
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        pairs = []
        for index in range(3):
            pairs.append((
                add_application(conn, "u1", f"S{index}", f"P{index}"),
                add_application(conn, "u1", f"D{index}", f"Q{index}"),
            ))
    for index in range(2):
        operation = operations.prepare_application_merge_operation(
            db_path,
            "u1",
            source_application_id=pairs[index][0],
            source_company=f"S{index}",
            source_position=f"P{index}",
            destination_application_id=pairs[index][1],
            destination_company=f"D{index}",
            destination_position=f"Q{index}",
        )
        assert operation["state"] == "pending"
    with pytest.raises(operations.ApplicationMergeOperationConflict, match="安全上限"):
        operations.prepare_application_merge_operation(
            db_path,
            "u1",
            source_application_id=pairs[2][0],
            source_company="S2",
            source_position="P2",
            destination_application_id=pairs[2][1],
            destination_company="D2",
            destination_position="Q2",
        )
    assert len(operations.list_pending_application_merge_operations(db_path, "u1")) == 2


def test_cross_tenant_incoming_reference_is_rejected(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
        conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, summary, "
            "from_stage, to_stage, source, created_time) "
            "VALUES ('evil', ?, '恶意', '跨租户', 'backlog', 'backlog', 'manual', ?)",
            (source_id, NOW),
        )
    with pytest.raises(operations.ApplicationMergeOperationConflict, match="跨租户"):
        prepare(db_path)
    assert read_rows(db_path, "SELECT COUNT(*) FROM journal") == [(0,)]


def test_collateral_trigger_write_rolls_back_and_cannot_fake_untouched_receipt(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(conn, "u1", "A", "P1")
        destination_id = add_application(conn, "u1", "B", "P2")
        conn.execute(
            "INSERT INTO status_log (user_id, log_date, mood, created_time) "
            "VALUES ('u1', '2026-07-13', 'steady', ?)",
            (NOW,),
        )
        conn.execute(
            "CREATE TRIGGER collateral_status_delete AFTER DELETE ON applications "
            f"WHEN OLD.id = {source_id} BEGIN "
            "DELETE FROM status_log WHERE user_id = OLD.user_id; END",
        )
    proposal = prepare(db_path)

    with pytest.raises(operations.ApplicationMergeOperationConflict, match="冻结影响之外"):
        operations.approve_application_merge_operation(
            db_path, "u1", proposal["operation_id"],
        )

    assert read_rows(
        db_path, "SELECT id FROM applications ORDER BY id",
    ) == [(source_id,), (destination_id,)]
    assert read_rows(db_path, "SELECT user_id, mood FROM status_log") == [("u1", "steady")]
    assert operations.get_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "pending"


def test_concurrent_approve_replays_one_atomic_result(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)

    def approve() -> dict:
        return operations.approve_application_merge_operation(
            db_path, "u1", proposal["operation_id"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(), range(2)))
    assert results[0] == results[1]
    assert results[0]["state"] == "completed"
    assert read_rows(db_path, "SELECT company, position FROM applications") == [("B", "P2")]


def test_concurrent_opposite_commands_have_one_terminal_winner(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)

    def command(action: str):
        try:
            if action == "approve":
                return operations.approve_application_merge_operation(
                    db_path, "u1", proposal["operation_id"],
                )
            return operations.reject_application_merge_operation(
                db_path, "u1", proposal["operation_id"],
            )
        except operations.ApplicationMergeOperationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(command, ("approve", "reject")))
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1
    assert results.count("conflict") == 1
    terminal = operations.get_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert terminal["state"] in {"completed", "rejected"}
    expected_count = 1 if terminal["state"] == "completed" else 2
    assert read_rows(db_path, "SELECT COUNT(*) FROM applications") == [(expected_count,)]


def test_exact_names_and_tenant_are_required(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(conn, "u1", "A", "P1")
        destination_id = add_application(conn, "u1", "B", "P2")
    assert prepare(db_path, source=("A", "拼错"))["status"] == "not_found"
    assert operations.prepare_application_merge_operation(
        db_path,
        "other",
        source_application_id=source_id,
        source_company="A",
        source_position="P1",
        destination_application_id=destination_id,
        destination_company="B",
        destination_position="P2",
    )["status"] == "not_found"
    assert operations.get_application_merge_operation(
        db_path, "other", "00000000-0000-0000-0000-000000000001",
    ) is None


def test_update_tool_cannot_mutate_after_freezing_merge_in_same_request(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
    tool = UpdateApplicationTool(db_path, "u1", client_turn_id=UPDATE_TURN_ID)

    proposal_response = tool.run({"updates": [{
        "company": "A",
        "position": "P1",
        "new_company": "B",
        "new_position": "P2",
    }]})
    blocked = tool.run({"updates": [{
        "company": "B", "position": "P2", "new_stage": "offer",
    }]})

    assert proposal_response.status == "success"
    assert blocked.status == "error"
    assert blocked.data == {
        "reason": "request_proposal_write_fence",
        "proposal_type": "application_merge",
    }
    assert read_rows(db_path, "SELECT stage FROM applications WHERE company='B'") == [("backlog",)]
    assert operations.get_application_merge_operation(
        db_path, "u1", proposal_response.data["operation_id"],
    )["state"] == "pending"


def test_corrupt_envelope_is_persistently_stale_and_zero_write(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET derivation_json = ? WHERE operation_id = ?",
            (
                json.dumps({"operation": {
                    "type": "application_merge",
                    "proposal_digest": "0" * 64,
                }}),
                proposal["operation_id"],
            ),
        )

    with pytest.raises(operations.ApplicationMergeOperationConflict, match="失效"):
        operations.approve_application_merge_operation(
            db_path, "u1", proposal["operation_id"],
        )
    assert read_rows(db_path, "SELECT COUNT(*) FROM applications") == [(2,)]
    assert operations.get_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "stale"


def test_corrupt_terminal_result_cannot_be_presented_as_completed(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, "u1", "A", "P1")
        add_application(conn, "u1", "B", "P2")
    proposal = prepare(db_path)
    completed = operations.approve_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT derivation_json FROM journal WHERE operation_id = ?",
            (proposal["operation_id"],),
        ).fetchone()
        derivation = json.loads(row[0])
        derivation["operation"]["result"]["source_application_id"] = (
            completed["result"]["destination_application_id"]
        )
        conn.execute(
            "UPDATE journal SET derivation_json = ? WHERE operation_id = ?",
            (json.dumps(derivation), proposal["operation_id"]),
        )
    assert operations.get_application_merge_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "stale"


def test_merge_endpoint_cannot_claim_other_operation_type(tmp_path):
    db_path = make_db(tmp_path)
    operation_id = "55555555-5555-4555-8555-555555555555"
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) VALUES "
            "('u1', 'correction', 'delete op', ?, '{}', "
            "'{\"operation\":{\"type\":\"application_delete\"}}', "
            "'awaiting_user', ?)",
            (NOW, operation_id),
        )
    assert operations.get_application_merge_operation(db_path, "u1", operation_id) is None
    with pytest.raises(operations.ApplicationMergeOperationNotFound):
        operations.approve_application_merge_operation(db_path, "u1", operation_id)


def test_application_merge_http_contract_and_canonical_recovery(client):
    test_client, db_path = client
    with transaction(db_path) as conn:
        source_id = add_application(conn, "me", "A", "P1")
        destination_id = add_application(conn, "me", "B", "P2")
    proposal = operations.prepare_application_merge_operation(
        db_path,
        "me",
        source_application_id=source_id,
        source_company="A",
        source_position="P1",
        destination_application_id=destination_id,
        destination_company="B",
        destination_position="P2",
    )
    base = f"/api/timeline/application-merge-operations/{proposal['operation_id']}"

    assert test_client.get(
        "/api/timeline/application-merge-operations/pending",
    ).json()["operations"] == [proposal]
    assert test_client.get(base).json() == proposal
    assert test_client.post(
        f"{base}/approve", json={"source_application_id": proposal["source"]["application_id"]},
    ).status_code == 422

    completed = test_client.post(f"{base}/approve", json={})
    replay = test_client.post(f"{base}/approve", json={})
    recovered = test_client.get(base)
    assert completed.status_code == replay.status_code == recovered.status_code == 200
    assert completed.json() == replay.json() == recovered.json()
    assert completed.json()["state"] == "completed"
    assert test_client.post(f"{base}/reject", json={}).status_code == 409
    assert test_client.get(base, headers={"Remote-User": "other"}).status_code == 404
    assert test_client.post(
        f"{base}/approve", json={}, headers={"Remote-User": "other"},
    ).status_code == 404
