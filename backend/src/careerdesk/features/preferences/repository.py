"""SQLite repository for long-term preference projections."""

from __future__ import annotations

from sqlite3 import Connection

from ...platform.database import read_connection
from .models import (
    MAX_ACTIVE_PREFERENCES,
    MAX_JSON_SAFE_INTEGER,
    MAX_PREFERENCE_TOTAL_CHARS,
    PreferenceItem,
    PreferenceListDTO,
    PreferenceSettingsSnapshot,
)


class PreferenceProjectionConflict(RuntimeError):
    """Preference projection is corrupt, oversized, or missed CAS."""


def _snapshot(conn: Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "WITH candidate_ids(id) AS ("
        "SELECT id FROM preferences WHERE user_id = ? UNION "
        "SELECT p.id FROM preference_owners AS owner "
        "JOIN preferences AS p ON p.id = owner.preference_id "
        "WHERE owner.user_id = ?) "
        "SELECT p.id, p.user_id, p.key, p.value, p.revision, p.created_time, "
        "p.updated_time, o.preference_id, o.user_id, o.created_time "
        "FROM preferences AS p JOIN candidate_ids AS candidate ON candidate.id = p.id "
        "LEFT JOIN preference_owners AS o ON o.preference_id = p.id "
        "ORDER BY p.key, p.id",
        (user_id, user_id),
    ).fetchall()
    if len(rows) > MAX_ACTIVE_PREFERENCES:
        raise PreferenceProjectionConflict("当前偏好数量超过安全上限")
    items: list[dict] = []
    for row in rows:
        raw = {
            "id": row[0],
            "key": row[2],
            "value": row[3],
            "revision": row[4],
            "created_time": row[5],
            "updated_time": row[6],
        }
        if (
            row[1] != user_id
            or row[7] != raw["id"]
            or row[8] != user_id
            or row[9] != raw["created_time"]
        ):
            raise PreferenceProjectionConflict("当前偏好 owner identity 已损坏")
        item = None
        try:
            item = PreferenceItem.model_validate({
                key: value for key, value in raw.items() if key != "id"
            })
        except (TypeError, ValueError):
            pass
        if item is None:
    # Raise outside except; from None hides traceback but retains input_value context.
            raise PreferenceProjectionConflict("当前偏好不符合安全契约")
        if item.model_dump() != {key: value for key, value in raw.items() if key != "id"}:
            raise PreferenceProjectionConflict("当前偏好不是 canonical contract")
        if (
            not isinstance(raw["id"], int)
            or isinstance(raw["id"], bool)
            or not 1 <= raw["id"] <= MAX_JSON_SAFE_INTEGER
        ):
            raise PreferenceProjectionConflict("当前偏好 identity 无效")
        items.append(raw)
    if len({item["key"] for item in items}) != len(items):
        raise PreferenceProjectionConflict("当前偏好存在重复 key")
    if sum(len(item["key"]) + len(item["value"]) for item in items) > (
        MAX_PREFERENCE_TOTAL_CHARS
    ):
        raise PreferenceProjectionConflict("当前偏好总长度超过安全上限")
    return items


def _public_list(items: list[dict]) -> dict:
    payload = {
        "items": [
            {key: value for key, value in item.items() if key != "id"}
            for item in items
        ],
        "total": len(items),
        "total_chars": sum(
            len(item["key"]) + len(item["value"])
            for item in items
        ),
    }
    result = None
    try:
        result = PreferenceListDTO.model_validate(payload).model_dump()
    except (TypeError, ValueError):  # pragma: no cover - _snapshot already validates
        pass
    if result is None:  # pragma: no cover - _snapshot already validates
        raise PreferenceProjectionConflict("当前偏好列表无法校验")
    return result


def list_current_preferences(db_path: str, user_id: str) -> dict:
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        return _public_list(_snapshot(conn, user_id))


def list_preferences_for_settings(
    db_path: str,
    user_id: str,
    *,
    recovery_scope: str,
) -> dict:
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        items = _snapshot(conn, user_id)
        payload = {
            "items": items,
            "total": len(items),
            "total_chars": sum(
                len(item["key"]) + len(item["value"]) for item in items
            ),
            "recovery_scope": recovery_scope,
        }
        result = None
        try:
            result = PreferenceSettingsSnapshot.model_validate(payload).model_dump()
        except (TypeError, ValueError):
            pass
        if result is None:
            raise PreferenceProjectionConflict("设置页偏好列表无法校验")
        return result


def _insert(
    conn: Connection,
    user_id: str,
    key: str,
    value: str,
    timestamp: str,
) -> dict:
    cursor = conn.execute(
        "INSERT INTO preferences (user_id, key, value, revision, created_time, updated_time) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (user_id, key, value, timestamp, timestamp),
    )
    if cursor.rowcount != 1 or cursor.lastrowid is None:
        raise PreferenceProjectionConflict("偏好创建未精确命中")
    owner = conn.execute(
        "SELECT preference_id, user_id, created_time FROM preference_owners "
        "WHERE preference_id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    if owner != (cursor.lastrowid, user_id, timestamp):
        raise PreferenceProjectionConflict("偏好 owner 未与新行原子落底")
    return {
        "id": cursor.lastrowid,
        "key": key,
        "value": value,
        "revision": 1,
        "created_time": timestamp,
        "updated_time": timestamp,
    }


def _update(
    conn: Connection,
    user_id: str,
    before: dict,
    value: str,
    timestamp: str,
) -> dict:
    changed = conn.execute(
        "UPDATE preferences SET value = ?, revision = revision + 1, updated_time = ? "
        "WHERE user_id = ? AND id = ? AND key = ? AND value = ? AND revision = ? "
        "AND created_time = ? AND updated_time = ?",
        (
            value,
            timestamp,
            user_id,
            before["id"],
            before["key"],
            before["value"],
            before["revision"],
            before["created_time"],
            before["updated_time"],
        ),
    ).rowcount
    if changed != 1:
        raise PreferenceProjectionConflict("偏好更新 CAS 未命中")
    return {
        **before,
        "value": value,
        "revision": before["revision"] + 1,
        "updated_time": timestamp,
    }


def _delete(conn: Connection, user_id: str, before: dict) -> None:
    changed = conn.execute(
        "DELETE FROM preferences WHERE user_id = ? AND id = ? AND key = ? "
        "AND value = ? AND revision = ? AND created_time = ? AND updated_time = ?",
        (
            user_id,
            before["id"],
            before["key"],
            before["value"],
            before["revision"],
            before["created_time"],
            before["updated_time"],
        ),
    ).rowcount
    if changed != 1:
        raise PreferenceProjectionConflict("偏好删除 CAS 未命中")
