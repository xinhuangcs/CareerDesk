
import sqlite3

import pytest

from careerdesk.platform.database import init_db, read_connection
from careerdesk.platform.database import schema as database_schema


TABLE = "review_timeline_entry_edit_undo_commands"
INDEX = "idx_review_timeline_entry_edit_undo_commands_operation"
REVIEW_RECORD_INDEXES = {
    "uq_journal_review_record_turn",
    "uq_journal_review_record_source",
    "idx_journal_review_record_target",
}
PREFERENCE_INDEXES = {
    "idx_journal_operation_turn_extraction",
    "idx_journal_operation_turn_derivation",
    "uq_journal_preference_update_turn_extraction",
    "uq_journal_preference_update_turn_derivation",
}
INTAKE_OWNER_TABLE = "application_intake_operation_owners"
INTAKE_OWNER_INDEX = "idx_application_intake_owner_user_journal"
INTAKE_OWNER_TRIGGERS = {
    "trg_application_intake_owner_insert_contract",
    "trg_application_intake_owner_immutable_update",
    "trg_application_intake_owner_immutable_delete",
    "trg_application_intake_journal_identity_update",
    "trg_application_intake_journal_no_delete",
}
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
OTHER_COMMAND_ID = "22222222-2222-4222-8222-222222222222"
OPERATION_ID = "33333333-3333-4333-8333-333333333333"
OTHER_OPERATION_ID = "44444444-4444-4444-8444-444444444444"


def _insert_receipt(conn: sqlite3.Connection, values: tuple) -> None:
    conn.execute(
        f"INSERT INTO {TABLE} "
        "(user_id, command_id, operation_id, state, error_code, error_message, "
        "finished_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
        values,
    )


def test_fresh_database_installs_current_schema_contracts(tmp_path):
    path = str(tmp_path / "fresh-schema.db")

    init_db(path)
    init_db(path)

    with read_connection(path) as conn:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone()[0]
        index_columns = [
            row[2] for row in conn.execute(f"PRAGMA index_info({INDEX})").fetchall()
        ]
        foreign_keys = conn.execute(f"PRAGMA foreign_key_list({TABLE})").fetchall()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'",
            ).fetchall()
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        intake_owner_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (INTAKE_OWNER_TABLE,),
        ).fetchone()[0]
        intake_owner_foreign_keys = conn.execute(
            f"PRAGMA foreign_key_list({INTAKE_OWNER_TABLE})",
        ).fetchall()
        intake_owner_triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'",
            ).fetchall()
        }

    assert database_schema.FRESH_SCHEMA_REVISION == 1
    assert database_schema.SCHEMA_VERSION == 28 == version
    assert table_sql.endswith("STRICT")
    assert "PRIMARY KEY (user_id, command_id)" in table_sql
    assert "'completed', 'rejected'" in table_sql
    assert "'provenance_changed'" in table_sql
    assert index_columns == ["user_id", "operation_id", "finished_time"]
    assert REVIEW_RECORD_INDEXES | PREFERENCE_INDEXES <= indexes
    assert INTAKE_OWNER_INDEX in indexes
    assert foreign_keys == []
    assert intake_owner_sql.endswith("STRICT")
    assert intake_owner_foreign_keys[0][2:5] == ("journal", "journal_id", "id")
    assert INTAKE_OWNER_TRIGGERS <= intake_owner_triggers
    assert integrity == "ok"


def test_fresh_schema_uses_stage_projection_and_factual_timeline(tmp_path):
    path = str(tmp_path / "fresh-progress.db")
    init_db(path)

    with read_connection(path) as conn:
        application_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(applications)")
        }
        timeline_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(timeline_entries)")
        }
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
        triggers = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'",
            )
        }

    assert {
        "stage", "current_step", "current_state_entry_id", "next_stage",
        "next_step", "next_date", "next_time", "next_note", "paused_from_stage",
        "pause_reason", "application_note", "revision",
    } <= application_columns
    assert not {
        "status", "latest_round", "next_event_date", "next_event_note",
        "edit_revision",
    } & application_columns
    assert {
        "step", "occurred_date", "outcome", "summary", "from_stage", "from_step",
        "to_stage", "to_step", "source", "journal_id", "created_time",
    } <= timeline_columns
    assert "events" not in tables
    assert "trg_applications_edit_revision" not in triggers


@pytest.mark.parametrize(
    "values",
    [
        ("", COMMAND_ID, OPERATION_ID, "completed", None, None, "finished"),
        ("u1", "not-a-uuid", OPERATION_ID, "completed", None, None, "finished"),
        (
            "u1",
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            OPERATION_ID,
            "completed",
            None,
            None,
            "finished",
        ),
        ("u1", COMMAND_ID, "not-a-uuid", "completed", None, None, "finished"),
        ("u1", COMMAND_ID, OPERATION_ID, "running", None, None, "finished"),
        (
            "u1",
            COMMAND_ID,
            OPERATION_ID,
            "completed",
            "target_changed",
            "目标变化",
            "finished",
        ),
        ("u1", COMMAND_ID, OPERATION_ID, "rejected", None, None, "finished"),
        (
            "u1",
            COMMAND_ID,
            OPERATION_ID,
            "rejected",
            "unknown_code",
            "拒绝",
            "finished",
        ),
        (
            "u1",
            COMMAND_ID,
            OPERATION_ID,
            "rejected",
            "target_changed",
            "",
            "finished",
        ),
        (
            "u1",
            COMMAND_ID,
            OPERATION_ID,
            "rejected",
            "target_changed",
            " 两端空白 ",
            "finished",
        ),
        (
            "u1",
            COMMAND_ID,
            OPERATION_ID,
            "rejected",
            "target_changed",
            "x" * 257,
            "finished",
        ),
        ("u1", COMMAND_ID, OPERATION_ID, "completed", None, None, ""),
        ("u1", COMMAND_ID, OPERATION_ID, "completed", None, None, " padded "),
        ("u1", COMMAND_ID, OPERATION_ID, "completed", None, None, "x" * 65),
    ],
)
def test_review_timeline_entry_edit_undo_ledger_rejects_invalid_receipts(tmp_path, values):
    path = str(tmp_path / "invalid-review-timeline-entry-edit-undo.db")
    init_db(path)

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        _insert_receipt(conn, values)


def test_review_timeline_entry_edit_undo_ledger_scopes_identity_per_tenant(tmp_path):
    path = str(tmp_path / "tenant-review-timeline-entry-edit-undo.db")
    init_db(path)

    with sqlite3.connect(path) as conn:
        _insert_receipt(
            conn,
            ("u1", COMMAND_ID, OPERATION_ID, "completed", None, None, "finished-1"),
        )
        _insert_receipt(
            conn,
            ("u2", COMMAND_ID, OPERATION_ID, "completed", None, None, "finished-2"),
        )
        _insert_receipt(
            conn,
            (
                "u1",
                OTHER_COMMAND_ID,
                OTHER_OPERATION_ID,
                "rejected",
                "operation_not_found",
                "复盘历程修改操作不存在",
                "finished-3",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_receipt(
                conn,
                (
                    "u1",
                    COMMAND_ID,
                    OTHER_OPERATION_ID,
                    "completed",
                    None,
                    None,
                    "finished-4",
                ),
            )

    with read_connection(path) as conn:
        rows = conn.execute(
            f"SELECT user_id, command_id, operation_id, state FROM {TABLE} "
            "ORDER BY user_id, command_id",
        ).fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

    assert rows == [
        ("u1", COMMAND_ID, OPERATION_ID, "completed"),
        ("u1", OTHER_COMMAND_ID, OTHER_OPERATION_ID, "rejected"),
        ("u2", COMMAND_ID, OPERATION_ID, "completed"),
    ]
    assert integrity == "ok"
