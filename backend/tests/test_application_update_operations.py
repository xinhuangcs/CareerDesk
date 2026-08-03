
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import (
    DatabaseBusy,
    init_db,
    now_iso,
    read_connection,
    transaction,
)
from careerdesk.features.applications import operations
from careerdesk.features.applications.operations import update as update_operations
from careerdesk.features.preferences.public import execute_preference_update_operation


def make_db(tmp_path) -> str:
    path = str(tmp_path / "application-update.db")
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


def add_company(conn, user_id="u1", name="A") -> int:
    stamp = now_iso()
    return conn.execute(
        "INSERT INTO companies (user_id, name, created_time, updated_time) VALUES (?, ?, ?, ?)",
        (user_id, name, stamp, stamp),
    ).lastrowid


def add_application(conn, user_id="u1", company="A", position="P", *,
                    company_id=None, stage="backlog", prep=False,
                    jd_text: str | None = None) -> int:
    stamp = now_iso()
    return conn.execute(
        "INSERT INTO applications (user_id, company, company_id, position, stage, jd_text, "
        "prep_status, prep_generation, prep_heartbeat_time, prep_json, "
        "created_time, updated_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, company, company_id, position, stage, jd_text,
            "ready" if prep else "none", "generation" if prep else None,
            stamp if prep else None, '{"ready":true}' if prep else None, stamp, stamp,
        ),
    ).lastrowid


def execute(db_path: str, operation_id=None, turn_id=None, **overrides):
    return operations.execute_application_update_operation(
        db_path,
        "u1",
        operation_id=operation_id or str(uuid4()),
        client_turn_id=turn_id or str(uuid4()),
        company=overrides.pop("company", "A"),
        position=overrides.pop("position", "P"),
        changes=overrides.pop("changes", {"stage": "applied"}),
        **overrides,
    )


def rows(db_path: str, sql: str, *params):
    with read_connection(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def execute_batch(db_path: str, commands: list[dict], *, operation_ids=None, turn_id=None):
    return operations.execute_application_update_batch(
        db_path,
        "u1",
        operation_ids=operation_ids or [str(uuid4()) for _ in commands],
        client_turn_id=turn_id or str(uuid4()),
        commands=commands,
    )


def test_batch_updates_two_applications_in_one_public_operation(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")
        add_application(conn, company="B", position="P2")
    turn_id = str(uuid4())

    result = execute_batch(
        db_path,
        [
            {"company": "A", "position": "P1", "changes": {"stage": "applied"}},
            {"company": "B", "position": "P2", "changes": {"priority": "high"}},
        ],
        turn_id=turn_id,
    )

    assert result["state"] == "completed"
    assert (result["requested_count"], result["changed_count"], result["no_change_count"]) == (
        2, 2, 0,
    )
    assert [item["status"] for item in result["results"]] == ["completed", "completed"]
    assert rows(
        db_path,
        "SELECT company, position, stage, priority, revision FROM applications ORDER BY id",
    ) == [("A", "P1", "applied", None, 1), ("B", "P2", "backlog", "high", 1)]
    assert len(operations.list_application_update_operations_for_turn(
        db_path, "u1", turn_id,
    )) == 2


def test_batch_rolls_back_every_item_when_any_target_is_missing(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")

    result = execute_batch(db_path, [
        {"company": "A", "position": "P1", "changes": {"stage": "applied"}},
        {"company": "Missing", "position": "P2", "changes": {"stage": "offer"}},
    ])

    assert result["state"] == "rejected"
    assert result["issues"] == [{"index": 1, "reason": "not_found"}]
    assert rows(db_path, "SELECT stage, revision FROM applications") == [("backlog", 0)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(0,)]


def test_batch_rolls_back_tentative_writes_on_later_revision_conflict(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        first_id = add_application(conn, company="A", position="P1")
        second_id = add_application(conn, company="B", position="P2")

    result = execute_batch(db_path, [
        {
            "company": "A",
            "position": "P1",
            "changes": {"stage": "applied"},
            "expected_application_id": first_id,
            "expected_revision": 0,
        },
        {
            "company": "B",
            "position": "P2",
            "changes": {"stage": "offer"},
            "expected_application_id": second_id,
            "expected_revision": 99,
        },
    ])

    assert result["state"] == "rejected"
    assert result["issues"][0]["index"] == 1
    assert result["issues"][0]["reason"] == "conflict"
    assert rows(
        db_path, "SELECT company, stage, revision FROM applications ORDER BY id",
    ) == [("A", "backlog", 0), ("B", "backlog", 0)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(0,)]


def test_batch_commits_changed_items_and_reports_no_change_items(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1", stage="applied")
        add_application(conn, company="B", position="P2")

    result = execute_batch(db_path, [
        {"company": "A", "position": "P1", "changes": {"stage": "applied"}},
        {"company": "B", "position": "P2", "changes": {"stage": "applied"}},
    ])

    assert result["state"] == "completed"
    assert (result["changed_count"], result["no_change_count"]) == (1, 1)
    assert [item["status"] for item in result["results"]] == ["no_change", "completed"]
    assert rows(
        db_path, "SELECT company, stage, revision FROM applications ORDER BY id",
    ) == [("A", "applied", 0), ("B", "applied", 1)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(1,)]


def test_batch_replay_with_the_same_ids_is_idempotent(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")
        add_application(conn, company="B", position="P2")
    operation_ids = [str(uuid4()), str(uuid4())]
    turn_id = str(uuid4())
    commands = [
        {"company": "A", "position": "P1", "changes": {"stage": "applied"}},
        {"company": "B", "position": "P2", "changes": {"stage": "offer"}},
    ]

    first = execute_batch(
        db_path, commands, operation_ids=operation_ids, turn_id=turn_id,
    )
    replay = execute_batch(
        db_path, commands, operation_ids=operation_ids, turn_id=turn_id,
    )

    assert replay == first
    assert rows(
        db_path, "SELECT company, stage, revision FROM applications ORDER BY id",
    ) == [("A", "applied", 1), ("B", "offer", 1)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(2,)]


def test_batch_retries_database_busy_once_as_a_whole(tmp_path, monkeypatch):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")
    real_transaction = update_operations.transaction
    attempts = 0

    def flaky_transaction(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DatabaseBusy("busy")
        return real_transaction(*args, **kwargs)

    monkeypatch.setattr(update_operations, "transaction", flaky_transaction)
    result = execute_batch(db_path, [
        {"company": "A", "position": "P1", "changes": {"stage": "applied"}},
    ])

    assert result["state"] == "completed"
    assert attempts == 2
    assert rows(db_path, "SELECT stage, revision FROM applications") == [("applied", 1)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(1,)]


def test_batch_retries_after_busy_error_with_tentative_writes_rolled_back(
        tmp_path, monkeypatch):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")
        add_application(conn, company="B", position="P2")
    real_execute = update_operations._execute_application_update_operation
    first_application_attempts = 0
    busy_raised = False

    def flaky_execute(*args, **kwargs):
        nonlocal first_application_attempts, busy_raised
        if kwargs["company"] == "A":
            first_application_attempts += 1
        if kwargs["company"] == "B" and not busy_raised:
            busy_raised = True
            raise DatabaseBusy("busy after the first tentative write")
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(
        update_operations,
        "_execute_application_update_operation",
        flaky_execute,
    )
    result = execute_batch(db_path, [
        {"company": "A", "position": "P1", "changes": {"stage": "applied"}},
        {"company": "B", "position": "P2", "changes": {"stage": "offer"}},
    ])

    assert result["state"] == "completed"
    assert busy_raised is True
    assert first_application_attempts == 2
    assert rows(
        db_path, "SELECT company, stage, revision FROM applications ORDER BY id",
    ) == [("A", "applied", 1), ("B", "offer", 1)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(2,)]


def test_batch_redacts_validation_details_and_rolls_back_tentative_writes(
        tmp_path, monkeypatch):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")
        add_application(conn, company="B", position="P2")
    real_execute = update_operations._execute_application_update_operation
    sentinel = "PRIVATE_INVALID_INPUT_SENTINEL"

    def invalid_second_command(*args, **kwargs):
        if kwargs["company"] == "B":
            raise ValueError(sentinel)
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(
        update_operations,
        "_execute_application_update_operation",
        invalid_second_command,
    )
    result = execute_batch(db_path, [
        {"company": "A", "position": "P1", "changes": {"stage": "applied"}},
        {"company": "B", "position": "P2", "changes": {"stage": "offer"}},
    ])

    assert result["state"] == "rejected"
    assert result["issues"] == [{"index": 1, "reason": "conflict"}]
    assert sentinel not in json.dumps(result)
    assert rows(
        db_path, "SELECT company, stage, revision FROM applications ORDER BY id",
    ) == [("A", "backlog", 0), ("B", "backlog", 0)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(0,)]


def test_multi_item_batch_rejects_identity_changes_before_write(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")
        add_application(conn, company="B", position="P2")

    with pytest.raises(ValueError, match="不能在同一批中改公司或岗位名"):
        execute_batch(db_path, [
            {"company": "A", "position": "P1", "changes": {"company": "A2"}},
            {"company": "B", "position": "P2", "changes": {"stage": "applied"}},
        ])

    assert rows(
        db_path, "SELECT company, stage, revision FROM applications ORDER BY id",
    ) == [("A", "backlog", 0), ("B", "backlog", 0)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(0,)]


def test_batch_rejects_two_selectors_that_resolve_to_the_same_application(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn, company="A", position="P1")

    result = execute_batch(db_path, [
        {"company": "A", "changes": {"stage": "applied"}},
        {"company": "A", "position": "P1", "changes": {"priority": "high"}},
    ])

    assert result["state"] == "rejected"
    assert result["issues"] == [{"index": 1, "reason": "duplicate_target"}]
    assert rows(
        db_path, "SELECT stage, priority, revision FROM applications",
    ) == [("backlog", None, 0)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(0,)]


def test_expected_projection_prevents_stale_partial_next_action_overwrite(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn, stage="interviewing")
        conn.execute(
            "UPDATE applications SET next_stage='interviewing', next_step='终面', "
            "next_date='2026-07-25', revision=1 WHERE id=?",
            (application_id,),
        )

    with pytest.raises(
        operations.ApplicationUpdateOperationConflict,
        match="读取下一步后更新",
    ):
        execute(
            db_path,
            changes={
                "next_action": {
                    "stage": "interviewing",
                    "step": "一面",
                    "date": "2026-07-20",
                    "time": None,
                    "note": "旧读取合并结果",
                },
            },
            expected_application_id=application_id,
            expected_revision=0,
        )

    assert rows(
        db_path,
        "SELECT next_step, next_date, revision FROM applications WHERE id=?",
        application_id,
    ) == [("终面", "2026-07-25", 1)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id IS NOT NULL") == [(0,)]


def test_other_valid_operation_family_in_same_turn_is_ignored(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    turn_id = str(uuid4())
    preference_operation = execute_preference_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=turn_id,
        changes=[{"op": "set", "key": "城市", "value": "哥本哈根"}],
    )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="身份已损坏"):
        operations.get_application_update_operation(
            db_path, "u1", preference_operation["operation_id"],
        )

    application_operation = execute(
        db_path,
        turn_id=turn_id,
        changes={"stage": "applied"},
    )

    assert operations.list_application_update_operations_for_turn(
        db_path, "u1", turn_id,
    ) == [application_operation]
    assert rows(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE user_id='u1' AND operation_id IS NOT NULL",
    ) == [(2,)]


@pytest.mark.parametrize("damage", ["dual_family_key", "derivation_turn"])
def test_corrupted_other_family_candidate_blocks_application_update_before_write(
    tmp_path, damage,
):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    turn_id = str(uuid4())
    preference_operation = execute_preference_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=turn_id,
        changes=[{"op": "set", "key": "城市", "value": "哥本哈根"}],
    )

    with transaction(db_path) as conn:
        derivation_json = conn.execute(
            "SELECT derivation_json FROM journal WHERE operation_id=?",
            (preference_operation["operation_id"],),
        ).fetchone()[0]
        derivation = json.loads(derivation_json)
        if damage == "dual_family_key":
            derivation["operation"]["type"] = "preference_update"
        else:
            derivation["operation"]["client_turn_id"] = str(uuid4())
        conn.execute(
            "UPDATE journal SET derivation_json=? WHERE operation_id=?",
            (
                json.dumps(derivation, ensure_ascii=False),
                preference_operation["operation_id"],
            ),
        )

    before_application = rows(
        db_path,
        "SELECT id, stage, revision, updated_time FROM applications ORDER BY id",
    )
    before_journal = rows(
        db_path,
        "SELECT id, user_id, kind, operation_id, state, extraction_json, "
        "derivation_json, revision FROM journal ORDER BY id",
    )

    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="身份已损坏"):
        execute(
            db_path,
            operation_id=str(uuid4()),
            turn_id=turn_id,
            changes={"stage": "applied"},
        )

    assert rows(
        db_path,
        "SELECT id, stage, revision, updated_time FROM applications ORDER BY id",
    ) == before_application
    assert rows(
        db_path,
        "SELECT id, user_id, kind, operation_id, state, extraction_json, "
        "derivation_json, revision FROM journal ORDER BY id",
    ) == before_journal


def test_broad_turn_candidate_cap_fails_closed_before_write(tmp_path, monkeypatch):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    turn_id = str(uuid4())
    execute_preference_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=turn_id,
        changes=[{"op": "set", "key": "城市", "value": "哥本哈根"}],
    )
    execute(db_path, turn_id=turn_id, changes={"stage": "applied"})
    monkeypatch.setattr(update_operations, "MAX_TURN_OPERATION_CANDIDATES", 1)
    before_application = rows(
        db_path, "SELECT stage, revision, updated_time FROM applications",
    )
    before_journal = rows(db_path, "SELECT COUNT(*) FROM journal")

    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="候选.*上限"):
        execute(db_path, turn_id=turn_id, changes={"stage": "offer"})

    assert rows(
        db_path, "SELECT stage, revision, updated_time FROM applications",
    ) == before_application
    assert rows(db_path, "SELECT COUNT(*) FROM journal") == before_journal


@pytest.mark.parametrize(
    "damage",
    [
        "kind",
        "extraction_family_missing",
        "derivation_family_conflict",
        "both_families_unknown",
        "extraction_turn",
        "derivation_turn",
    ],
)
def test_broad_locator_corruption_fails_closed_before_new_turn_write(tmp_path, damage):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    turn_id = str(uuid4())
    operation = execute(db_path, turn_id=turn_id, changes={"stage": "applied"})

    with transaction(db_path) as conn:
        extraction_json, derivation_json = conn.execute(
            "SELECT extraction_json, derivation_json FROM journal WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()
        extraction = json.loads(extraction_json)
        derivation = json.loads(derivation_json)
        if damage == "kind":
            conn.execute(
                "UPDATE journal SET kind='review' WHERE operation_id=?",
                (operation["operation_id"],),
            )
        else:
            if damage == "extraction_family_missing":
                extraction.pop("operation_type")
            elif damage == "derivation_family_conflict":
                derivation["operation"]["type"] = "review_record"
            elif damage == "both_families_unknown":
                extraction["operation_type"] = "future_operation"
                derivation["operation"]["type"] = "future_operation"
            elif damage == "extraction_turn":
                extraction["client_turn_id"] = str(uuid4())
            else:
                derivation["operation"]["client_turn_id"] = str(uuid4())
            conn.execute(
                "UPDATE journal SET extraction_json=?, derivation_json=? "
                "WHERE operation_id=?",
                (
                    json.dumps(extraction, ensure_ascii=False),
                    json.dumps(derivation, ensure_ascii=False),
                    operation["operation_id"],
                ),
            )

    before_application = rows(
        db_path,
        "SELECT stage, revision, updated_time FROM applications",
    )
    before_journal = rows(
        db_path,
        "SELECT id, kind, operation_id, state, extraction_json, derivation_json, revision "
        "FROM journal ORDER BY id",
    )
    assert operations.get_application_update_operation(
        db_path, "other", operation["operation_id"],
    ) is None
    assert operations.list_application_update_operations_for_turn(
        db_path, "other", turn_id,
    ) == []
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="损坏"):
        operations.get_application_update_operation(
            db_path, "u1", operation["operation_id"],
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="损坏"):
        operations.list_application_update_operations_for_turn(db_path, "u1", turn_id)
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="损坏"):
        execute(
            db_path,
            operation_id=str(uuid4()),
            turn_id=turn_id,
            changes={"stage": "offer"},
        )
    assert rows(
        db_path,
        "SELECT stage, revision, updated_time FROM applications",
    ) == before_application
    assert rows(
        db_path,
        "SELECT id, kind, operation_id, state, extraction_json, derivation_json, revision "
        "FROM journal ORDER BY id",
    ) == before_journal


def test_stage_update_replay_noop_and_conditional_undo(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn)
    operation_id = str(uuid4())
    turn_id = str(uuid4())

    completed = execute(db_path, operation_id, turn_id)
    replay = execute(db_path, operation_id, turn_id)
    assert completed == replay
    assert completed["state"] == "completed" and completed["undo_available"]
    assert completed["target"] == {
        "application_id": application_id,
        "company": "A",
        "position": "P",
        "application_created_time": completed["target"]["application_created_time"],
    }
    assert completed["before"]["stage"] == "backlog"
    assert completed["final"]["stage"] == "applied"
    assert completed["final"]["revision"] == 1
    assert completed["effect"]["prep_invalidated"] is False
    assert operations.list_application_update_operations_for_turn(
        db_path, "u1", turn_id,
    ) == [completed]
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="另一条"):
        execute(
            db_path, operation_id, turn_id,
            changes={"stage": "offer"},
        )

    no_change = execute(
        db_path, str(uuid4()), str(uuid4()), changes={"stage": "applied"},
    )
    assert no_change == {
        "status": "no_change", "application_id": application_id,
        "company": "A", "position": "P",
    }
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE kind='correction'") == [(1,)]

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_heartbeat_time='heartbeat' WHERE id=?",
            (application_id,),
        )
    assert operations.get_application_update_operation(
        db_path, "u1", operation_id,
    )["undo_available"] is True
    undo_command_id = str(uuid4())
    undone = operations.undo_application_update_operation(
        db_path, "u1", operation_id, command_id=undo_command_id,
    )
    assert undone["state"] == "undone"
    assert operations.undo_application_update_operation(
        db_path, "u1", operation_id, command_id=undo_command_id,
    ) == undone
    assert rows(
        db_path, "SELECT stage, revision, priority, prep_heartbeat_time FROM applications",
    ) == [("backlog", 2, None, "heartbeat")]


def test_priority_update_and_undo_preserve_enum_and_null(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn)

    operation = execute(
        db_path,
        str(uuid4()),
        str(uuid4()),
        changes={"priority": "high"},
    )
    assert operation["before"]["priority"] is None
    assert operation["final"]["priority"] == "high"
    assert rows(db_path, "SELECT priority, revision FROM applications WHERE id=?", application_id) == [
        ("high", 1),
    ]

    undone = operations.undo_application_update_operation(
        db_path,
        "u1",
        operation["operation_id"],
        command_id=str(uuid4()),
    )
    assert undone["state"] == "undone"
    assert rows(db_path, "SELECT priority, revision FROM applications WHERE id=?", application_id) == [
        (None, 2),
    ]


def _add_review_provenance(conn, application_id: int):
    stamp = now_iso()
    journal_id = conn.execute(
        "INSERT INTO journal (user_id, kind, content, created_time, state) "
        "VALUES ('u1', 'review', 'review', ?, 'applied')",
        (stamp,),
    ).lastrowid
    question_id = conn.execute(
        "INSERT INTO questions (user_id, text, source, company, application_id, journal_id, "
        "created_time, updated_time) VALUES ('u1', 'Q', 'real', 'A', ?, ?, ?, ?)",
        (application_id, journal_id, stamp, stamp),
    ).lastrowid
    conn.execute(
        "INSERT INTO review_question_occurrences "
        "(user_id, journal_id, question_id, application_id, company, source_step, asked_date) "
        "VALUES ('u1', ?, ?, ?, 'A', '一面', '2026-07-13')",
        (journal_id, question_id, application_id),
    )
    return journal_id, question_id


def test_company_rename_freezes_provenance_and_undo_never_restores_prep(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        company_id = add_company(conn)
        application_id = add_application(conn, company_id=company_id, prep=True)
        journal_id, question_id = _add_review_provenance(conn, application_id)

    operation = execute(db_path, changes={"company": "B"})
    assert operation["effect"]["company_record_created"] is True
    assert operation["effect"]["prep_invalidated"] is True
    assert [item["id"] for item in operation["effect"]["question_provenance"]] == [question_id]
    assert operation["effect"]["question_occurrences"][0]["journal_id"] == journal_id
    assert rows(
        db_path,
        "SELECT a.company, c.name, a.prep_status, a.prep_json, q.company, o.company "
        "FROM applications a JOIN companies c ON c.id=a.company_id "
        "JOIN questions q ON q.application_id=a.id "
        "JOIN review_question_occurrences o ON o.application_id=a.id",
    ) == [("B", "B", "none", '{"ready": true}', "B", "B")]

    undone = operations.undo_application_update_operation(
        db_path, "u1", operation["operation_id"], command_id=str(uuid4()),
    )
    assert undone["state"] == "undone"
    assert rows(
        db_path,
        "SELECT company, company_id, prep_status, prep_json, revision FROM applications",
    ) == [("A", company_id, "none", '{"ready": true}', 2)]
    assert rows(db_path, "SELECT name FROM companies ORDER BY name") == [("A",), ("B",)]
    assert rows(db_path, "SELECT company FROM questions") == [("A",)]
    assert rows(db_path, "SELECT company FROM review_question_occurrences") == [("A",)]


def test_jd_update_invalidates_prep_and_is_conditionally_undoable(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(
            conn,
            jd_text="旧 JD：负责 Python",
            prep=True,
        )

    operation = execute(
        db_path,
        changes={"jd_text": "新 JD：负责 Python、FastAPI 与 PostgreSQL"},
    )

    assert operation["state"] == "completed"
    assert operation["before"]["jd_text"] == "旧 JD：负责 Python"
    assert operation["final"]["jd_text"].startswith("新 JD")
    assert operation["effect"]["changed_fields"] == [{
        "field": "jd_text",
        "before": "旧 JD：负责 Python",
        "after": "新 JD：负责 Python、FastAPI 与 PostgreSQL",
    }]
    assert operation["effect"]["prep_invalidated"] is True
    assert rows(
        db_path,
        "SELECT jd_text, prep_status, prep_json, revision FROM applications WHERE id=?",
        application_id,
    ) == [("新 JD：负责 Python、FastAPI 与 PostgreSQL", "none", '{"ready": true}', 1)]

    undone = operations.undo_application_update_operation(
        db_path,
        "u1",
        operation["operation_id"],
        command_id=str(uuid4()),
    )

    assert undone["state"] == "undone"
    assert rows(
        db_path,
        "SELECT jd_text, prep_status, prep_json, revision FROM applications WHERE id=?",
        application_id,
    ) == [("旧 JD：负责 Python", "none", '{"ready": true}', 2)]


def test_jd_update_undo_is_blocked_after_later_jd_change(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn, jd_text="旧 JD")
    operation = execute(db_path, changes={"jd_text": "新 JD"})
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET jd_text='后来再次修改的 JD' WHERE id=?",
            (application_id,),
        )

    canonical = operations.get_application_update_operation(
        db_path,
        "u1",
        operation["operation_id"],
    )

    assert canonical["undo_available"] is False
    assert canonical["undo_block_reason"] == "target_changed"


@pytest.mark.parametrize("drift", ["ready_artifact", "generation", "heartbeat"])
def test_identity_undo_never_clears_prep_created_after_apply(tmp_path, drift):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        company_id = add_company(conn)
        application_id = add_application(conn, company_id=company_id, prep=True)
    operation = execute(db_path, changes={"position": "Q"})
    with transaction(db_path) as conn:
        if drift == "ready_artifact":
            conn.execute(
                "UPDATE applications SET prep_status='ready', prep_generation='new-gen', "
                "prep_heartbeat_time='new-heartbeat', prep_json='{\"new\":true}' WHERE id=?",
                (application_id,),
            )
        elif drift == "generation":
            conn.execute(
                "UPDATE applications SET prep_generation='new-gen' WHERE id=?",
                (application_id,),
            )
        else:
            conn.execute(
                "UPDATE applications SET prep_heartbeat_time='new-heartbeat' WHERE id=?",
                (application_id,),
            )
    before = rows(
        db_path,
        "SELECT position, prep_status, prep_generation, prep_heartbeat_time, prep_json "
        "FROM applications WHERE id=?",
        application_id,
    )
    canonical = operations.get_application_update_operation(
        db_path, "u1", operation["operation_id"],
    )
    assert canonical["undo_available"] is False
    assert canonical["undo_block_reason"] == "prep_changed"
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="准备材料"):
        operations.undo_application_update_operation(
            db_path, "u1", operation["operation_id"], command_id=str(uuid4()),
        )
    assert rows(
        db_path,
        "SELECT position, prep_status, prep_generation, prep_heartbeat_time, prep_json "
        "FROM applications WHERE id=?",
        application_id,
    ) == before


@pytest.mark.parametrize("later_write", ["progress", "next_action", "stage"])
def test_stage_undo_refuses_every_later_state_write(tmp_path, later_write):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn, stage="applied")
    operation = execute(db_path, changes={"stage": "interviewing"})
    assert operation["before"]["stage"] == "applied"

    if later_write == "progress":
        update_operations.repository.record_application_progress(
            db_path,
            "u1",
            application_id,
            expected_revision=1,
            step="一面",
            occurred_date="2026-07-13",
            outcome="passed",
            summary="完成一面",
            update_current_state=True,
            target_stage=None,
            target_step="一面",
            replace_next_action=False,
            next_action=None,
        )
    elif later_write == "next_action":
        update_operations.repository.set_application_next_action(
            db_path,
            "u1",
            application_id,
            expected_revision=1,
            next_action={
                "stage": "interviewing",
                "step": "二面",
                "date": "2026-07-20",
                "time": None,
                "note": None,
            },
        )
    else:
        update_operations.repository.move_application_stage(
            db_path,
            "u1",
            application_id,
            expected_revision=1,
            stage="offer",
        )

    canonical = operations.get_application_update_operation(
        db_path, "u1", operation["operation_id"],
    )
    assert canonical["undo_available"] is False
    assert canonical["undo_block_reason"] == "target_changed"
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="此后又被修改"):
        operations.undo_application_update_operation(
            db_path, "u1", operation["operation_id"], command_id=str(uuid4()),
        )
    assert rows(db_path, "SELECT revision FROM applications") == [(2,)]


def test_state_update_and_undo_append_factual_timeline_entries(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn, stage="applied")
    operation = execute(
        db_path,
        changes={"stage": "interviewing", "current_step": "一面"},
    )
    assert operation["result"]["apply"]["timeline_entry_id"] is not None
    operations.undo_application_update_operation(
        db_path, "u1", operation["operation_id"], command_id=str(uuid4()),
    )
    assert rows(
        db_path,
        "SELECT source, from_stage, from_step, to_stage, to_step "
        "FROM timeline_entries WHERE application_id=? ORDER BY id",
        application_id,
    ) == [
        ("agent", "applied", None, "interviewing", "一面"),
        ("agent", "interviewing", "一面", "applied", None),
    ]


@pytest.mark.parametrize("drift", ["new_question", "question_aba", "journal_revision"])
def test_company_undo_refuses_any_frozen_provenance_drift(tmp_path, drift):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        company_id = add_company(conn)
        application_id = add_application(conn, company_id=company_id)
        journal_id, question_id = _add_review_provenance(conn, application_id)
    operation = execute(db_path, changes={"company": "B"})
    with transaction(db_path) as conn:
        if drift == "new_question":
            stamp = now_iso()
            conn.execute(
                "INSERT INTO questions (user_id, text, source, company, application_id, "
                "created_time, updated_time) VALUES ('u1', 'later', 'real', 'B', ?, ?, ?)",
                (application_id, stamp, stamp),
            )
        elif drift == "question_aba":
            conn.execute(
                "UPDATE questions SET company='away', updated_time='away' WHERE id=?",
                (question_id,),
            )
            conn.execute(
                "UPDATE questions SET company='B', updated_time='back' WHERE id=?",
                (question_id,),
            )
        else:
            conn.execute(
                "UPDATE journal SET revision=revision+1 WHERE id=?", (journal_id,),
            )
    canonical = operations.get_application_update_operation(
        db_path, "u1", operation["operation_id"],
    )
    assert canonical["state"] == "completed"
    assert canonical["undo_available"] is False
    assert canonical["undo_block_reason"] == "provenance_changed"
    with pytest.raises(operations.ApplicationUpdateOperationConflict):
        operations.undo_application_update_operation(
            db_path, "u1", operation["operation_id"], command_id=str(uuid4()),
        )
    assert rows(db_path, "SELECT company FROM applications") == [("B",)]


def test_revision_blocks_stage_and_company_identity_aba(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        company_id = add_company(conn)
        application_id = add_application(conn, company_id=company_id)
    operation = execute(db_path)
    update_operations.repository.move_application_stage(
        db_path, "u1", application_id, expected_revision=1, stage="offer",
    )
    update_operations.repository.move_application_stage(
        db_path, "u1", application_id, expected_revision=2, stage="applied",
    )
    canonical = operations.get_application_update_operation(
        db_path, "u1", operation["operation_id"],
    )
    assert canonical["undo_block_reason"] == "target_changed"
    assert rows(db_path, "SELECT revision FROM applications") == [(3,)]

    renamed = execute(db_path, changes={"company": "Other"})
    operations.undo_application_update_operation(
        db_path, "u1", renamed["operation_id"], command_id=str(uuid4()),
    )
    assert rows(db_path, "SELECT company, company_id, revision FROM applications") == [
        ("A", company_id, 5),
    ]


def test_collision_uses_frozen_ids_and_delete_recreate_cannot_create_merge_card(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(conn, position="P1")
        destination_id = add_application(conn, position="P2")
    collision = execute(
        db_path, position="P1", changes={"position": "P2", "stage": "offer"},
    )
    assert collision == {
        "status": "merge_required",
        "source_id": source_id,
        "destination_id": destination_id,
        "source_company": "A",
        "source_position": "P1",
        "source_stage": "backlog",
        "destination_company": "A",
        "destination_position": "P2",
    }
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE kind='correction'") == [(0,)]
    with transaction(db_path) as conn:
        conn.execute("DELETE FROM applications WHERE id=?", (source_id,))
        replacement = add_application(conn, position="P1")
    assert replacement > destination_id
    assert operations.prepare_application_merge_operation(
        db_path,
        "u1",
        source_application_id=source_id,
        source_company="A",
        source_position="P1",
        destination_application_id=destination_id,
        destination_company="A",
        destination_position="P2",
    ) == {"status": "not_found"}
    assert operations.list_pending_application_merge_operations(db_path, "u1") == []


def test_same_update_and_undo_are_concurrently_idempotent(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    operation_id = str(uuid4())
    turn_id = str(uuid4())

    def apply(_):
        return execute(db_path, operation_id, turn_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        applied = list(pool.map(apply, range(8)))
    assert all(item == applied[0] for item in applied)
    assert rows(db_path, "SELECT stage, revision FROM applications") == [("applied", 1)]
    assert rows(db_path, "SELECT COUNT(*) FROM journal WHERE operation_id=?", operation_id) == [(1,)]

    undo_command_id = str(uuid4())

    def undo(_):
        return operations.undo_application_update_operation(
            db_path, "u1", operation_id, command_id=undo_command_id,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        undone = list(pool.map(undo, range(8)))
    assert all(item == undone[0] for item in undone)
    assert undone[0]["state"] == "undone"
    assert rows(db_path, "SELECT stage, revision FROM applications") == [("backlog", 2)]
    assert rows(
        db_path,
        "SELECT command_id, operation_id, state FROM application_update_undo_commands",
    ) == [(undo_command_id, operation_id, "completed")]


def test_undo_command_status_is_the_only_late_commit_proof(tmp_path, monkeypatch):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    operation = execute(db_path)
    command_id = str(uuid4())
    entered_transaction = Event()
    release_transaction = Event()
    original = update_operations.repository._undo_application_update_in_transaction

    def paused_undo(*args, **kwargs):
        entered_transaction.set()
        if not release_transaction.wait(timeout=5):  # pragma: no cover - test deadlock guard
            raise RuntimeError("test did not release undo transaction")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        update_operations.repository,
        "_undo_application_update_in_transaction",
        paused_undo,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            operations.undo_application_update_operation,
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
        assert entered_transaction.wait(timeout=5)
        in_flight = operations.get_application_update_undo_command_status(
            db_path, "u1", command_id,
        )
        assert in_flight["state"] == "absent" and in_flight["terminal"] is False
        release_transaction.set()
        assert future.result(timeout=5)["state"] == "undone"

    terminal = operations.get_application_update_undo_command_status(
        db_path, "u1", command_id,
    )
    assert terminal["state"] == "completed" and terminal["terminal"] is True


def test_undo_rejection_replays_after_precondition_becomes_safe(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    operation = execute(db_path, changes={"position": "Q"})
    with transaction(db_path) as conn:
        blocker_id = add_application(conn, position="P")
    command_id = str(uuid4())

    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="占用"):
        operations.undo_application_update_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
    rejected = operations.get_application_update_undo_command_status(
        db_path, "u1", command_id,
    )
    assert rejected["state"] == "rejected" and rejected["terminal"] is True
    assert rejected["error"] == {
        "code": "natural_key_taken",
        "message": "原公司与岗位名已被另一条记录占用",
    }

    with transaction(db_path) as conn:
        conn.execute("DELETE FROM applications WHERE id = ?", (blocker_id,))
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="占用"):
        operations.undo_application_update_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
    assert operations.get_application_update_undo_command_status(
        db_path, "u1", command_id,
    ) == rejected

    undone = operations.undo_application_update_operation(
        db_path,
        "u1",
        operation["operation_id"],
        command_id=str(uuid4()),
    )
    assert undone["state"] == "undone"


def test_update_locates_application_across_internal_space(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        app_id = add_application(conn, company="字节", position="AI应用工程师")
    execute(db_path, company="字节", position="AI 应用工程师", changes={"stage": "applied"})
    assert rows(db_path, "SELECT stage FROM applications WHERE id = ?", app_id) == [("applied",)]


def test_undo_command_cannot_be_rebound_to_another_operation(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
        add_application(conn, company="B")
    first = execute(db_path)
    second = execute(db_path, company="B")
    command_id = str(uuid4())
    operations.undo_application_update_operation(
        db_path, "u1", first["operation_id"], command_id=command_id,
    )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="另一条"):
        operations.undo_application_update_operation(
            db_path, "u1", second["operation_id"], command_id=command_id,
        )
    assert operations.get_application_update_operation(
        db_path, "u1", second["operation_id"],
    )["state"] == "completed"
    assert operations.get_application_update_undo_command_status(
        db_path, "u1", command_id,
    )["operation_id"] == first["operation_id"]


def test_undo_command_receipt_collateral_write_rolls_back_business_undo(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn)
    operation = execute(db_path)
    command_id = str(uuid4())
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER collateral_undo_command "
            "AFTER INSERT ON application_update_undo_commands BEGIN "
            "UPDATE applications SET priority='high' WHERE id=1; END",
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="额外写入"):
        operations.undo_application_update_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )
    assert rows(
        db_path,
        "SELECT stage, revision, priority FROM applications WHERE id=?",
        application_id,
    ) == [("applied", 1, None)]
    assert rows(
        db_path,
        "SELECT state, revision FROM journal WHERE operation_id=?",
        operation["operation_id"],
    ) == [("applied", 0)]
    assert operations.get_application_update_undo_command_status(
        db_path, "u1", command_id,
    )["state"] == "absent"


def test_corrupt_undo_command_receipt_fails_closed(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    operation = execute(db_path)
    command_id = str(uuid4())
    operations.undo_application_update_operation(
        db_path, "u1", operation["operation_id"], command_id=command_id,
    )
    with transaction(db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE application_update_undo_commands SET finished_time='' "
            "WHERE user_id='u1' AND command_id=?",
            (command_id,),
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="命令回执已损坏"):
        operations.get_application_update_undo_command_status(db_path, "u1", command_id)
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="命令回执已损坏"):
        operations.undo_application_update_operation(
            db_path,
            "u1",
            operation["operation_id"],
            command_id=command_id,
        )


def test_write_boundary_cap_cross_tenant_and_collateral_trigger_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(update_operations, "MAX_OPERATIONS_PER_TURN", 2)
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn)
    turn_id = str(uuid4())
    execute(db_path, turn_id=turn_id, changes={"stage": "applied"})
    execute(db_path, turn_id=turn_id, changes={"stage": "offer"})
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="安全上限"):
        execute(db_path, turn_id=turn_id, changes={"stage": "rejected"})
    assert len(operations.list_application_update_operations_for_turn(
        db_path, "u1", turn_id,
    )) == 2

    with transaction(db_path) as conn:
        stamp = now_iso()
        conn.execute(
            "INSERT INTO questions (user_id, text, source, company, application_id, "
            "created_time, updated_time) VALUES ('evil', 'cross', 'real', 'A', ?, ?, ?)",
            (application_id, stamp, stamp),
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="跨租户"):
        execute(db_path, changes={"company": "B"})
    assert rows(db_path, "SELECT company FROM applications") == [("A",)]

    with transaction(db_path) as conn:
        conn.execute("DELETE FROM questions WHERE user_id='evil'")
        conn.execute(
            "CREATE TRIGGER collateral_update AFTER INSERT ON journal "
            "WHEN NEW.kind='correction' BEGIN "
            "UPDATE applications SET priority='high' WHERE id=1; END",
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="额外写入"):
        execute(db_path, changes={"position": "Q"})
    assert rows(
        db_path, "SELECT position, priority, revision FROM applications",
    ) == [("P", None, 2)]


def test_noncanonical_application_cas_timestamp_fails_before_any_write(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn)
        conn.execute(
            "UPDATE applications SET created_time=' token ', updated_time=' token ' "
            "WHERE id=?",
            (application_id,),
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="安全契约"):
        execute(db_path)
    assert rows(
        db_path,
        "SELECT stage, revision, created_time, updated_time FROM applications",
    ) == [("backlog", 0, " token ", " token ")]
    assert rows(db_path, "SELECT COUNT(*) FROM journal") == [(0,)]


@pytest.mark.parametrize("table", ["questions", "review_question_occurrences"])
def test_noncanonical_provenance_company_fails_closed_without_private_error(tmp_path, table):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        company_id = add_company(conn)
        application_id = add_application(conn, company_id=company_id)
        _add_review_provenance(conn, application_id)
        conn.execute(f"UPDATE {table} SET company=' A '")
    with pytest.raises(
        operations.ApplicationUpdateOperationConflict,
        match="provenance|出处",
    ):
        execute(db_path, changes={"company": "B"})
    assert rows(
        db_path,
        "SELECT company, revision FROM applications WHERE id=?",
        application_id,
    ) == [("A", 0)]
    assert rows(db_path, f"SELECT company FROM {table}") == [(" A ",)]
    assert rows(db_path, "SELECT name FROM companies ORDER BY id") == [("A",)]
    assert rows(
        db_path, "SELECT COUNT(*) FROM journal WHERE kind='correction'",
    ) == [(0,)]


def test_corrupt_receipt_is_stale_and_never_undoes(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    operation = execute(db_path)
    with transaction(db_path) as conn:
        derivation = json.loads(conn.execute(
            "SELECT derivation_json FROM journal WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()[0])
        derivation["operation"]["apply"]["result"]["revision"] = 99
        conn.execute(
            "UPDATE journal SET derivation_json=? WHERE operation_id=?",
            (json.dumps(derivation), operation["operation_id"]),
        )
    stale = operations.get_application_update_operation(
        db_path, "u1", operation["operation_id"],
    )
    assert stale["state"] == "stale" and stale["result"] is None
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="损坏"):
        operations.undo_application_update_operation(
            db_path, "u1", operation["operation_id"], command_id=str(uuid4()),
        )
    assert rows(db_path, "SELECT stage FROM applications") == [("applied",)]


def test_corrupt_proposal_cannot_cross_identity_in_by_turn_recovery(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        add_application(conn)
    turn_id = str(uuid4())
    operation = execute(db_path, turn_id=turn_id)
    with transaction(db_path) as conn:
        extraction = json.loads(conn.execute(
            "SELECT extraction_json FROM journal WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()[0])
        extraction["target"]["application_id"] = 0
        conn.execute(
            "UPDATE journal SET extraction_json=? WHERE operation_id=?",
            (json.dumps(extraction), operation["operation_id"]),
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="proposal 身份已损坏"):
        operations.get_application_update_operation(
            db_path, "u1", operation["operation_id"],
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="proposal 身份已损坏"):
        operations.list_application_update_operations_for_turn(db_path, "u1", turn_id)


def test_undo_journal_collateral_trigger_rolls_back_every_write(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        application_id = add_application(conn)
    operation = execute(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER collateral_undo AFTER UPDATE OF state ON journal "
            "WHEN NEW.operation_id IS NOT NULL BEGIN "
            "UPDATE applications SET priority='high' WHERE id=1; END",
        )
    with pytest.raises(operations.ApplicationUpdateOperationConflict, match="额外写入"):
        operations.undo_application_update_operation(
            db_path, "u1", operation["operation_id"], command_id=str(uuid4()),
        )
    assert rows(
        db_path,
        "SELECT stage, revision, priority FROM applications WHERE id=?",
        application_id,
    ) == [("applied", 1, None)]
    assert rows(
        db_path,
        "SELECT state, revision FROM journal WHERE operation_id=?",
        operation["operation_id"],
    ) == [("applied", 0)]
    canonical = operations.get_application_update_operation(
        db_path, "u1", operation["operation_id"],
    )
    assert canonical["state"] == "completed" and canonical["undo_available"] is True


def test_merge_destination_revision_blocks_older_update_undo(tmp_path):
    db_path = make_db(tmp_path)
    with transaction(db_path) as conn:
        source_id = add_application(conn, position="source")
        destination_id = add_application(conn, position="destination")
    update = execute(
        db_path, position="destination", changes={"stage": "applied"},
    )
    merge = operations.prepare_application_merge_operation(
        db_path,
        "u1",
        source_application_id=source_id,
        source_company="A",
        source_position="source",
        destination_application_id=destination_id,
        destination_company="A",
        destination_position="destination",
    )
    operations.approve_application_merge_operation(db_path, "u1", merge["operation_id"])
    canonical = operations.get_application_update_operation(
        db_path, "u1", update["operation_id"],
    )
    assert canonical["undo_available"] is False
    assert canonical["undo_block_reason"] == "target_changed"


def test_http_get_by_turn_get_by_id_and_empty_undo(client):
    test_client, db_path = client
    with transaction(db_path) as conn:
        add_application(conn, user_id="me")
    operation_id = str(uuid4())
    turn_id = str(uuid4())
    operation = operations.execute_application_update_operation(
        db_path,
        "me",
        operation_id=operation_id,
        client_turn_id=turn_id,
        company="A",
        position="P",
        changes={"stage": "applied"},
    )
    assert operation["contract_version"] == 1
    base = f"/api/timeline/application-update-operations/{operation_id}"
    assert test_client.get(base).json() == operation
    assert test_client.get(
        f"/api/timeline/application-update-operations/by-client-turn/{turn_id}",
    ).json() == {"operations": [operation]}
    assert test_client.get(
        f"/api/timeline/application-update-operations/by-client-turn/{turn_id}",
        headers={"Remote-User": "other"},
    ).json() == {"operations": []}
    assert test_client.post(base + "/undo").status_code == 422
    assert test_client.post(base + "/undo", json={"confirmed": True}).status_code == 422
    command_id = str(uuid4())
    command_base = f"/api/timeline/application-update-undo-commands/{command_id}"
    assert test_client.get(command_base).json() == {
        "command_id": command_id,
        "operation_id": None,
        "state": "absent",
        "terminal": False,
        "error": None,
        "finished_time": None,
    }
    undone = test_client.post(base + "/undo", json={"command_id": command_id})
    assert undone.status_code == 200 and undone.json()["state"] == "undone"
    assert test_client.post(
        base + "/undo", json={"command_id": command_id},
    ).json() == undone.json()
    completed_status = test_client.get(command_base).json()
    assert completed_status["command_id"] == command_id
    assert completed_status["operation_id"] == operation_id
    assert completed_status["state"] == "completed"
    assert completed_status["terminal"] is True
    assert completed_status["error"] is None
    assert completed_status["finished_time"]
    assert test_client.get(
        command_base, headers={"Remote-User": "other"},
    ).json()["state"] == "absent"
    assert test_client.get(base, headers={"Remote-User": "other"}).status_code == 404
    assert test_client.post(
        base + "/undo",
        json={"command_id": command_id},
        headers={"Remote-User": "other"},
    ).status_code == 404
    other_status = test_client.get(
        command_base, headers={"Remote-User": "other"},
    ).json()
    assert other_status["state"] == "rejected"
    assert other_status["error"]["code"] == "operation_not_found"
    assert test_client.get(command_base).json() == completed_status


def test_http_by_turn_single_side_turn_corruption_is_409_and_tenant_private(client):
    test_client, db_path = client
    with transaction(db_path) as conn:
        add_application(conn, user_id="me")
    operation_id = str(uuid4())
    turn_id = str(uuid4())
    operations.execute_application_update_operation(
        db_path,
        "me",
        operation_id=operation_id,
        client_turn_id=turn_id,
        company="A",
        position="P",
        changes={"stage": "applied"},
    )
    with transaction(db_path) as conn:
        extraction = json.loads(conn.execute(
            "SELECT extraction_json FROM journal WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0])
        extraction["client_turn_id"] = str(uuid4())
        conn.execute(
            "UPDATE journal SET extraction_json=? WHERE operation_id=?",
            (json.dumps(extraction, ensure_ascii=False), operation_id),
        )

    by_turn = f"/api/timeline/application-update-operations/by-client-turn/{turn_id}"
    by_id = f"/api/timeline/application-update-operations/{operation_id}"
    assert test_client.get(by_turn).status_code == 409
    assert test_client.get(by_id).status_code == 409
    assert test_client.get(
        by_turn,
        headers={"Remote-User": "other"},
    ).json() == {"operations": []}
    assert test_client.get(
        by_id,
        headers={"Remote-User": "other"},
    ).status_code == 404


def test_http_update_receipts_publish_versioned_openapi_models(client):
    test_client, _ = client
    openapi = test_client.app.openapi()
    paths = openapi["paths"]
    list_schema = paths[
        "/api/timeline/application-update-operations/by-client-turn/{client_turn_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    item_schema = paths[
        "/api/timeline/application-update-operations/{operation_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    undo_schema = paths[
        "/api/timeline/application-update-operations/{operation_id}/undo"
    ]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    command_schema = paths[
        "/api/timeline/application-update-undo-commands/{command_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert list_schema == {
        "$ref": "#/components/schemas/ApplicationUpdateOperationsResponse",
    }
    expected_item = {"$ref": "#/components/schemas/ApplicationUpdateOperationDTO"}
    assert item_schema == expected_item
    assert undo_schema == expected_item
    assert command_schema == {
        "$ref": "#/components/schemas/ApplicationUpdateUndoCommandStatus",
    }
    operation_schema = openapi["components"]["schemas"]["ApplicationUpdateOperationDTO"]
    assert "contract_version" in operation_schema["required"]
    assert operation_schema["properties"]["contract_version"]["const"] == 1
    undo_request = openapi["components"]["schemas"]["ApplicationUpdateUndoRequest"]
    assert undo_request["required"] == ["command_id"]
    assert undo_request["additionalProperties"] is False
