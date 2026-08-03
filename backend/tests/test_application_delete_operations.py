
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.features.applications import operations
from careerdesk.features.applications.operations import delete as delete_operations
from careerdesk.features.applications import repository as application_repository

NOW = "2026-07-13T12:00:00+00:00"


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "application-delete.db")
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


def _seed_application(
    db_path: str,
    *,
    user_id: str = "u1",
    company: str = "A公司",
    position: str = "Agent工程师",
    resume_name: str = "岗位专属简历",
) -> dict[str, int]:
    with transaction(db_path) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (user_id, name, aliases_json, research_json, research_time, "
            "notes, created_time, updated_time) VALUES (?, ?, '[]', '{\"business\":\"保留\"}', "
            "?, '公司备注', ?, ?)",
            (user_id, company, NOW, NOW, NOW),
        ).lastrowid
        review_id = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, processed_time, "
            "extraction_json, derivation_json, state, revision) VALUES "
            "(?, 'review', '原始复盘全文', ?, ?, '{}', '{}', 'applied', 1)",
            (user_id, NOW, NOW),
        ).lastrowid
        application_id = conn.execute(
            "INSERT INTO applications (user_id, company, company_id, position, department, "
            "channel, jd_text, jd_parsed_json, stage, current_step, priority, applied_date, "
            "next_stage, next_step, next_date, next_note, prep_status, prep_json, "
            "created_time, updated_time) "
            "VALUES (?, ?, ?, ?, '平台部', '内推', '完整 JD 原文', "
            "'{\"skills\":[\"Python\"],\"highlights\":[\"Agent 架构\"]}', "
            "'interviewing', '二面', 'high', '2026-07-01', 'interviewing', '三面', "
            "'2026-07-20', '等待三面', 'ready', "
            "'{\"resume_adaptation\":{\"artifact_version\":1}}', ?, ?)",
            (user_id, company, company_id, position, NOW, NOW),
        ).lastrowid
        resume_id = conn.execute(
            "INSERT INTO resumes (user_id, name, family, binding, application_id, file_path, "
            "content_text, content_hash, extraction_receipt_json, segments_json, lines_json, "
            "archived, created_time, updated_time) VALUES "
            "(?, ?, 'agent_app', 'application', ?, '/managed/resume.pdf', "
            "'正文', ?, '{}', '[]', '[]', 0, ?, ?)",
            (user_id, resume_name, application_id, "0" * 64, NOW, NOW),
        ).lastrowid
        conn.execute(
            "UPDATE applications SET resume_id = ? WHERE id = ?",
            (resume_id, application_id),
        )
        entry_id = conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, occurred_date, "
            "outcome, summary, from_stage, from_step, to_stage, to_step, source, journal_id, "
            "created_time) VALUES (?, ?, '二面', '2026-07-13', 'passed', '二面完成', "
            "'interviewing', '一面', 'interviewing', '二面', 'review', ?, ?)",
            (user_id, application_id, review_id, NOW),
        ).lastrowid
        question_id = conn.execute(
            "INSERT INTO questions (user_id, text, source, company, source_step, asked_date, "
            "application_id, status, journal_id, created_time, updated_time) "
            "VALUES (?, '如何设计稳定的 Agent operation？', 'real', ?, '二面', '2026-07-13', ?, "
            "'active', ?, ?, ?)",
            (user_id, company, application_id, review_id, NOW, NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO review_question_occurrences (user_id, journal_id, question_id, "
            "application_id, company, source_step, asked_date) "
            "VALUES (?, ?, ?, ?, ?, '二面', '2026-07-13')",
            (user_id, review_id, question_id, application_id, company),
        )
        status_log_id = conn.execute(
            "INSERT INTO status_log (user_id, log_date, mood, journal_id, created_time) "
            "VALUES (?, '2026-07-13', '稳定', ?, ?)",
            (user_id, review_id, NOW),
        ).lastrowid
    return {
        "application": application_id,
        "company": company_id,
        "review": review_id,
        "resume": resume_id,
        "timeline_entry": entry_id,
        "question": question_id,
        "status_log": status_log_id,
    }


def _business_snapshot(db_path: str, user_id: str = "u1") -> dict:
    with read_connection(db_path) as conn:
        return {
            "applications": conn.execute(
                "SELECT * FROM applications WHERE user_id=? ORDER BY id", (user_id,),
            ).fetchall(),
            "timeline_entries": conn.execute(
                "SELECT * FROM timeline_entries WHERE user_id=? ORDER BY id", (user_id,),
            ).fetchall(),
            "questions": conn.execute(
                "SELECT id, application_id, text, status FROM questions WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall(),
            "occurrences": conn.execute(
                "SELECT journal_id, question_id, application_id, company "
                "FROM review_question_occurrences WHERE user_id=? ORDER BY journal_id, question_id",
                (user_id,),
            ).fetchall(),
            "resumes": conn.execute(
                "SELECT id, application_id, binding, name FROM resumes WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall(),
            "status_logs": conn.execute(
                "SELECT id, journal_id FROM status_log WHERE user_id=? ORDER BY id", (user_id,),
            ).fetchall(),
            "companies": conn.execute(
                "SELECT id, name, research_json FROM companies WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall(),
        }


def test_prepare_freezes_exact_effect_without_business_write_and_reuses_id(db_path):
    ids = _seed_application(db_path)
    before = _business_snapshot(db_path)
    proposal_links: list[tuple[str, str]] = []

    def record_proposal(conn, surface: str, operation_id: str) -> None:
        assert conn.in_transaction
        proposal_links.append((surface, operation_id))

    proposal = operations.prepare_application_delete_operation(
        db_path,
        "u1",
        company="A公司",
        position="Agent工程师",
        proposal_recorder=record_proposal,
    )
    replay = operations.prepare_application_delete_operation(
        db_path,
        "u1",
        company="A公司",
        position="Agent工程师",
        proposal_recorder=record_proposal,
    )

    assert replay == proposal
    assert proposal_links == [
        ("application_delete", proposal["operation_id"]),
        ("application_delete", proposal["operation_id"]),
    ]
    assert proposal["state"] == "pending"
    assert proposal["operation_type"] == "application_delete"
    assert proposal["target"]["application_id"] == ids["application"]
    assert proposal["target"]["selected_resume"] == {
        "id": ids["resume"], "name": "岗位专属简历", "archived": False,
    }
    assert proposal["target"]["prep_artifact_present"] is True
    assert [row["id"] for row in proposal["effect"]["timeline_entries"]] == [
        ids["timeline_entry"],
    ]
    assert [row["id"] for row in proposal["effect"]["questions_detached"]] == [
        ids["question"],
    ]
    assert proposal["effect"]["question_occurrences_detached"] == 1
    assert "status_logs" not in proposal["effect"]
    assert "dependency_fingerprint" not in proposal
    assert _business_snapshot(db_path) == before
    assert operations.list_pending_application_delete_operations(db_path, "u1") == [proposal]


def test_prepare_batch_freezes_all_targets_in_one_transaction_without_business_write(db_path):
    first = _seed_application(db_path, company="A公司", position="Agent工程师")
    second = _seed_application(
        db_path,
        company="B公司",
        position="前端工程师",
        resume_name="B公司岗位专属简历",
    )
    before = _business_snapshot(db_path)
    proposal_links: list[tuple[str, str]] = []

    proposals = operations.prepare_application_delete_operations(
        db_path,
        "u1",
        [
            {"company": "A公司", "position": "Agent工程师"},
            {"company": "B公司", "position": "前端工程师"},
        ],
        proposal_recorder=lambda conn, surface, operation_id: proposal_links.append(
            (surface, operation_id),
        ),
    )

    assert [item["target"]["application_id"] for item in proposals] == [
        first["application"], second["application"],
    ]
    assert proposal_links == [
        ("application_delete", proposal["operation_id"])
        for proposal in proposals
    ]
    assert _business_snapshot(db_path) == before
    assert {
        item["operation_id"]
        for item in operations.list_pending_application_delete_operations(db_path, "u1")
    } == {item["operation_id"] for item in proposals}


def test_prepare_batch_accepts_full_200_target_product_limit(db_path):
    targets = [
        {"company": f"测试公司{i:03d}", "position": f"测试岗位{i:03d}"}
        for i in range(201)
    ]
    with transaction(db_path) as conn:
        conn.executemany(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', ?, ?, ?, ?)",
            [
                (target["company"], target["position"], NOW, NOW)
                for target in targets
            ],
        )

    proposals = operations.prepare_application_delete_operations(
        db_path,
        "u1",
        targets[:200],
    )

    assert len(proposals) == 200
    assert all(proposal["state"] == "pending" for proposal in proposals)
    with pytest.raises(ValueError, match="1–200"):
        operations.prepare_application_delete_operations(db_path, "u1", targets)


def test_prepare_all_resolves_only_the_tenant_complete_set_inside_one_batch(db_path):
    first = _seed_application(db_path, company="A公司", position="Agent工程师")
    second = _seed_application(
        db_path,
        company="B公司",
        position="前端工程师",
        resume_name="B公司岗位专属简历",
    )
    _seed_application(db_path, user_id="u2", company="其他公司", position="其他岗位")

    proposals = operations.prepare_all_application_delete_operations(db_path, "u1")

    assert [proposal["target"]["application_id"] for proposal in proposals] == [
        first["application"],
        second["application"],
    ]
    assert all(proposal["state"] == "pending" for proposal in proposals)
    assert operations.list_pending_application_delete_operations(db_path, "u2") == []


def test_prepare_all_rejects_more_than_the_complete_batch_limit_without_partial_preview(db_path):
    with transaction(db_path) as conn:
        conn.executemany(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', ?, ?, ?, ?)",
            [
                (f"测试公司{index:03d}", f"测试岗位{index:03d}", NOW, NOW)
                for index in range(201)
            ],
        )

    with pytest.raises(ValueError, match="1–200"):
        operations.prepare_all_application_delete_operations(db_path, "u1")

    assert operations.list_pending_application_delete_operations(db_path, "u1") == []


def test_prepare_batch_rolls_back_every_proposal_when_one_target_is_missing(db_path):
    _seed_application(db_path, company="A公司", position="Agent工程师")

    with pytest.raises(
        operations.ApplicationDeleteOperationConflict,
        match="没找到精确匹配",
    ):
        operations.prepare_application_delete_operations(
            db_path,
            "u1",
            [
                {"company": "A公司", "position": "Agent工程师"},
                {"company": "不存在", "position": "前端工程师"},
            ],
        )

    assert operations.list_pending_application_delete_operations(db_path, "u1") == []


def test_prepare_locates_application_across_internal_space(db_path):
    ids = _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A 公司", position="Agent 工程师",
    )
    assert proposal["state"] == "pending"
    assert proposal["target"]["application_id"] == ids["application"]


def test_prepare_rolls_back_proposal_when_atomic_link_fails(db_path):
    _seed_application(db_path)

    def reject_link(conn, surface: str, operation_id: str) -> None:
        assert conn.in_transaction
        assert surface == "application_delete"
        assert operation_id
        raise RuntimeError("link failed")

    with pytest.raises(RuntimeError, match="link failed"):
        operations.prepare_application_delete_operation(
            db_path,
            "u1",
            company="A公司",
            position="Agent工程师",
            proposal_recorder=reject_link,
        )

    assert operations.list_pending_application_delete_operations(db_path, "u1") == []


def test_prepare_enforces_pending_operation_budget_at_write_boundary(db_path, monkeypatch):
    monkeypatch.setattr(delete_operations, "MAX_PENDING_DELETE_OPERATIONS", 2)
    with transaction(db_path) as conn:
        conn.executemany(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', ?, 'Agent工程师', ?, ?)",
            [(company, NOW, NOW) for company in ("A公司", "B公司", "C公司")],
        )

    first = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    second = operations.prepare_application_delete_operation(
        db_path, "u1", company="B公司", position="Agent工程师",
    )
    pending_ids = {
        item["operation_id"]
        for item in operations.list_pending_application_delete_operations(db_path, "u1")
    }
    assert pending_ids == {first["operation_id"], second["operation_id"]}

    with pytest.raises(operations.ApplicationDeleteOperationConflict, match="已达安全上限"):
        operations.prepare_application_delete_operation(
            db_path, "u1", company="C公司", position="Agent工程师",
        )
    assert len(operations.list_pending_application_delete_operations(db_path, "u1")) == 2


def test_prepare_selector_is_exact_and_ambiguous_without_position(db_path):
    _seed_application(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A公司', '另一岗位', ?, ?)",
            (NOW, NOW),
        )
    assert operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="拼错岗位",
    ) == {"status": "not_found"}
    ambiguous = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司",
    )
    assert ambiguous == {"status": "ambiguous", "options": ["Agent工程师", "另一岗位"]}
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL",
        ).fetchone() == (0,)


def test_prepare_rejects_oversized_dependencies_without_truncating_effect(db_path):
    ids = _seed_application(db_path)
    with transaction(db_path) as conn:
        conn.executemany(
            "INSERT INTO timeline_entries (user_id, application_id, step, summary, "
            "from_stage, to_stage, source, created_time) "
            "VALUES ('u1', ?, '补记', ?, 'interviewing', 'interviewing', 'manual', ?)",
            [(ids["application"], f"额外事件 {index}", NOW) for index in range(100)],
        )
    with pytest.raises(operations.ApplicationDeleteOperationConflict, match="行数上限"):
        operations.prepare_application_delete_operation(
            db_path, "u1", company="A公司", position="Agent工程师",
        )
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL",
        ).fetchone() == (0,)


def test_prepare_fails_closed_on_cross_tenant_incoming_reference(db_path):
    ids = _seed_application(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO questions (user_id, text, source, application_id, created_time, "
            "updated_time) VALUES ('u2', '损坏关联', 'imported', ?, ?, ?)",
            (ids["application"], NOW, NOW),
        )
    with pytest.raises(operations.ApplicationDeleteOperationConflict, match="跨租户"):
        operations.prepare_application_delete_operation(
            db_path, "u1", company="A公司", position="Agent工程师",
        )
    assert _business_snapshot(db_path)["applications"]


def test_approve_deletes_only_timeline_record_and_detaches_retained_assets(db_path):
    ids = _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )

    completed = operations.approve_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    replay = operations.approve_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert replay == completed
    assert completed["state"] == "completed"
    assert completed["result"] == {
        "status": "ok",
        "application_id": ids["application"],
        "timeline_entries_removed": 1,
        "questions_detached": 1,
        "question_occurrences_detached": 1,
        "resumes_detached": 1,
    }
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM timeline_entries").fetchone() == (0,)
        assert conn.execute(
            "SELECT application_id, status FROM questions WHERE id=?", (ids["question"],),
        ).fetchone() == (None, "active")
        assert conn.execute(
            "SELECT application_id, company FROM review_question_occurrences",
        ).fetchone() == (None, "A公司")
        assert conn.execute(
            "SELECT application_id, binding, name FROM resumes WHERE id=?", (ids["resume"],),
        ).fetchone() == (None, "family", "岗位专属简历")
        assert conn.execute("SELECT COUNT(*) FROM status_log").fetchone() == (1,)
        assert conn.execute("SELECT research_json FROM companies").fetchone() == (
            '{"business":"保留"}',
        )
        assert conn.execute(
            "SELECT state FROM journal WHERE id=?", (ids["review"],),
        ).fetchone() == ("applied",)

    with pytest.raises(operations.ApplicationDeleteOperationConflict):
        operations.reject_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )


def test_reject_is_idempotent_and_never_changes_business_rows(db_path):
    _seed_application(db_path)
    before = _business_snapshot(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    first = operations.reject_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    replay = operations.reject_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert first == replay
    assert first["state"] == "rejected"
    assert _business_snapshot(db_path) == before
    with pytest.raises(operations.ApplicationDeleteOperationConflict):
        operations.approve_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )


def test_concurrent_approve_replays_one_receipt(db_path):
    _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )

    def approve():
        return operations.approve_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: approve(), range(2)))
    assert results[0] == results[1]
    assert results[0]["state"] == "completed"


def test_operation_identity_is_tenant_scoped(db_path):
    _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    assert operations.get_application_delete_operation(
        db_path, "u2", proposal["operation_id"],
    ) is None
    with pytest.raises(operations.ApplicationDeleteOperationNotFound):
        operations.approve_application_delete_operation(
            db_path, "u2", proposal["operation_id"],
        )
    with pytest.raises(operations.ApplicationDeleteOperationNotFound):
        operations.reject_application_delete_operation(
            db_path, "u2", proposal["operation_id"],
        )


@pytest.mark.parametrize(
    "mutate",
    [
        "UPDATE applications SET priority='low' WHERE user_id='u1'",
        "UPDATE timeline_entries SET summary='变化' WHERE user_id='u1'",
        "UPDATE questions SET text='变化后的题面' WHERE user_id='u1'",
        "UPDATE review_question_occurrences SET company='变化公司' WHERE user_id='u1'",
        "UPDATE resumes SET name='变化后的简历' WHERE user_id='u1'",
    ],
)
def test_any_relevant_dependency_drift_stales_without_partial_delete(db_path, mutate):
    _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    with transaction(db_path) as conn:
        conn.execute(mutate)
    before = _business_snapshot(db_path)

    with pytest.raises(operations.ApplicationDeleteOperationConflict, match="失效"):
        operations.approve_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )
    assert _business_snapshot(db_path) == before
    assert operations.get_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "stale"


def test_prep_heartbeat_alone_does_not_create_an_unapprovable_card(db_path):
    _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_heartbeat_time=? WHERE user_id='u1'",
            ("2026-07-13T13:00:00+00:00",),
        )
    completed = operations.approve_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert completed["state"] == "completed"


def test_deleted_target_recreated_with_same_natural_key_is_never_accepted(db_path):
    ids = _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = application_repository._delete_application_in_transaction(
            conn, "u1", ids["application"],
        )
        assert result["status"] == "ok"
        replacement = conn.execute(
            "INSERT INTO applications (user_id, company, position, stage, created_time, "
            "updated_time) VALUES ('u1', 'A公司', 'Agent工程师', 'backlog', ?, ?)",
            (NOW, NOW),
        ).lastrowid
    assert replacement > ids["application"]

    with pytest.raises(operations.ApplicationDeleteOperationConflict):
        operations.approve_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT id FROM applications WHERE company='A公司' AND position='Agent工程师'",
        ).fetchone() == (replacement,)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("extraction_json", "{}"),
        ("created_time", "   "),
        ("derivation_json", '{"operation":{"type":"application_delete"}}'),
    ],
)
def test_corrupt_operation_is_safely_staled(db_path, column, value):
    _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    with transaction(db_path) as conn:
        conn.execute(
            f"UPDATE journal SET {column}=? WHERE operation_id=?",
            (value, proposal["operation_id"]),
        )
    with pytest.raises(operations.ApplicationDeleteOperationConflict):
        operations.approve_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )
    assert operations.get_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "stale"


def test_receipt_failure_rolls_back_every_business_side_effect(db_path):
    _seed_application(db_path)
    before = _business_snapshot(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_delete_receipt BEFORE UPDATE ON journal "
            "WHEN OLD.operation_id IS NOT NULL AND NEW.state='applied' "
            "BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="receipt unavailable"):
        operations.approve_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )
    assert _business_snapshot(db_path) == before
    assert operations.get_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )["state"] == "pending"


def test_corrupt_terminal_result_cannot_be_reported_as_completed(db_path):
    _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    operations.approve_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    with transaction(db_path) as conn:
        raw = conn.execute(
            "SELECT derivation_json FROM journal WHERE operation_id=?",
            (proposal["operation_id"],),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["operation"]["result"]["timeline_entries_removed"] = 0
        conn.execute(
            "UPDATE journal SET derivation_json=? WHERE operation_id=?",
            (json.dumps(payload), proposal["operation_id"]),
        )
    canonical = operations.get_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert canonical["state"] == "stale"
    assert canonical["result"] is None


def test_terminal_receipt_is_bound_to_the_original_frozen_proposal(db_path):
    _seed_application(db_path)
    proposal = operations.prepare_application_delete_operation(
        db_path, "u1", company="A公司", position="Agent工程师",
    )
    operations.approve_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    with transaction(db_path) as conn:
        raw = conn.execute(
            "SELECT extraction_json FROM journal WHERE operation_id=?",
            (proposal["operation_id"],),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["target"]["company"] = "伪造公司"
        payload["target"]["position"] = "伪造岗位"
        conn.execute(
            "UPDATE journal SET extraction_json=? WHERE operation_id=?",
            (json.dumps(payload, ensure_ascii=False), proposal["operation_id"]),
        )

    canonical = operations.get_application_delete_operation(
        db_path, "u1", proposal["operation_id"],
    )
    assert canonical["state"] == "stale"
    assert canonical["result"] is None
    with pytest.raises(operations.ApplicationDeleteOperationConflict):
        operations.approve_application_delete_operation(
            db_path, "u1", proposal["operation_id"],
        )


def test_application_delete_endpoint_cannot_claim_review_operation(db_path):
    operation_id = "44444444-4444-4444-8444-444444444444"
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) VALUES "
            "('u1', 'correction', 'review op', ?, '{}', "
            "'{\"operation\":{\"type\":\"review_undo\"}}', 'awaiting_user', ?)",
            (NOW, operation_id),
        )
    assert operations.get_application_delete_operation(db_path, "u1", operation_id) is None
    with pytest.raises(operations.ApplicationDeleteOperationNotFound):
        operations.approve_application_delete_operation(db_path, "u1", operation_id)


def test_application_delete_http_contract_and_canonical_recovery(client):
    test_client, db_path = client
    _seed_application(db_path, user_id="me")
    proposal = operations.prepare_application_delete_operation(
        db_path, "me", company="A公司", position="Agent工程师",
    )
    base = f"/api/timeline/application-delete-operations/{proposal['operation_id']}"

    assert test_client.get(
        "/api/timeline/application-delete-operations/pending",
    ).json()["operations"] == [proposal]
    assert test_client.get(base).json() == proposal
    assert test_client.post(
        f"{base}/approve", json={"application_id": proposal["target"]["application_id"]},
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
