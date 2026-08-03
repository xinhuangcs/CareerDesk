"""CareerDesk SQLite connection, initialization, and transaction infrastructure.

Design principles:
- Journal first: spoken source enters the audit journal before derived records are written in
  one transaction. Any failure rolls back the full derivation. Only fresh or current schemas
  are accepted.
- Fixed tables support direct UI reads and CHECK constraints stabilize their formats. Semantic
  retrieval belongs to the retrieval stack, not this module.
- Every table carries user_id so tenant isolation exists even during single-user deployment.

The sibling ``schema`` module owns physical declarations and read-only manifest gates. This
module owns connections, initialization, transaction contexts, and small shared utilities.
"""

import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import schema as database_schema
from .identity import (
    application_identity_key as application_identity_key,
    normalize_application_identity_part,
)
from ..storage.private import (
    harden_private_file_if_exists,
    prepare_private_file,
)

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """Return the current UTC time as the canonical ISO 8601 database timestamp."""
    return datetime.now(timezone.utc).isoformat()


def loads_json(text: str | None, default):
    """Deserialize JSON safely and return ``default`` for missing or invalid text.

    Args:
        text: JSON text to parse; ``None`` returns ``default`` immediately.
        default: Fallback such as ``[]`` or ``None``.
    """
    if text is None:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("库中存在无法解析的 JSON 字段，已回落默认值（建议排查该行）")
        return default


def squash_whitespace(value: str | None) -> str | None:
    """Remove whitespace for natural-key comparisons while preserving ``None``.

    Models and users may vary whitespace in role names. This normalization is comparison-only;
    stored source text remains unchanged.
    """
    return normalize_application_identity_part(value)


# Increment these whenever the corresponding derivation or extraction logic changes. Startup
# compares them with database metadata. Cached derivations can refresh automatically; extraction
# upgrades require a paid model call and therefore explicit user confirmation.
DERIVE_VERSION = 1
EXTRACT_VERSION = 1


def _open_database_read_only(private_path) -> sqlite3.Connection:
    uri = f"{private_path.as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _open_database_read_write(private_path) -> sqlite3.Connection:
    return sqlite3.connect(private_path, timeout=30)


def _preflight_database_read_only(
    private_path,
    *,
    require_complete_current_schema: bool = False,
) -> None:
    """Validate the visible main DB plus uncheckpointed WAL before any read-write open."""
    with closing(_open_database_read_only(private_path)) as conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        database_schema.assert_supported_schema_version(current_version)
        if require_complete_current_schema or current_version == 0:
            database_schema.assert_database_shape_before_init(conn, current_version)


DEFAULT_BUSY_TIMEOUT_MS = 10_000
INTERACTIVE_BUSY_TIMEOUT_MS = 2_000
DERIVED_DATABASE_FILENAME = "derived.db"
WAL_TRUNCATE_THRESHOLD_BYTES = 64 * 1024 * 1024


class DatabaseBusy(RuntimeError):
    """A write-lock wait timed out without changing data; callers may retry safely."""


def _connect(
    db_path: str,
    *,
    require_complete_current_schema: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a connection with WAL, bounded lock waiting, and foreign keys enabled.

    Multiple write paths can overlap. WAL plus busy_timeout reduces avoidable lock errors, while
    per-connection foreign-key enforcement prevents invalid cross-table references.
    """
    private_path = prepare_private_file(db_path)
    # Reject hostile/pre-existing final links before SQLite has any chance to
    # attach or recover through them.  A second pass below hardens files SQLite
    # creates between this preflight and WAL activation.
    harden_private_file_if_exists(f"{private_path}-wal")
    harden_private_file_if_exists(f"{private_path}-shm")
    # A normal read-write open may recover and checkpoint a crash WAL before the first explicit
    # SQL statement. Probe the visible main DB plus WAL through an OS-enforced read-only handle
    # first, then revalidate after opening read-write. The data-root instance lock excludes an
    # external writer between those steps; the SHM file is only a rebuildable WAL index.
    _preflight_database_read_only(
        private_path,
        require_complete_current_schema=require_complete_current_schema,
    )
    conn = _open_database_read_write(private_path)
    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        database_schema.assert_supported_schema_version(current_version)
        if require_complete_current_schema or current_version == 0:
            database_schema.assert_database_shape_before_init(conn, current_version)
        conn.execute("PRAGMA journal_mode=WAL")
        # SQLite derives newly-created WAL/SHM modes from the 0600 main DB.
        # Pre-existing sidecars need a non-creating hardening pass after attach.
        harden_private_file_if_exists(f"{private_path}-wal")
        harden_private_file_if_exists(f"{private_path}-shm")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms):d}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.create_function("squash_whitespace", 1, squash_whitespace, deterministic=True)
        return conn
    except Exception:
        conn.close()
        raise


def _repair_current_derived_schema(conn: sqlite3.Connection) -> None:
    """Restore missing derived indexes/triggers atomically and verify the current manifest."""
    try:
        # _connect revalidates after RW open.  BEGIN lives in the same
        # executescript as the DDL because sqlite3.executescript would otherwise
        # commit a separately-started transaction before running the script.
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{database_schema.INDEXES}\n{database_schema.TRIGGERS}"
        )
        database_schema.assert_current_schema_manifest(
            conn,
            allow_missing_derived=False,
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def init_db(db_path: str) -> None:
    """Create a fresh current database or verify an existing current manifest.

    Args:
        db_path: SQLite file path, for example ``data/careerdesk.db``.
    """
    with closing(_connect(db_path, require_complete_current_schema=True)) as conn:
        (current_version,) = conn.execute("PRAGMA user_version").fetchone()
        # Build a fresh database directly from the final declaration; existing databases must
        # already match the current manifest.
        (existing_objects,) = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master"
        ).fetchone()
        is_fresh = existing_objects == 0
        if is_fresh:
            if current_version != 0:
                raise RuntimeError(
                    f"数据库记录为 v{current_version}，但没有任何表；"
                    "为避免把损坏数据库当作新库覆盖，启动已中止，请从已验证备份恢复"
                )
            # Commit schema, seed metadata, indexes, and user_version atomically so an interrupted
            # first startup cannot leave a partial final schema resembling an older database.
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{database_schema.SCHEMA}\n"
                f"INSERT OR IGNORE INTO meta(key, value) VALUES "
                f"('derive_version', '{DERIVE_VERSION:d}'), "
                f"('extract_version', '{EXTRACT_VERSION:d}'), "
                f"('assistant_recovery_scope_secret', lower(hex(randomblob(32))));\n"
                f"{database_schema.INDEXES}\n{database_schema.TRIGGERS}\n"
                f"PRAGMA user_version = {database_schema.SCHEMA_VERSION:d};"
            )
            database_schema.assert_current_schema_manifest(
                conn,
                allow_missing_derived=False,
            )
            conn.commit()
            return
        if current_version == database_schema.SCHEMA_VERSION:
            # _connect validated the table profile and existing derived objects before WAL.
            # Missing indexes/triggers can be rebuilt idempotently in one all-or-nothing commit.
            _repair_current_derived_schema(conn)
            return
        raise RuntimeError("数据库版本/形状预检与初始化分支不一致，启动已中止")


@contextmanager
def transaction(db_path: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
    """Provide an atomic cross-table write context and close the connection afterward.

    Multi-table derivations must use this context so partial derived state never survives a
    failure; journaled source remains available for a complete retry.

    Args:
        db_path: Primary database path.
        busy_timeout_ms: Maximum write-lock wait. Interactive writes use
            ``INTERACTIVE_BUSY_TIMEOUT_MS`` and raise ``DatabaseBusy`` on timeout.

    Example:
        with transaction(db_path) as conn:
            conn.execute("INSERT INTO timeline_entries ...")
            conn.execute("UPDATE applications ...")
    """
    with closing(_connect(db_path, busy_timeout_ms=busy_timeout_ms)) as conn:
        try:
            with conn:   # sqlite3 connection context commits on success and rolls back on error.
                yield conn
        except sqlite3.OperationalError as error:
            if "database is locked" in str(error).lower():
                raise DatabaseBusy("数据库写入排队超时；本次操作未执行") from error
            raise


@contextmanager
def read_connection(db_path: str):
    """Provide a read connection context and close it without opening an explicit transaction."""
    with closing(_connect(db_path)) as conn:
        yield conn


def derived_db_path(db_path: str) -> str:
    """Return the hardened path of the rebuildable semantic/FTS database.

    Derived data is excluded from backups and can be rebuilt. A separate WAL database prevents
    bulk indexing from competing for the primary database's write lock.

    Args:
        db_path: Source-of-truth database path.

    Returns:
        The sibling ``derived.db`` path.
    """
    candidate = Path(db_path).resolve().parent / DERIVED_DATABASE_FILENAME
    private_path = prepare_private_file(candidate)
    with closing(sqlite3.connect(private_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    harden_private_file_if_exists(f"{private_path}-wal")
    harden_private_file_if_exists(f"{private_path}-shm")
    return str(private_path)


def truncate_wal_if_oversized(
    db_path: str,
    *,
    threshold_bytes: int = WAL_TRUNCATE_THRESHOLD_BYTES,
) -> bool:
    """TRUNCATE-checkpoint an oversized WAL, deferring safely if the database is busy.

    The checkpoint uses SQLite's normal persistence path and changes no business data. Call it
    only during idle periods such as startup.

    Args:
        db_path: Primary or derived database path.
        threshold_bytes: WAL size that triggers truncation.

    Returns:
        Whether truncation completed.
    """
    wal = Path(f"{db_path}-wal")
    try:
        if wal.stat().st_size < threshold_bytes:
            return False
    except FileNotFoundError:
        return False
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError:
        return False
    completed = row is not None and row[0] == 0
    if completed:
        logger.info("wal truncated after exceeding threshold: %s", db_path)
    return completed


def get_meta(db_path: str, key: str, default: str | None = None) -> str | None:
    """Read one global metadata value, returning ``default`` when absent."""
    with read_connection(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db_path: str, key: str, value: str | int) -> None:
    """Insert or replace one global metadata value."""
    with transaction(db_path) as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
