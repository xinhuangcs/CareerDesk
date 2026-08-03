
import hashlib
import json
import os
import sqlite3
import stat
import time
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.platform.database import connection as db
from careerdesk.platform.database.connection import now_iso
from careerdesk.platform.database import schema as database_schema


REVIEW_RECORD_INDEXES = {
    "uq_journal_review_record_turn",
    "uq_journal_review_record_source",
    "idx_journal_review_record_target",
}
@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def _insert_journal(conn, user_id: str = "u1") -> int:
    cursor = conn.execute(
        "INSERT INTO journal (user_id, kind, content, created_time) VALUES (?, 'review', '面了字节二面', ?)",
        (user_id, now_iso()),
    )
    return cursor.lastrowid


def _database_catalog(path: str | Path):
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        objects = conn.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
    return version, journal_mode, objects


def _forbid_read_write_open(monkeypatch) -> None:
    def fail_if_called(_private_path):
        raise AssertionError("database reached the read-write opener")

    monkeypatch.setattr(db, "_open_database_read_write", fail_if_called)


def _logical_database_snapshot(path: str | Path):
    with closing(sqlite3.connect(path)) as conn:
        return {
            "dump": tuple(conn.iterdump()),
            "version": conn.execute("PRAGMA user_version").fetchone()[0],
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_keys": conn.execute("PRAGMA foreign_key_check").fetchall(),
            "sequence": conn.execute(
                "SELECT name, seq FROM sqlite_sequence ORDER BY name"
            ).fetchall(),
        }


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_database_directory_main_file_and_wal_sidecars_are_private(tmp_path):
    path = tmp_path / "data" / "private.db"
    old_umask = os.umask(0)
    try:
        init_db(str(path))
        conn = db._connect(str(path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                assert candidate.exists()
                assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
            conn.rollback()
        finally:
            conn.close()
    finally:
        os.umask(old_umask)


def test_database_rejects_preexisting_wal_symlink_before_sqlite_opens_it(tmp_path):
    path = tmp_path / "data" / "private.db"
    init_db(str(path))
    wal = Path(f"{path}-wal")
    wal.unlink(missing_ok=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    try:
        wal.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="符号链接"):
        db._connect(str(path))

    assert wal.is_symlink() and outside.read_bytes() == b"keep"


def test_init_db_idempotent_and_versioned(db_path):
    with sqlite3.connect(db_path) as conn:
        before_dump = tuple(conn.iterdump())
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        after_dump = tuple(conn.iterdump())
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert before_dump == after_dump
    assert version == database_schema.SCHEMA_VERSION
    assert {"journal", "companies", "resumes", "applications", "timeline_entries", "knowledge_points",
            "questions", "question_knowledge", "grill_sessions", "grill_answers", "status_log",
            "assistant_turns"} <= tables


def test_current_schema_preserves_application_autoincrement_high_water(tmp_path):
    path = str(tmp_path / "current-high-water.db")
    init_db(path)

    with closing(sqlite3.connect(path)) as conn, conn:
        first_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'First Co', 'Engineer', 'created', 'updated')"
        ).lastrowid
        conn.execute("DELETE FROM applications WHERE id = ?", (first_id,))
        conn.execute(
            "UPDATE sqlite_sequence SET seq = 1000 WHERE name = 'applications'"
        )

    init_db(path)
    init_db(path)

    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'applications'"
        ).fetchone() == (1000,)
        second_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'Second Co', 'Engineer', 'created', 'updated')"
        ).lastrowid
        conn.execute("DELETE FROM applications WHERE id = ?", (second_id,))

    init_db(path)

    with closing(sqlite3.connect(path)) as conn, conn:
        third_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'Third Co', 'Engineer', 'created', 'updated')"
        ).lastrowid

    assert first_id == 1
    assert second_id == 1001
    assert third_id == 1002


def test_current_fresh_only_schema_declarations_are_frozen():
    assert database_schema.FRESH_SCHEMA_REVISION == 1
    assert database_schema.SCHEMA_VERSION == 28
    assert hashlib.sha256(database_schema.SCHEMA.encode()).hexdigest() == (
        "72658517bbba09e11f7f817bb3e7004b7faec414883d71643d56f5af648d5600"
    )
    assert hashlib.sha256(database_schema.INDEXES.encode()).hexdigest() == (
        "2c1ad8a30d439dbedf1be966bbd87c0662e9b350124d6f2ceb177bf6f1602c67"
    )
    assert hashlib.sha256(database_schema.TRIGGERS.encode()).hexdigest() == (
        "35c7e469110f8043f395a67feb2e50e87701cd076a34613ca9222e106698831c"
    )


@pytest.mark.parametrize("with_sentinel", [False, True], ids=["empty", "sentinel"])
def test_init_rejects_future_database_before_any_mutation(
    tmp_path, monkeypatch, with_sentinel,
):
    path = tmp_path / f"future-{'sentinel' if with_sentinel else 'empty'}.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        if with_sentinel:
            conn.execute("CREATE TABLE future_only (id INTEGER PRIMARY KEY) STRICT")
        conn.execute(
            f"PRAGMA user_version = {database_schema.SCHEMA_VERSION + 1}"
        )

    before_bytes = path.read_bytes()
    before_catalog = _database_catalog(path)
    assert before_catalog[1] == "delete"
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()

    _forbid_read_write_open(monkeypatch)
    with pytest.raises(RuntimeError, match="高于当前程序支持"):
        init_db(str(path))

    assert path.read_bytes() == before_bytes
    assert _database_catalog(path) == before_catalog
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_init_rejects_future_version_visible_only_in_crash_left_wal(
    tmp_path, monkeypatch,
):
    path = tmp_path / "future-in-wal.db"
    crash_writer = """
import os
import sqlite3
import sys

path, current_version = sys.argv[1], int(sys.argv[2])
conn = sqlite3.connect(path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE future_only (id INTEGER PRIMARY KEY) STRICT")
conn.execute(f"PRAGMA user_version = {current_version}")
conn.commit()
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.execute(f"PRAGMA user_version = {current_version + 1}")
conn.execute("INSERT INTO future_only DEFAULT VALUES")
conn.commit()
os._exit(0)
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            crash_writer,
            str(path),
            str(database_schema.SCHEMA_VERSION),
        ],
        check=True,
    )
    wal_path = Path(f"{path}-wal")
    assert wal_path.exists()
    before_main = path.read_bytes()
    before_wal = wal_path.read_bytes()
    assert (
        int.from_bytes(before_main[60:64], "big")
        == database_schema.SCHEMA_VERSION
    )

    _forbid_read_write_open(monkeypatch)
    with pytest.raises(RuntimeError, match="高于当前程序支持"):
        init_db(str(path))

    assert path.read_bytes() == before_main
    assert wal_path.read_bytes() == before_wal
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            database_schema.SCHEMA_VERSION + 1
        )
        assert conn.execute("SELECT COUNT(*) FROM future_only").fetchone()[0] == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert tables == {"future_only"}


def test_init_rejects_versioned_empty_database_instead_of_treating_it_as_fresh(
    tmp_path, monkeypatch,
):
    path = tmp_path / "versioned-empty.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"PRAGMA user_version = {database_schema.SCHEMA_VERSION}")
    before_bytes = path.read_bytes()
    before_catalog = _database_catalog(path)

    _forbid_read_write_open(monkeypatch)
    with pytest.raises(RuntimeError, match="没有任何表"):
        init_db(str(path))

    assert path.read_bytes() == before_bytes
    assert _database_catalog(path) == before_catalog
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


@pytest.mark.parametrize(
    "setup_sql",
    [
        "CREATE VIEW unknown_view AS SELECT 1 AS value;",
        "CREATE TABLE removed (id INTEGER PRIMARY KEY AUTOINCREMENT); "
        "DROP TABLE removed;",
    ],
    ids=["view-only", "sqlite-sequence-only"],
)
def test_init_rejects_unversioned_nonblank_catalog_before_read_write_open(
    tmp_path, monkeypatch, setup_sql,
):
    path = tmp_path / "unversioned-nonblank.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(setup_sql)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 0
    before_bytes = path.read_bytes()
    before_catalog = _database_catalog(path)

    _forbid_read_write_open(monkeypatch)
    with pytest.raises(RuntimeError, match="已包含 schema 对象"):
        init_db(str(path))

    assert path.read_bytes() == before_bytes
    assert _database_catalog(path) == before_catalog
    assert not Path(f"{path}-wal").exists()


def test_schema_is_fresh_only_without_a_migration_contract():
    assert database_schema.FRESH_SCHEMA_REVISION == 1
    assert database_schema.SCHEMA_VERSION == 28
    assert not hasattr(database_schema, "PREVIOUS_SCHEMA_VERSION")
    assert not hasattr(database_schema, "MIGRATIONS")


def test_read_connection_rejects_pre_baseline_version_visible_only_in_wal(
    tmp_path, monkeypatch,
):
    path = tmp_path / "old-version-in-wal.db"
    init_db(str(path))
    crash_writer = """
import os
import sqlite3
import sys

path, old_version = sys.argv[1], int(sys.argv[2])
conn = sqlite3.connect(path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.execute(f"PRAGMA user_version = {old_version}")
conn.execute("INSERT INTO meta(key, value) VALUES ('wal-old-version', 'KEEP')")
conn.commit()
os._exit(0)
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            crash_writer,
            str(path),
            "0",
        ],
        check=True,
    )
    wal_path = Path(f"{path}-wal")
    assert wal_path.exists()
    before_main = path.read_bytes()
    before_wal = wal_path.read_bytes()
    assert (
        int.from_bytes(before_main[60:64], "big")
        == database_schema.SCHEMA_VERSION
    )

    _forbid_read_write_open(monkeypatch)
    with pytest.raises(RuntimeError, match="未记录 schema 版本"):
        with read_connection(str(path)):
            pass

    assert path.read_bytes() == before_main
    assert wal_path.read_bytes() == before_wal
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'wal-old-version'"
        ).fetchone() == ("KEEP",)


@pytest.mark.parametrize(
    ("invalid_version", "message"),
    [(-1, "版本 v-1 无效"), (0, "未记录 schema 版本但已包含 schema 对象")],
)
def test_init_rejects_invalid_version_on_current_schema_without_writes(
    tmp_path,
    monkeypatch,
    invalid_version,
    message,
):
    path = tmp_path / f"invalid-version-{invalid_version}.db"
    init_db(str(path))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, application_note, prep_generation, "
            "prep_heartbeat_time, revision, created_time, updated_time) "
            "VALUES ('u1', 'Keep Co', 'Engineer', 'KEEP', 'GEN', 'HEART', 7, 'c0', 'u0')"
        )
        conn.execute(f"PRAGMA user_version = {invalid_version}")
    before_bytes = path.read_bytes()
    before_catalog = _database_catalog(path)

    _forbid_read_write_open(monkeypatch)
    with pytest.raises(RuntimeError, match=message):
        init_db(str(path))

    assert path.read_bytes() == before_bytes
    assert _database_catalog(path) == before_catalog
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT application_note, prep_generation, prep_heartbeat_time, revision "
            "FROM applications WHERE company = 'Keep Co'"
        ).fetchone()
    assert row == ("KEEP", "GEN", "HEART", 7)


def test_init_rejects_missing_current_table_without_recreating_it(tmp_path, monkeypatch):
    path = tmp_path / "damaged-current.db"
    init_db(str(path))
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE status_log")
    before_catalog = _database_catalog(path)

    _forbid_read_write_open(monkeypatch)
    with pytest.raises(RuntimeError, match="缺少必需 schema 对象") as caught:
        init_db(str(path))

    assert "表 status_log" in str(caught.value)
    assert _database_catalog(path) == before_catalog


@pytest.mark.parametrize(
    "drop_sql",
    [
        "DROP INDEX idx_journal_user_kind_time",
        "DROP TRIGGER trg_preferences_insert_revision",
    ],
)
def test_init_rebuilds_missing_current_derived_schema_objects(tmp_path, drop_sql):
    path = tmp_path / "recoverable-current.db"
    init_db(str(path))
    complete_catalog = _database_catalog(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(drop_sql)
    assert _database_catalog(path) != complete_catalog

    init_db(str(path))

    assert _database_catalog(path) == complete_catalog


def test_current_init_preserves_extension_objects_data_and_meta_state(tmp_path):
    path = tmp_path / "current-with-extensions.db"
    init_db(str(path))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE extension_probe (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT;
            INSERT INTO extension_probe (id, value) VALUES (1, 'KEEP');
            CREATE INDEX extension_probe_value ON extension_probe(value);
            CREATE TRIGGER extension_probe_immutable
            BEFORE DELETE ON extension_probe
            BEGIN
                SELECT RAISE(ABORT, 'extension row is immutable');
            END;
            CREATE VIEW extension_probe_view AS
                SELECT id, value FROM extension_probe;
            UPDATE meta SET value = '1' WHERE key = 'extract_version';
            """
        )
    before = _logical_database_snapshot(path)

    init_db(str(path))
    init_db(str(path))

    assert _logical_database_snapshot(path) == before
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM extension_probe_view").fetchall() == [(1, "KEEP")]
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'extract_version'"
        ).fetchone() == ("1",)


def test_current_derived_schema_repair_rolls_back_as_one_transaction(tmp_path):
    path = tmp_path / "atomic-derived-repair.db"
    init_db(str(path))
    early_index = "idx_journal_user_kind_time"
    failing_index = "uq_grill_one_active_per_user"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"DROP INDEX {early_index}")
        conn.execute(f"DROP INDEX {failing_index}")
        set_id = conn.execute(
            "INSERT INTO question_sets (user_id, kind, edition, resume_id, state, generation, "
            "material_fingerprint, policy_fingerprint, generation_fingerprint, prompt_version, "
            "schema_version, rubric_version, segmentation_version, summary_policy_version, "
            "input_receipt_json, coverage_json, context_label, created_time, updated_time) "
            "VALUES ('u1', 'generated', 'basic', 1, 'ready', 'g0', 'm0', 'p0', 'f0', "
            "'v1', 'v1', 'v1', 'v1', 'v1', '{}', '{}', '测试题集', 's0', 's0')"
        ).lastrowid
        conn.executemany(
            "INSERT INTO grill_sessions "
            "(user_id, question_set_id, kind, edition, context_label, state, started_time, updated_time) "
            "VALUES ('u1', ?, 'generated', 'basic', '测试题集', 'active', 's0', 's0')",
            [(set_id,), (set_id,)],
        )

    with pytest.raises(sqlite3.IntegrityError):
        init_db(str(path))

    with closing(sqlite3.connect(path)) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert early_index not in indexes
        assert failing_index not in indexes
        assert conn.execute("SELECT COUNT(*) FROM grill_sessions").fetchone()[0] == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            database_schema.SCHEMA_VERSION
        )


def test_current_online_backup_restore_is_a_logical_no_op(tmp_path):
    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "backup.db"
    init_db(str(source_path))
    with closing(sqlite3.connect(source_path)) as conn, conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'Backup Co', 'Engineer', 'c0', 'u0')"
        )
        conn.execute(
            "CREATE TABLE backup_extension "
            "(id INTEGER PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        conn.execute("INSERT INTO backup_extension VALUES (1, 'KEEP')")
        source_secret = conn.execute(
            "SELECT value FROM meta WHERE key = 'assistant_recovery_scope_secret'"
        ).fetchone()[0]
    with closing(sqlite3.connect(source_path)) as source, closing(
        sqlite3.connect(backup_path)
    ) as target:
        source.backup(target)

    source_snapshot = _logical_database_snapshot(source_path)
    before = _logical_database_snapshot(backup_path)
    assert before == source_snapshot

    init_db(str(backup_path))
    init_db(str(backup_path))

    assert _logical_database_snapshot(backup_path) == before
    with closing(sqlite3.connect(backup_path)) as conn:
        assert conn.execute("SELECT value FROM backup_extension").fetchone() == ("KEEP",)
        restored_secret = conn.execute(
            "SELECT value FROM meta WHERE key = 'assistant_recovery_scope_secret'"
        ).fetchone()[0]
    assert restored_secret == source_secret


def test_application_incoming_foreign_keys_match_delete_effect_contract(db_path):
    with read_connection(db_path) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        ).fetchall()]
        incoming = {
            (table, row[3], row[6])
            for table in tables
            for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            if row[2] == "applications"
        }
    assert incoming == {
        ("timeline_entries", "application_id", "NO ACTION"),
        ("questions", "application_id", "NO ACTION"),
        ("resumes", "application_id", "NO ACTION"),
        ("review_question_occurrences", "application_id", "NO ACTION"),
    }


def test_application_delete_pending_target_index_blocks_duplicate_live_cards(db_path):
    extraction = json.dumps({
        "operation_type": "application_delete",
        "target": {"application_id": 7},
    })
    derivation = json.dumps({"operation": {"type": "application_delete"}})
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) VALUES "
            "('u1', 'correction', 'first', 'j1', ?, ?, 'awaiting_user', "
            "'11111111-1111-4111-8111-111111111111')",
            (extraction, derivation),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
                "derivation_json, state, operation_id) VALUES "
                "('u1', 'correction', 'duplicate', 'j2', ?, ?, 'awaiting_user', "
                "'22222222-2222-4222-8222-222222222222')",
                (extraction, derivation),
            )
        conn.execute(
            "UPDATE journal SET state='voided' "
            "WHERE operation_id='11111111-1111-4111-8111-111111111111'",
        )
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) VALUES "
            "('u1', 'correction', 'replacement', 'j3', ?, ?, 'awaiting_user', "
            "'33333333-3333-4333-8333-333333333333')",
            (extraction, derivation),
        )


def test_review_record_indexes_scope_turn_and_source_to_operation_type_and_tenant(db_path):
    from careerdesk.features.reviews.operations.record_models import ReviewRecordProposal

    first_turn = "11111111-1111-4111-8111-111111111111"
    second_turn = "22222222-2222-4222-8222-222222222222"

    def insert_operation(
        conn,
        *,
        user_id: str,
        operation_id: str,
        client_turn_id: str,
        source_journal_id: int,
        target_journal_id: int,
        operation_type: str = "review_record",
        mode: str = "initial",
    ) -> None:
        if operation_type == "review_record":
            extraction_payload = {
                "operation_type": operation_type,
                "contract_version": 1,
                "operation_id": operation_id,
                "review_reference": (
                    operation_id
                    if mode == "initial"
                    else "77777777-7777-4777-8777-777777777777"
                ),
                "client_turn_id": client_turn_id,
                "request_digest": "a" * 64,
                "source_digest": "b" * 64,
                "combined_digest": "c" * 64,
                "attempt_token": "88888888-8888-4888-8888-888888888888",
                "mode": mode,
                "effective_date": "2026-07-14",
                "source_journal_id": source_journal_id,
                "target_journal_id": target_journal_id,
                "target_expected_state": (
                    "pending" if mode == "initial" else "awaiting_user"
                ),
                "target_expected_revision": 0 if mode == "initial" else 1,
            }
        else:
            extraction_payload = {
                "operation_type": operation_type,
                "client_turn_id": client_turn_id,
                "source_journal_id": source_journal_id,
            }
        if operation_type == "review_record":
            extraction_payload = ReviewRecordProposal.model_validate(
                extraction_payload,
            ).model_dump()
        extraction = json.dumps(
            extraction_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        derivation = json.dumps({
            "operation": {"type": operation_type},
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, parent_journal_id, operation_id) VALUES "
            "(?, 'correction', ?, ?, ?, ?, 'applied', ?, ?)",
            (
                user_id,
                f"{operation_type}:{operation_id}",
                operation_id,
                extraction,
                derivation,
                target_journal_id,
                operation_id,
            ),
        )

    with sqlite3.connect(db_path) as conn:
        u1_source_one = _insert_journal(conn, "u1")
        u1_source_two = _insert_journal(conn, "u1")
        u2_target = _insert_journal(conn, "u2")

        insert_operation(
            conn,
            user_id="u1",
            operation_id="61111111-1111-4111-8111-111111111111",
            client_turn_id=first_turn,
            source_journal_id=u1_source_one,
            target_journal_id=u1_source_one,
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_operation(
                conn,
                user_id="u1",
                operation_id="62222222-2222-4222-8222-222222222222",
                client_turn_id=first_turn,
                source_journal_id=u1_source_two,
                target_journal_id=u1_source_two,
            )
        with pytest.raises(sqlite3.IntegrityError):
            insert_operation(
                conn,
                user_id="u1",
                operation_id="63333333-3333-4333-8333-333333333333",
                client_turn_id=second_turn,
                source_journal_id=u1_source_one,
                target_journal_id=u1_source_one,
            )

        insert_operation(
            conn,
            user_id="u2",
            operation_id="64444444-4444-4444-8444-444444444444",
            client_turn_id=first_turn,
            source_journal_id=u1_source_one,
            target_journal_id=u2_target,
            mode="supplement",
        )
        insert_operation(
            conn,
            user_id="u1",
            operation_id="65555555-5555-4555-8555-555555555555",
            client_turn_id=first_turn,
            source_journal_id=u1_source_one,
            target_journal_id=u1_source_one,
            operation_type="review_timeline_entry_edit",
        )
        insert_operation(
            conn,
            user_id="u1",
            operation_id="66666666-6666-4666-8666-666666666666",
            client_turn_id=second_turn,
            source_journal_id=u1_source_two,
            target_journal_id=u1_source_two,
        )

    with read_connection(db_path) as conn:
        operation_rows = conn.execute(
            "SELECT user_id, json_extract(extraction_json, '$.operation_type'), "
            "json_extract(extraction_json, '$.client_turn_id'), "
            "json_extract(extraction_json, '$.source_journal_id') "
            "FROM journal WHERE kind='correction' AND operation_id IS NOT NULL "
            "ORDER BY id",
        ).fetchall()
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'",
            ).fetchall()
        }

    assert REVIEW_RECORD_INDEXES <= indexes
    assert operation_rows == [
        ("u1", "review_record", first_turn, u1_source_one),
        ("u2", "review_record", first_turn, u1_source_one),
        ("u1", "review_timeline_entry_edit", first_turn, u1_source_one),
        ("u1", "review_record", second_turn, u1_source_two),
    ]


def test_application_merge_pending_indexes_block_same_role_duplicates(db_path):
    def proposal(source_id: int, destination_id: int) -> str:
        return json.dumps({
            "operation_type": "application_merge",
            "source": {"application_id": source_id},
            "destination": {"application_id": destination_id},
        })

    derivation = json.dumps({"operation": {"type": "application_merge"}})
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) VALUES "
            "('u1', 'correction', 'first', 'j1', ?, ?, 'awaiting_user', "
            "'41111111-1111-4111-8111-111111111111')",
            (proposal(1, 2), derivation),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
                "derivation_json, state, operation_id) VALUES "
                "('u1', 'correction', 'same-source', 'j2', ?, ?, 'awaiting_user', "
                "'42222222-2222-4222-8222-222222222222')",
                (proposal(1, 3), derivation),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
                "derivation_json, state, operation_id) VALUES "
                "('u1', 'correction', 'same-destination', 'j3', ?, ?, 'awaiting_user', "
                "'43333333-3333-4333-8333-333333333333')",
                (proposal(4, 2), derivation),
            )
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) VALUES "
            "('u1', 'correction', 'cross-role', 'j4', ?, ?, 'awaiting_user', "
            "'44444444-4444-4444-8444-444444444444')",
            (proposal(2, 5), derivation),
        )


def test_review_undo_live_target_index_blocks_parallel_or_repeated_live_operations(tmp_path):
    path = str(tmp_path / "review-undo-live-target.db")
    init_db(path)
    extraction = json.dumps({"operation_type": "review_undo"})
    with sqlite3.connect(path) as conn:
        target = _insert_journal(conn)
        first = "11111111-1111-4111-8111-111111111111"
        second = "22222222-2222-4222-8222-222222222222"
        third = "33333333-3333-4333-8333-333333333333"
        cursor = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "state, parent_journal_id, operation_id) VALUES "
            "('u1', 'correction', 'first', 'j1', ?, 'awaiting_user', ?, ?)",
            (extraction, target, first),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
                "state, parent_journal_id, operation_id) VALUES "
                "('u1', 'correction', 'parallel', 'j2', ?, 'awaiting_user', ?, ?)",
                (extraction, target, second),
            )

        conn.execute("UPDATE journal SET state='voided' WHERE id=?", (cursor.lastrowid,))
        completed = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "state, parent_journal_id, operation_id) VALUES "
            "('u1', 'correction', 'second', 'j3', ?, 'applied', ?, ?)",
            (extraction, target, second),
        )
        assert completed.lastrowid is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
                "state, parent_journal_id, operation_id) VALUES "
                "('u1', 'correction', 'after-complete', 'j4', ?, 'awaiting_user', ?, ?)",
                (extraction, target, third),
            )


def test_connection_pragmas(db_path):
    with read_connection(db_path) as conn:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        (busy_timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
    assert mode == "wal"
    assert busy_timeout == 10_000
    assert fk == 1


def test_connection_lifecycle_order_is_frozen(monkeypatch):
    events: list[str] = []

    class Result:
        def fetchone(self):
            return (database_schema.SCHEMA_VERSION,)

    class Connection:
        def execute(self, statement):
            events.append(f"sql:{statement}")
            return Result()

        def create_function(self, name, narg, func, *, deterministic=False):
            events.append(f"create_function:{name}:{narg}:{deterministic}")

        def close(self):
            events.append("close")

    connection = Connection()
    monkeypatch.setattr(
        db,
        "prepare_private_file",
        lambda path: events.append(f"prepare:{path}") or path,
    )
    monkeypatch.setattr(
        db,
        "harden_private_file_if_exists",
        lambda path: events.append(f"harden:{path}"),
    )
    monkeypatch.setattr(
        db,
        "_preflight_database_read_only",
        lambda path, *, require_complete_current_schema: events.append(
            f"preflight:{path}:{require_complete_current_schema}"
        ),
    )
    monkeypatch.setattr(
        db,
        "_open_database_read_write",
        lambda path: events.append(f"open-rw:{path}") or connection,
    )
    monkeypatch.setattr(
        database_schema,
        "assert_supported_schema_version",
        lambda version: events.append(f"supported:{version}"),
    )
    monkeypatch.setattr(
        database_schema,
        "assert_database_shape_before_init",
        lambda _connection, version: events.append(f"shape:{version}"),
    )

    assert db._connect("frozen.db", require_complete_current_schema=True) is connection
    assert events == [
        "prepare:frozen.db",
        "harden:frozen.db-wal",
        "harden:frozen.db-shm",
        "preflight:frozen.db:True",
        "open-rw:frozen.db",
        "sql:PRAGMA user_version",
        f"supported:{database_schema.SCHEMA_VERSION}",
        f"shape:{database_schema.SCHEMA_VERSION}",
        "sql:PRAGMA journal_mode=WAL",
        "harden:frozen.db-wal",
        "harden:frozen.db-shm",
        "sql:PRAGMA busy_timeout=10000",
        "sql:PRAGMA foreign_keys=ON",
        "create_function:squash_whitespace:1:True",
    ]


def test_connect_closes_rw_connection_when_post_open_validation_fails(monkeypatch):
    class Result:
        def fetchone(self):
            return (database_schema.SCHEMA_VERSION,)

    class Connection:
        closed = False

        def execute(self, _statement):
            return Result()

        def close(self):
            self.closed = True

    connection = Connection()

    def fail_validation(_version):
        raise RuntimeError("post-open failure")

    monkeypatch.setattr(db, "prepare_private_file", lambda path: path)
    monkeypatch.setattr(db, "harden_private_file_if_exists", lambda _path: None)
    monkeypatch.setattr(db, "_preflight_database_read_only", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "_open_database_read_write", lambda _path: connection)
    monkeypatch.setattr(
        database_schema,
        "assert_supported_schema_version",
        fail_validation,
    )

    with pytest.raises(RuntimeError, match="post-open failure"):
        db._connect("failure.db")

    assert connection.closed is True


def test_check_constraint_rejects_bad_enum(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, stage, created_time, updated_time) "
            "VALUES ('u1', '阿里', '后端', 'withdrawn', ?, ?)",
            (now_iso(), now_iso()),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as conn:
            conn.execute(
                "INSERT INTO applications (user_id, company, position, stage, created_time, updated_time) "
                "VALUES ('u1', '字节', 'LLM应用', '面着呢', ?, ?)",
                (now_iso(), now_iso()),
            )


def test_grill_database_constraints_enforce_session_invariants(db_path):
    with transaction(db_path) as conn:
        question_id = conn.execute(
            "INSERT INTO questions (user_id, text, source, created_time, updated_time) "
            "VALUES ('u1', '幂等怎么做？', 'real', ?, ?)", (now_iso(), now_iso())).lastrowid
        set_id = conn.execute(
            "INSERT INTO question_sets (user_id, kind, state, generation, material_fingerprint, "
            "policy_fingerprint, generation_fingerprint, prompt_version, schema_version, "
            "rubric_version, segmentation_version, summary_policy_version, input_receipt_json, "
            "coverage_json, context_label, created_time, updated_time) VALUES "
            "('u1', 'library_snapshot', 'ready', 'g1', 'm1', 'p1', 'f1', 'v1', 'v1', "
            "'v1', 'v1', 'v1', '{}', '{}', '题库快照', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
        item_id = conn.execute(
            "INSERT INTO question_set_items (user_id, question_set_id, ordinal, canonical_question_id, "
            "canonical_revision, canonical_digest, text, category, channel, response_format, "
            "evaluation_kind, difficulty, primary_competency, secondary_tags_json, rubric_json, "
            "answer_authority, answer_guide_json, evidence_json, follow_up_allowed, repeat_scope, "
            "created_time) VALUES ('u1', ?, 0, ?, 1, ?, '幂等怎么做？', 'professional_domain', "
            "'interview', 'oral_text', 'rubric', 'intermediate', '幂等设计', '[]', '{}', "
            "'user_verified', '{}', '[]', 1, 'global', ?)",
            (set_id, question_id, "0" * 64, now_iso()),
        ).lastrowid
        active_id = conn.execute(
            "INSERT INTO grill_sessions (user_id, question_set_id, kind, context_label, state, "
            "started_time, updated_time) VALUES ('u1', ?, 'library_snapshot', '题库快照', "
            "'active', ?, ?)", (set_id, now_iso(), now_iso())).lastrowid
        session_item_id = conn.execute(
            "INSERT INTO grill_session_items (user_id, session_id, question_set_item_id, ordinal) "
            "VALUES ('u1', ?, ?, 0)", (active_id, item_id)).lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as conn:
            conn.execute(
                "INSERT INTO grill_sessions (user_id, question_set_id, kind, context_label, state, "
                "started_time, updated_time) VALUES ('u1', ?, 'library_snapshot', '题库快照', "
                "'active', ?, ?)", (set_id, now_iso(), now_iso()))

    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO grill_answers (user_id, session_id, session_item_id, question_id, verdict, "
            "created_time) VALUES ('u1', ?, ?, ?, 'meets', ?)",
            (active_id, session_item_id, question_id, now_iso()))
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as conn:
            conn.execute(
                "INSERT INTO grill_answers (user_id, session_id, session_item_id, question_id, verdict, "
                "created_time) VALUES ('u1', ?, ?, ?, 'needs_work', ?)",
                (active_id, session_item_id, question_id, now_iso()))


def test_foreign_key_enforced(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as conn:
            conn.execute(
                "INSERT INTO timeline_entries (user_id, application_id, summary, "
                "from_stage, to_stage, source, created_time) "
                "VALUES ('u1', 99999, '面试', 'interviewing', 'interviewing', "
                "'manual', ?)",
                (now_iso(),),
            )


def test_transaction_rolls_back_atomically(db_path):
    with pytest.raises(RuntimeError):
        with transaction(db_path) as conn:
            _insert_journal(conn)
            raise RuntimeError("派生中途失败")
    with read_connection(db_path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM journal").fetchone()
    assert count == 0


def test_transaction_keeps_deferred_commit_and_connection_close_contract(
    db_path, monkeypatch,
):
    opened: list[sqlite3.Connection] = []
    original_connect = db._connect

    def tracked_connect(path, **kwargs):
        conn = original_connect(path, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "_connect", tracked_connect)

    with transaction(db_path) as conn:
        assert conn.in_transaction is False
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[-1].execute("SELECT 1")

    with transaction(db_path) as conn:
        _insert_journal(conn)
        assert conn.in_transaction is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[-1].execute("SELECT 1")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal").fetchone() == (1,)


def test_unique_application_conflict(db_path):
    def _insert(conn):
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', '字节', 'LLM应用', ?, ?)",
            (now_iso(), now_iso()),
        )

    with transaction(db_path) as conn:
        _insert(conn)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as conn:
            _insert(conn)


def test_supported_schema_version_rejections_point_to_upgrade_path():
    # Physical user_version must remain above every retired v1-v25 local build;
    # otherwise an old binary can mistake the fresh contract for an old schema
    # and run its retired migration chain over it.
    assert database_schema.SCHEMA_VERSION > 25
    database_schema.assert_supported_schema_version(0)
    database_schema.assert_supported_schema_version(database_schema.SCHEMA_VERSION)
    with pytest.raises(RuntimeError, match="版本 v-1 无效"):
        database_schema.assert_supported_schema_version(-1)
    with pytest.raises(RuntimeError, match="更新版本的 CareerDesk"):
        database_schema.assert_supported_schema_version(
            database_schema.SCHEMA_VERSION + 1
        )
    with pytest.raises(RuntimeError, match="fresh-only v28"):
        database_schema.assert_supported_schema_version(25)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_derived_db_path_creates_hardened_wal_sibling(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    init_db(db_path)

    from careerdesk.platform.database import derived_db_path

    first = derived_db_path(db_path)
    second = derived_db_path(db_path)
    assert first == second == str(tmp_path / "derived.db")
    info = Path(first).stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    with closing(sqlite3.connect(first)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_interactive_transaction_raises_busy_instead_of_waiting(tmp_path):
    from careerdesk.platform.database import DatabaseBusy

    db_path = str(tmp_path / "careerdesk.db")
    init_db(db_path)

    blocker = sqlite3.connect(db_path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(DatabaseBusy):
            with transaction(db_path, busy_timeout_ms=200) as conn:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('busy-probe', '1')"
                )
        assert time.monotonic() - started < 5
    finally:
        blocker.rollback()
        blocker.close()
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='busy-probe'").fetchone()
    assert row is None


def test_wal_truncation_only_fires_over_threshold(tmp_path):
    from careerdesk.platform.database import truncate_wal_if_oversized

    db_path = str(tmp_path / "careerdesk.db")
    init_db(db_path)
    wal_path = Path(f"{db_path}-wal")

    holder = sqlite3.connect(db_path)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        with holder:
            holder.execute(
                "INSERT INTO meta(key, value) VALUES ('wal-probe', ?)",
                ("x" * 200_000,),
            )
        assert wal_path.stat().st_size > 0

        assert truncate_wal_if_oversized(db_path, threshold_bytes=1024 ** 3) is False
        assert truncate_wal_if_oversized(db_path, threshold_bytes=1) is True
        assert wal_path.stat().st_size == 0
    finally:
        holder.close()
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='wal-probe'"
        ).fetchone()[0] == "x" * 200_000
