"""Minimal company identity persistence and transaction-scoped reads."""

from ...platform.database import loads_json, normalize_application_identity_part, now_iso


def company_profile_in_transaction(conn, user_id: str, name: str) -> dict:
    """Read adaptation-relevant company metadata inside the caller's snapshot.

    The caller owns the transaction boundary.  Returning normalized values
    keeps aliases/notes representation details inside the Companies feature.
    """
    if not getattr(conn, "in_transaction", False):
        raise ValueError("company profile read requires an active transaction")
    name_key = normalize_application_identity_part(name)
    row = conn.execute(
        "SELECT aliases_json, notes FROM companies "
        "WHERE user_id = ? AND name_key = ?",
        (user_id, name_key),
    ).fetchone()
    if row is None:
        return {"aliases": [], "notes": None}
    loaded_aliases = loads_json(row[0], [])
    aliases = (
        [item for item in loaded_aliases if isinstance(item, str)]
        if isinstance(loaded_aliases, list)
        else []
    )
    return {
        "aliases": aliases,
        "notes": row[1] if isinstance(row[1], str) else None,
    }


def ensure_company_in_transaction(conn, user_id: str, name: str) -> int:
    """Ensure a company exists inside the caller transaction and return its ID.

    Never opens, commits, rolls back, or closes the transaction so cross-table workflows
    can commit company identity atomically with their projections.
    """
    changed = now_iso()
    name_key = normalize_application_identity_part(name)
    if not name_key:
        raise ValueError("公司名去除空白后不能为空")
    conn.execute(
        "INSERT INTO companies (user_id, name, created_time, updated_time) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, name_key) DO NOTHING",
        (user_id, name, changed, changed),
    )
    row = conn.execute(
        "SELECT id FROM companies WHERE user_id = ? AND name_key = ?",
        (user_id, name_key),
    ).fetchone()
    if row is None:  # pragma: no cover - same-transaction INSERT/SELECT invariant
        raise RuntimeError("company identity upsert failed")
    return row[0]
