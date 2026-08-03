"""Fresh-v1 schema manifest gates, repair policy, and extension boundaries."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from careerdesk.platform.database import init_db
from careerdesk.platform.database import connection as db
from careerdesk.platform.database import schema as database_schema


REPAIRABLE_INDEX = "uq_journal_operation"
REPAIRABLE_TRIGGER = "trg_application_intake_owner_insert_contract"


def _replace_once(sql: str, old: str, new: str) -> str:
    assert sql.count(old) == 1, (old, sql)
    return sql.replace(old, new, 1)


def _stored_sql(conn: sqlite3.Connection, object_type: str, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, name),
    ).fetchone()
    assert row is not None and row[0]
    return row[0]


def _catalog(path: Path) -> tuple[int, tuple[tuple[str, str, str, str], ...]]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        objects = tuple(conn.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') "
            "FROM sqlite_master ORDER BY type, name",
        ).fetchall())
    return version, objects


def _new_current(path: Path) -> None:
    init_db(str(path))
    assert database_schema.FRESH_SCHEMA_REVISION == 1
    assert _catalog(path)[0] == database_schema.SCHEMA_VERSION == 28


def _replace_schema_object(
    path: Path,
    object_type: str,
    name: str,
    replacement_sql: str,
) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f'DROP {object_type.upper()} "{name}"')
        conn.execute(replacement_sql)


def _replace_table_sql(path: Path, table_name: str, old: str, new: str) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        replacement = _replace_once(
            _stored_sql(conn, "table", table_name), old, new,
        )
        conn.execute(f'DROP TABLE "{table_name}"')
        conn.execute(replacement)


def _forbid_read_write_open(monkeypatch) -> None:
    def fail_if_called(_private_path):
        raise AssertionError("database reached the read-write opener")

    monkeypatch.setattr(db, "_open_database_read_write", fail_if_called)


def _assert_rejected_read_only(path: Path, monkeypatch, pattern: str) -> None:
    before_bytes = path.read_bytes()
    before_catalog = _catalog(path)
    _forbid_read_write_open(monkeypatch)

    with pytest.raises(RuntimeError, match=pattern):
        init_db(str(path))

    assert path.read_bytes() == before_bytes
    assert _catalog(path) == before_catalog


def test_fresh_v1_manifest_is_idempotent(tmp_path):
    path = tmp_path / "fresh-current.db"

    _new_current(path)
    first_catalog = _catalog(path)
    init_db(str(path))

    assert _catalog(path) == first_catalog


def test_sql_manifest_ignores_comments_and_formatting_but_not_literals():
    annotated = "CREATE TABLE sample (value TEXT -- column note\n);"
    compact = "CREATE   TABLE sample (value TEXT\n);"
    changed_literal = "CREATE TABLE sample (value TEXT DEFAULT '-- column note');"

    assert database_schema._sql_digest(annotated) == database_schema._sql_digest(compact)
    assert database_schema._sql_digest(annotated) != database_schema._sql_digest(
        changed_literal,
    )
    assert database_schema._sql_digest("SELECT 'a  b'") != database_schema._sql_digest(
        "SELECT 'a b'",
    )


def test_fresh_manifest_digest_mismatch_rolls_back_every_object(tmp_path, monkeypatch):
    path = tmp_path / "fresh-digest-mismatch.db"
    original = database_schema._FRESH_CURRENT_TABLE_PROFILE_DIGEST
    database_schema._fresh_current_reference_manifest.cache_clear()
    monkeypatch.setattr(
        database_schema,
        "_FRESH_CURRENT_TABLE_PROFILE_DIGEST",
        "0" * 64,
    )
    try:
        with pytest.raises(RuntimeError, match="内置 fresh schema r1（物理 v28）"):
            init_db(str(path))
    finally:
        monkeypatch.setattr(
            database_schema,
            "_FRESH_CURRENT_TABLE_PROFILE_DIGEST",
            original,
        )
        database_schema._fresh_current_reference_manifest.cache_clear()

    assert _catalog(path) == (0, ())


def test_current_database_repairs_missing_derived_objects_atomically(tmp_path):
    path = tmp_path / "repair-derived.db"
    _new_current(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f'DROP INDEX "{REPAIRABLE_INDEX}"')
        conn.execute(f'DROP TRIGGER "{REPAIRABLE_TRIGGER}"')

    init_db(str(path))
    init_db(str(path))

    with closing(sqlite3.connect(path)) as conn:
        assert _stored_sql(conn, "index", REPAIRABLE_INDEX)
        assert _stored_sql(conn, "trigger", REPAIRABLE_TRIGGER)
        database_schema.assert_current_schema_manifest(
            conn,
            allow_missing_derived=False,
        )


def test_failed_derived_repair_rolls_back_the_whole_repair(tmp_path, monkeypatch):
    path = tmp_path / "repair-rollback.db"
    _new_current(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f'DROP INDEX "{REPAIRABLE_INDEX}"')
        conn.execute(f'DROP TRIGGER "{REPAIRABLE_TRIGGER}"')
    before = _catalog(path)
    monkeypatch.setattr(
        database_schema,
        "TRIGGERS",
        f"{database_schema.TRIGGERS}\nTHIS IS NOT SQL;",
    )

    with pytest.raises(sqlite3.OperationalError):
        init_db(str(path))

    assert _catalog(path) == before


@pytest.mark.parametrize(
    ("object_type", "name", "replacement_sql"),
    [
        pytest.param(
            "index",
            REPAIRABLE_INDEX,
            "CREATE INDEX uq_journal_operation ON journal(operation_id)",
            id="same-name-non-unique-index",
        ),
        pytest.param(
            "index",
            "uq_journal_application_delete_pending_target",
            "CREATE UNIQUE INDEX uq_journal_application_delete_pending_target "
            "ON journal(user_id, json_extract(extraction_json, '$.target.wrong_id')) "
            "WHERE kind='correction' AND state='awaiting_user'",
            id="wrong-expression-index",
        ),
        pytest.param(
            "index",
            "uq_grill_one_active_per_user",
            "CREATE UNIQUE INDEX uq_grill_one_active_per_user ON grill_sessions(user_id) "
            "WHERE state='suspended'",
            id="wrong-partial-predicate",
        ),
        pytest.param(
            "trigger",
            REPAIRABLE_TRIGGER,
            "CREATE TRIGGER trg_application_intake_owner_insert_contract "
            "BEFORE INSERT ON application_intake_operation_owners BEGIN SELECT 1; END",
            id="same-name-noop-trigger",
        ),
    ],
)
def test_current_manifest_rejects_same_name_derived_drift_before_rw_open(
    tmp_path,
    monkeypatch,
    object_type,
    name,
    replacement_sql,
):
    path = tmp_path / f"derived-drift-{name}.db"
    _new_current(path)
    _replace_schema_object(path, object_type, name, replacement_sql)

    _assert_rejected_read_only(path, monkeypatch, r"schema manifest|定义不匹配")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("notes         TEXT,", "notes         INTEGER,", id="declared-type"),
        pytest.param("name          TEXT NOT NULL,", "name          TEXT,", id="not-null"),
        pytest.param("id            INTEGER PRIMARY KEY,", "id            INTEGER,", id="pk"),
        pytest.param(
            "UNIQUE (user_id, name_key)",
            "UNIQUE (user_id, name_key, created_time)",
            id="unique",
        ),
        pytest.param(") STRICT", ")", id="strict"),
    ],
)
def test_current_manifest_rejects_core_table_profile_drift_before_rw_open(
    tmp_path,
    monkeypatch,
    old,
    new,
):
    path = tmp_path / "table-profile-drift.db"
    _new_current(path)
    _replace_table_sql(path, "companies", old, new)

    _assert_rejected_read_only(path, monkeypatch, r"schema manifest|定义不匹配")


def test_future_version_is_rejected_read_only(tmp_path, monkeypatch):
    path = tmp_path / "future.db"
    _new_current(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"PRAGMA user_version={database_schema.SCHEMA_VERSION + 1}")

    _assert_rejected_read_only(path, monkeypatch, "高于当前程序支持")


def test_unversioned_legacy_shape_is_rejected_without_migration(tmp_path, monkeypatch):
    path = tmp_path / "legacy-unversioned.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE legacy_applications(id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("PRAGMA user_version=0")

    _assert_rejected_read_only(path, monkeypatch, "未记录 schema 版本")


def test_current_schema_with_erased_version_is_not_treated_as_fresh(tmp_path, monkeypatch):
    path = tmp_path / "erased-version.db"
    _new_current(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA user_version=0")

    _assert_rejected_read_only(path, monkeypatch, "未记录 schema 版本")


@pytest.mark.parametrize(
    "extension_sql",
    [
        pytest.param(
            "CREATE UNIQUE INDEX injected_unique_user ON applications(user_id)",
            id="extra-index-on-core-table",
        ),
        pytest.param(
            """CREATE TRIGGER injected_application_block
BEFORE INSERT ON applications
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'blocked by injected trigger');
END""",
            id="extra-trigger-on-core-table",
        ),
        pytest.param(
            """CREATE TRIGGER injected_case_variant_block
BEFORE INSERT ON AppLiCaTiOnS
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'blocked by case-variant trigger');
END""",
            id="case-variant-core-trigger",
        ),
    ],
)
def test_manifest_rejects_unregistered_objects_attached_to_core_tables(
    tmp_path,
    monkeypatch,
    extension_sql,
):
    path = tmp_path / "core-injection.db"
    _new_current(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(extension_sql)

    _assert_rejected_read_only(path, monkeypatch, r"schema manifest|未登记")


def test_manifest_rejects_extension_foreign_key_into_core_table(tmp_path, monkeypatch):
    path = tmp_path / "inbound-extension-fk.db"
    _new_current(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE extension_application_notes ("
            "id INTEGER PRIMARY KEY, "
            "application_id INTEGER REFERENCES applications(id)"
            ") STRICT",
        )

    _assert_rejected_read_only(path, monkeypatch, r"schema manifest|扩展表")


def test_manifest_preserves_isolated_third_party_objects_and_data(tmp_path):
    path = tmp_path / "isolated-extensions.db"
    _new_current(path)
    extension_sql = """
CREATE TABLE extension_records (
    id INTEGER PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
CREATE INDEX extension_records_value ON extension_records(value);
CREATE TRIGGER extension_records_no_delete
BEFORE DELETE ON extension_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'extension record is immutable');
END;
CREATE VIEW extension_records_view AS
SELECT id, value FROM extension_records;
INSERT INTO extension_records (id, value) VALUES (1, 'keep');
"""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(extension_sql)
        conn.commit()
    before = _catalog(path)
    extension_before = tuple(
        row for row in before[1] if row[1].startswith("extension_records")
    )

    init_db(str(path))
    init_db(str(path))

    after = _catalog(path)
    extension_after = tuple(
        row for row in after[1] if row[1].startswith("extension_records")
    )
    assert extension_after == extension_before
    assert {row[0] for row in extension_after} == {"table", "index", "trigger", "view"}
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT id, value FROM extension_records").fetchall() == [
            (1, "keep"),
        ]
