"""Safe metadata maintenance without replaying historical business projections."""

import asyncio
from weakref import WeakKeyDictionary

from ...platform.database import get_meta, transaction
from ...platform.database.connection import DERIVE_VERSION
from ...features.journal import public as journal
from ...features.reviews import public as reviews

_LOCKS: WeakKeyDictionary = WeakKeyDictionary()


def _locks_for_current_loop() -> dict[tuple[str, str], asyncio.Lock]:
    loop = asyncio.get_running_loop()
    locks = _LOCKS.get(loop)
    if locks is None:
        locks = {}
        _LOCKS[loop] = locks
    return locks


def _version_for_user(db_path: str, kind: str, user_id: str) -> int:
    global_version = get_meta(db_path, f"{kind}_version", "0") or "0"
    stored = get_meta(db_path, f"{kind}_version:user:{user_id}", global_version) or "0"
    try:
        return int(stored)
    except ValueError:
        return 0


def _version_key(kind: str, user_id: str | None) -> str:
    return f"{kind}_version" if user_id is None else f"{kind}_version:user:{user_id}"


def _mark_in_transaction(conn, kind: str, version: int, user_id: str | None) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_version_key(kind, user_id), str(version)),
    )


def upgrade_status(db_path: str, user_id: str) -> dict:
    """Report only safely repairable metadata, excluding unfinished/voided/failed reviews."""
    version_behind = _version_for_user(db_path, "derive", user_id) < DERIVE_VERSION
    entries = journal.applied_reviews(db_path, user_id) if version_behind else []
    derive_pending = version_behind and bool(entries)
    count = len(entries) if derive_pending else 0
    return {
        "derive_pending": derive_pending,
        "pending_count": count,
    }


class MaintenanceService:
    """User single-flight, full journal snapshot, and one-transaction reconciliation."""

    def __init__(self, db_path: str):
        self._db_path = db_path

    async def reconcile(self, user_id: str) -> dict:
        key = (self._db_path, user_id)
        lock = _locks_for_current_loop().setdefault(key, asyncio.Lock())
        async with lock:
            status = upgrade_status(self._db_path, user_id)
            if not status["derive_pending"]:
                return {"status": "completed", "reconciled": 0}
            source_snapshot = journal.snapshot(self._db_path, user_id)
            entries = journal.applied_reviews(self._db_path, user_id)
            with transaction(self._db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if journal.snapshot_in_transaction(conn, user_id) != source_snapshot:
                    return {
                        "status": "error",
                        "message": "整理期间求职记录发生了变化，未修改任何数据；请重试",
                        "reconciled": 0,
                    }
                current = journal.applied_reviews_in_transaction(conn, user_id)
                if [(item["id"], item["revision"]) for item in current] != [
                    (item["id"], item["revision"]) for item in entries
                ]:
                    return {
                        "status": "error",
                        "message": "整理源已变化，未修改任何数据；请重试",
                        "reconciled": 0,
                    }
                reviews.reconcile_metadata_in_transaction(conn, user_id)
                _mark_in_transaction(conn, "derive", DERIVE_VERSION, user_id)
            return {"status": "ok", "reconciled": len(entries)}
