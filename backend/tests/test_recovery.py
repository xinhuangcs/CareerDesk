
import json

from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.applications import repository as applications_repository
from careerdesk.features.applications.intake_models import ParsedPosition
from careerdesk.services.recovery import recover_interrupted_work


def test_recover_interrupted_work_marks_jobs_retryable_and_preserves_payload(tmp_path):
    db_path = str(tmp_path / "recovery.db")
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, prep_status, prep_generation, "
            "prep_json, created_time, updated_time) "
            "VALUES ('u1', 'A厂', '后端', 'running', 'generation-1', ?, ?, ?)",
            (json.dumps({
                "unrelated_artifact": {"name": "保留我"},
                "research_snapshot": {"last_success": True},
                "research_attempt": {
                    "attempt_state": "running",
                    "generation": "generation-1",
                    "updated_time": now_iso(),
                    "error_code": None,
                },
            }), now_iso(), now_iso()),
        )

    assert recover_interrupted_work(db_path) == {
        "preps": 1, "intakes": 0, "review_records": 0,
        "question_sets": 0, "grill_answers": 0,
    }
    with read_connection(db_path) as conn:
        prep_status, prep_json = conn.execute(
            "SELECT prep_status, prep_json FROM applications WHERE company = 'A厂'"
        ).fetchone()
    assert prep_status == "failed"
    recovered_prep = json.loads(prep_json)
    assert recovered_prep["unrelated_artifact"]["name"] == "保留我"
    assert recovered_prep["research_snapshot"] == {"last_success": True}
    assert recovered_prep["research_attempt"]["attempt_state"] == "failed"
    assert recovered_prep["research_attempt"]["error_code"] == "interrupted"
    assert "中断" in recovered_prep["error"]

    assert recover_interrupted_work(db_path) == {
        "preps": 0, "intakes": 0, "review_records": 0,
        "question_sets": 0, "grill_answers": 0,
    }


def test_recovery_preserves_succeeded_research_when_later_prep_stage_was_interrupted(tmp_path):
    db_path = str(tmp_path / "succeeded-research.db")
    init_db(db_path)
    prep = {
        "research_snapshot": {"last_success": True},
        "research_attempt": {
            "attempt_state": "succeeded",
            "generation": "generation-1",
            "updated_time": now_iso(),
            "error_code": None,
        },
    }
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, prep_status, "
            "prep_generation, prep_json, created_time, updated_time) "
            "VALUES ('u1', 'A厂', '后端', 'running', 'generation-1', ?, ?, ?)",
            (json.dumps(prep), now_iso(), now_iso()),
        )

    assert recover_interrupted_work(db_path)["preps"] == 1
    with read_connection(db_path) as conn:
        status, raw = conn.execute(
            "SELECT prep_status, prep_json FROM applications"
        ).fetchone()
    recovered = json.loads(raw)
    assert status == "failed"
    assert recovered["research_snapshot"] == {"last_success": True}
    assert recovered["research_attempt"]["attempt_state"] == "succeeded"


def test_recovery_replaces_non_object_payloads_with_safe_error_objects(tmp_path):
    db_path = str(tmp_path / "malformed-recovery.db")
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, prep_status, prep_json, "
            "created_time, updated_time) VALUES ('u1', 'A厂', '后端', 'running', '[]', ?, ?)",
            (now_iso(), now_iso()),
        )

    assert recover_interrupted_work(db_path) == {
        "preps": 1, "intakes": 0, "review_records": 0,
        "question_sets": 0, "grill_answers": 0,
    }
    with read_connection(db_path) as conn:
        prep_json = conn.execute("SELECT prep_json FROM applications").fetchone()[0]
    assert "中断" in json.loads(prep_json)["error"]


def test_recovery_fails_interrupted_intakes_and_never_resurrects_older_preview(tmp_path):
    db_path = str(tmp_path / "intake-recovery.db")
    init_db(db_path)
    older_id, older_operation = applications_repository.create_intake_batch(
        db_path, "u1", "旧预览",
    )
    assert applications_repository.activate_intake_proposal(
        db_path, "u1", older_id,
        [ParsedPosition(company="旧公司", position="旧岗位")],
    )
    interrupted_id, _ = applications_repository.create_intake_batch(
        db_path, "u1", "进程退出时正在解析的新意图",
    )

    assert recover_interrupted_work(db_path) == {
        "preps": 0, "intakes": 1, "review_records": 0,
        "question_sets": 0, "grill_answers": 0,
    }
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, state, derivation_json FROM journal ORDER BY id",
        ).fetchall()
    assert rows[0][0:2] == (older_id, "superseded")
    assert json.loads(rows[0][2])["superseded_by"] == interrupted_id
    assert rows[1][0:2] == (interrupted_id, "failed")
    assert json.loads(rows[1][2]) == {
        "intake_failure": {"reason": "application_restart_interrupted"},
    }
    assert applications_repository.get_intake_operation(
        db_path, "u1", older_operation,
    )["state"] == "stale"

    new_id, _ = applications_repository.create_intake_batch(db_path, "u1", "重试")
    assert applications_repository.activate_intake_proposal(
        db_path, "u1", new_id,
        [ParsedPosition(company="新公司", position="新岗位")],
    )


def test_recovery_uses_latest_terminal_intent_to_retire_older_awaiting(tmp_path):
    db_path = str(tmp_path / "terminal-intent.db")
    init_db(db_path)
    older_id, _ = applications_repository.create_intake_batch(db_path, "u1", "旧")
    assert applications_repository.activate_intake_proposal(
        db_path, "u1", older_id,
        [ParsedPosition(company="旧公司", position="旧岗位")],
    )
    newer_id, _ = applications_repository.create_intake_batch(db_path, "u1", "新")
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET state = 'failed', derivation_json = ? WHERE id = ?",
            (json.dumps({"intake_failure": {"reason": "already_terminal"}}), newer_id),
        )

    assert recover_interrupted_work(db_path) == {
        "preps": 0, "intakes": 0, "review_records": 0,
        "question_sets": 0, "grill_answers": 0,
    }
    with read_connection(db_path) as conn:
        old_state, derivation = conn.execute(
            "SELECT state, derivation_json FROM journal WHERE id = ?", (older_id,),
        ).fetchone()
    assert old_state == "superseded"
    assert json.loads(derivation)["superseded_by"] == newer_id
