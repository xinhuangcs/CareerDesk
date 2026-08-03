"""Journal envelopes, supplements, and state/revision CAS without domain projections."""

import json
from dataclasses import dataclass
from sqlite3 import Connection

from ...platform.database import loads_json, now_iso, read_connection, transaction

_ENTRY_COLUMNS = (
    "id, user_id, kind, content, created_time, processed_time, extraction_json, "
    "derivation_json, state, revision, parent_journal_id"
)

_MAX_OPERATION_CANDIDATES_PER_READ = 128
_OPERATION_CANDIDATE_COLUMNS = (
    "id, operation_id, extraction_json, derivation_json, kind"
)
_OPERATION_CANDIDATE_SQL = (
    "WITH candidate_ids(id) AS ("
    "SELECT id FROM journal WHERE operation_id IS NOT NULL AND user_id = ? AND "
    "json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json "
    "ELSE '{}' END, '$.client_turn_id') = ? UNION "
    "SELECT id FROM journal WHERE operation_id IS NOT NULL AND user_id = ? AND "
    "json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
    "ELSE '{}' END, '$.operation.client_turn_id') = ?) "
    f"SELECT {_OPERATION_CANDIDATE_COLUMNS} FROM journal "
    "WHERE user_id = ? AND id IN (SELECT id FROM candidate_ids) "
    "ORDER BY id LIMIT ?"
)


@dataclass(frozen=True, slots=True)
class OperationCandidate:
    """Minimal raw fields broadly located by either untrusted persisted turn copy."""

    journal_id: object
    operation_id: object
    extraction_json: object
    derivation_json: object
    kind: object


def read_operation_candidates_for_turn_in_transaction(
    conn: Connection,
    user_id: str,
    client_turn_id: str,
    *,
    maximum: int,
) -> tuple[OperationCandidate, ...]:
    """Broadly locate turn candidates without classifying or trusting their receipts.

    The caller owns the transaction, family contract, DTO validation and overflow
    error.  Reading one row beyond ``maximum`` preserves each feature's fail-closed
    budget while this neutral seam owns only tenant filtering, union de-duplication
    and stable journal ordering.
    """
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= _MAX_OPERATION_CANDIDATES_PER_READ
    ):
        raise ValueError("operation candidate maximum 超过安全边界")
    rows = conn.execute(
        _OPERATION_CANDIDATE_SQL,
        (
            user_id,
            client_turn_id,
            user_id,
            client_turn_id,
            user_id,
            maximum + 1,
        ),
    ).fetchall()
    return tuple(OperationCandidate(*tuple(row)) for row in rows)


def _entry(row) -> dict:
    (journal_id, user_id, kind, content, created_time, processed_time, extraction_json,
     derivation_json, state, revision, parent_journal_id) = row
    return {
        "id": journal_id,
        "user_id": user_id,
        "kind": kind,
        "content": content,
        "created_time": created_time,
        "processed_time": processed_time,
        "extraction": loads_json(extraction_json, None),
        "derivation": loads_json(derivation_json, None),
        "state": state,
        "revision": revision,
        "parent_journal_id": parent_journal_id,
    }


def append_review(db_path: str, user_id: str, content: str) -> dict:
    """Persist raw review text before the model call and return its initial revision."""
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, state) "
            "VALUES (?, 'review', ?, ?, 'pending')",
            (user_id, content, now_iso()),
        )
        return {"id": cursor.lastrowid, "revision": 0}


def get_entry(db_path: str, user_id: str, journal_id: int) -> dict | None:
    """Read a user's journal row; return None for absent or cross-user IDs."""
    with read_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM journal WHERE user_id = ? AND id = ?",
            (user_id, journal_id),
        ).fetchone()
    return _entry(row) if row else None


def cache_review_extraction(db_path: str, user_id: str, journal_id: int, extraction: dict, *,
                            expected_state: str, expected_revision: int) -> int | None:
    """Cache extraction and enter awaiting_user without overwriting newer revisions."""
    with transaction(db_path) as conn:
        row = conn.execute(
            "UPDATE journal SET extraction_json = ?, state = 'awaiting_user', revision = revision + 1 "
            "WHERE user_id = ? AND id = ? AND kind = 'review' AND state = ? AND revision = ? "
            "RETURNING revision",
            (json.dumps(extraction, ensure_ascii=False), user_id, journal_id,
             expected_state, expected_revision),
        ).fetchone()
    return row[0] if row else None


def fail_review_extraction(db_path: str, user_id: str, journal_id: int, *,
                           expected_state: str, expected_revision: int) -> bool:
    """Close an unpublished failed extraction without presenting it as awaiting input."""
    with transaction(db_path) as conn:
        changed = conn.execute(
            "UPDATE journal SET state = 'failed', derivation_json = ?, revision = revision + 1 "
            "WHERE user_id = ? AND id = ? AND kind = 'review' AND state = ? AND revision = ?",
            (json.dumps({"extract_failed": True}, ensure_ascii=False), user_id, journal_id,
             expected_state, expected_revision),
        ).rowcount
    return changed == 1


def append_review_correction(db_path: str, user_id: str, parent_journal_id: int, content: str, *,
                             expected_revision: int) -> dict | None:
    """Append to an awaiting review and advance its parent revision atomically.

    The parent revision fences supplement generation, preventing stale out-of-order
    model results from publishing.
    """
    with transaction(db_path) as conn:
        parent = conn.execute(
            "UPDATE journal SET revision = revision + 1 "
            "WHERE user_id = ? AND id = ? AND kind = 'review' "
            "AND state = 'awaiting_user' AND revision = ? RETURNING revision",
            (user_id, parent_journal_id, expected_revision),
        ).fetchone()
        if parent is None:
            return None
        timestamp = now_iso()
        cursor = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, processed_time, "
            "derivation_json, state, parent_journal_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, 'applied', ?)",
            (
                user_id,
                content,
                timestamp,
                timestamp,
                json.dumps({
                    "merged_into": parent_journal_id,
                    "source_type": "review_supplement",
                }, ensure_ascii=False),
                parent_journal_id,
            ),
        )
        return {"id": cursor.lastrowid, "parent_revision": parent[0]}


def read_merged_corrections(db_path: str, user_id: str, journal_id: int) -> list[str]:
    """Read all same-tenant supplements explicitly linked to a review in order."""
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT content FROM journal WHERE user_id = ? AND kind = 'correction' "
            "AND parent_journal_id = ? AND operation_id IS NULL "
            "AND state = 'applied' AND json_extract(CASE WHEN "
            "json_valid(derivation_json) THEN derivation_json ELSE '{}' END, "
            "'$.source_type') = 'review_supplement' ORDER BY created_time, id",
            (user_id, journal_id),
        ).fetchall()
    return [content for (content,) in rows]


def claim_review_in_transaction(conn: Connection, user_id: str, journal_id: int, *,
                                expected_state: str, expected_revision: int) -> int | None:
    """Claim a review revision as the caller's first cross-table transaction DML."""
    row = conn.execute(
        "UPDATE journal SET revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'review' AND state = ? AND revision = ? "
        "RETURNING revision",
        (user_id, journal_id, expected_state, expected_revision),
    ).fetchone()
    return row[0] if row else None


def finish_review_in_transaction(conn: Connection, user_id: str, journal_id: int, *,
                                 claim_revision: int, extraction: dict, derivation: dict) -> int:
    """Publish review projections in the caller transaction or roll back on mismatch."""
    row = conn.execute(
        "UPDATE journal SET processed_time = ?, extraction_json = ?, derivation_json = ?, "
        "state = 'applied', revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'review' AND revision = ? RETURNING revision",
        (now_iso(), json.dumps(extraction, ensure_ascii=False),
         json.dumps(derivation, ensure_ascii=False), user_id, journal_id, claim_revision),
    ).fetchone()
    if row is None:
        raise RuntimeError("review derivation claim changed inside transaction")
    return row[0]


def void_review_in_transaction(conn: Connection, user_id: str, journal_id: int, *,
                               expected_revision: int, derivation: dict) -> int | None:
    """Atomically void an applied review while retaining its last applied audit time."""
    row = conn.execute(
        "UPDATE journal SET state = 'voided', derivation_json = ?, revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'review' "
        "AND state = 'applied' AND revision = ? RETURNING revision",
        (json.dumps({**derivation, "voided": True}, ensure_ascii=False),
         user_id, journal_id, expected_revision),
    ).fetchone()
    return row[0] if row else None


def applied_reviews(db_path: str, user_id: str) -> list[dict]:
    """Return applied reviews for metadata maintenance, never projection replay."""
    with read_connection(db_path) as conn:
        return applied_reviews_in_transaction(conn, user_id)


def applied_reviews_in_transaction(conn: Connection, user_id: str) -> list[dict]:
    """Read applied reviews in authoritative order within the caller transaction."""
    rows = conn.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM journal "
        "WHERE user_id = ? AND state = 'applied' AND kind = 'review' "
        "ORDER BY created_time, id",
        (user_id,),
    ).fetchall()
    return [_entry(row) for row in rows]


def snapshot(db_path: str, user_id: str) -> tuple[tuple[int, str, int], ...]:
    """Snapshot all journal revisions to detect writes during out-of-lock model work."""
    with read_connection(db_path) as conn:
        return snapshot_in_transaction(conn, user_id)


def snapshot_in_transaction(conn: Connection, user_id: str) -> tuple[tuple[int, str, int], ...]:
    rows = conn.execute(
        "SELECT id, state, revision FROM journal WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return tuple((journal_id, state, revision) for journal_id, state, revision in rows)
