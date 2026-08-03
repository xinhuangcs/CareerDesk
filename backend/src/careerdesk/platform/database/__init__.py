"""Stable platform facade for SQLite schema and connection lifecycle."""

from .connection import (
    DatabaseBusy,
    INTERACTIVE_BUSY_TIMEOUT_MS,
    derived_db_path,
    get_meta,
    init_db,
    loads_json,
    now_iso,
    application_identity_key,
    normalize_application_identity_part,
    read_connection,
    squash_whitespace,
    transaction,
    truncate_wal_if_oversized,
)

__all__ = [
    "DatabaseBusy",
    "INTERACTIVE_BUSY_TIMEOUT_MS",
    "derived_db_path",
    "get_meta",
    "init_db",
    "loads_json",
    "now_iso",
    "application_identity_key",
    "normalize_application_identity_part",
    "read_connection",
    "squash_whitespace",
    "transaction",
    "truncate_wal_if_oversized",
]
