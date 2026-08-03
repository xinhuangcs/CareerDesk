"""Durable idempotency boundary for assistant turns.

This module records whether a ``client_turn_id`` has already run and retains a
replayable terminal response. It is not an event source for tool transactions.
If an agent starts but cannot finish cleanly, the result becomes ``unknown`` and
the same turn is never rerun automatically. Terminal response text is retained
only for the reconnect window; older completed responses are replaced with a
short notice while their idempotency identity and completion metadata remain.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import ValidationError

from ...platform.database import now_iso, read_connection, transaction
from .contracts import (
    MAX_CHAT_PROPOSAL_OPERATIONS,
    ChatProposalOperationReference,
    ChatUiActionReference,
    ProposalSurface,
)

TurnStatus = Literal[
    "absent", "execute", "running", "completed", "unknown", "cancelled", "conflict",
    "session_busy",
]
PersistedTurnState = Literal["absent", "running", "completed", "unknown", "cancelled"]

# Covers disconnects, sleep, and later retrieval from another device. Expiry
# replaces only replay text and never deletes a ledger row.
TURN_REPLAY_RETENTION_DAYS = 30
_PROPOSAL_SIDECAR_DOMAIN = b"careerdesk:assistant-turn-proposals:v1\0"
_PROPOSAL_SIDECAR_KEY_PREFIX = "assistant_turn_proposals.v1."


@dataclass(frozen=True, slots=True)
class TurnDecision:
    """Neutral inspect/claim result; only ``execute`` includes an owner token."""

    status: TurnStatus
    attempt_token: str | None = None
    replay_events: list[dict] | None = None
    error: dict | None = None


@dataclass(frozen=True, slots=True)
class TurnStatusSnapshot:
    """Minimal ledger projection required by the status endpoint."""

    state: PersistedTurnState
    proposal_operations: tuple[dict[str, str], ...] = ()


class TurnOwnershipLost(RuntimeError):
    """The attempt token is stale, so its worker must not publish ``done``."""


def chat_request_hash(
    session_id: str,
    message: str,
    attachments: Sequence[Any],
    *,
    review_supplement_reference: str | None = None,
    output_locale: str = "zh-CN",
) -> str:
    """Hash the effective chat request without copying its content into the ledger.

    Attachment order affects model input, so JSON object keys are sorted but
    lists are not. A review-supplement reference is part of command identity;
    an omitted reference is represented as null in the same contract. Both
    Pydantic models and already-dumped mappings are accepted.
    """
    normalized: list[dict] = []
    for attachment in attachments:
        if isinstance(attachment, Mapping):
            payload = dict(attachment)
        else:
            dump = getattr(attachment, "model_dump", None)
            if not callable(dump):
                raise TypeError("attachment must be a mapping or a Pydantic model")
            payload = dump(mode="json", exclude_none=False)
            if not isinstance(payload, Mapping):
                raise TypeError("attachment model_dump() must return a mapping")
            payload = dict(payload)
        normalized.append(payload)

    canonical_reference = None
    if review_supplement_reference is not None:
        try:
            canonical_reference = str(uuid.UUID(review_supplement_reference))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(
                "review_supplement_reference must be a canonical UUID",
            ) from error
        if canonical_reference != review_supplement_reference:
            raise ValueError("review_supplement_reference must be a canonical UUID")

    request = {
        "attachments": normalized,
        "message": message,
        "output_locale": output_locale,
        "review_supplement_reference": canonical_reference,
        "session_id": session_id,
        "version": 3,
    }

    canonical = json.dumps(
        request,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def inspect_turn(db_path: str, user_id: str, client_turn_id: str,
                 session_id: str, request_hash: str) -> TurnDecision:
    """Read a turn decision without claiming execution ownership."""
    _validate_identity(user_id, client_turn_id, session_id, request_hash)
    with read_connection(db_path) as conn:
        return _inspect_with_connection(
            conn, user_id, client_turn_id, session_id, request_hash,
        )


def read_turn_state(db_path: str, user_id: str,
                    client_turn_id: str) -> PersistedTurnState:
    """Read a tenant turn state without loading request or replay content."""
    if not user_id or not client_turn_id:
        raise ValueError("user_id and client_turn_id must be non-empty")
    with read_connection(db_path) as conn:
        return _read_turn_state_with_connection(conn, user_id, client_turn_id)


def read_turn_status(db_path: str, user_id: str,
                     client_turn_id: str) -> TurnStatusSnapshot:
    """Read turn state and strict tenant-scoped proposal references."""
    if not user_id or not client_turn_id:
        raise ValueError("user_id and client_turn_id must be non-empty")
    with read_connection(db_path) as conn:
        state = _read_turn_state_with_connection(conn, user_id, client_turn_id)
        if state in {"absent", "cancelled"}:
            return TurnStatusSnapshot(state=state)
        if state == "completed":
            row = conn.execute(
                "SELECT replay_events_json FROM assistant_turns "
                "WHERE user_id=? AND client_turn_id=? AND state='completed'",
                (user_id, client_turn_id),
            ).fetchone()
            operations = _proposal_operations_from_replay(
                row[0] if row is not None else None,
            )
        else:
            operations = _read_proposal_sidecar(
                conn, user_id, client_turn_id,
            )
    return TurnStatusSnapshot(state=state, proposal_operations=tuple(operations))


def record_proposal_operation(
    db_path: str,
    user_id: str,
    client_turn_id: str,
    attempt_token: str,
    surface: ProposalSurface,
    operation_id: str,
) -> dict[str, str]:
    """Idempotently record a proposal reference after a tool commit.

    This is the crash-recovery fallback for ``Hook.after_tool``. The primary
    proposal path should call :func:`record_proposal_operation_in_transaction`
    inside the business journal transaction, avoiding a hard-kill window
    between the journal commit and this sidecar write.
    """
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return record_proposal_operation_in_transaction(
            conn,
            user_id,
            client_turn_id,
            attempt_token,
            surface,
            operation_id,
        )


def record_proposal_operation_in_transaction(
    conn: sqlite3.Connection,
    user_id: str,
    client_turn_id: str,
    attempt_token: str,
    surface: ProposalSurface,
    operation_id: str,
) -> dict[str, str]:
    """Record a proposal in the caller transaction; failure must roll it back."""
    if not user_id or not client_turn_id or not attempt_token:
        raise ValueError("proposal turn identity must be non-empty")
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise RuntimeError("proposal recorder requires an active SQLite transaction")
    reference = _proposal_reference(surface, operation_id)
    sidecar_key = _proposal_sidecar_key(user_id, client_turn_id)
    timestamp = now_iso()
    owner = conn.execute(
        "SELECT state, attempt_token FROM assistant_turns "
        "WHERE user_id=? AND client_turn_id=?",
        (user_id, client_turn_id),
    ).fetchone()
    if owner != ("running", attempt_token):
        raise TurnOwnershipLost(
            f"assistant turn ownership lost: {user_id}/{client_turn_id}",
        )
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (sidecar_key,),
    ).fetchone()
    operations = _decode_proposal_sidecar(row[0]) if row is not None else []
    identity = (reference["surface"], reference["operation_id"])
    existing = {(item["surface"], item["operation_id"]) for item in operations}
    if identity in existing:
        return reference
    if any(item["operation_id"] == reference["operation_id"] for item in operations):
        raise ValueError("one proposal operation_id cannot use multiple surfaces")
    if len(operations) >= MAX_CHAT_PROPOSAL_OPERATIONS:
        raise ValueError("proposal operation limit exceeded")
    operations.append(reference)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (sidecar_key, _encode_proposal_sidecar(operations)),
    )
    updated = conn.execute(
        "UPDATE assistant_turns SET updated_time=? "
        "WHERE user_id=? AND client_turn_id=? "
        "AND state='running' AND attempt_token=?",
        (timestamp, user_id, client_turn_id, attempt_token),
    ).rowcount
    if updated != 1:  # pragma: no cover - owner row was read in the same write txn
        raise TurnOwnershipLost(
            f"assistant turn ownership lost: {user_id}/{client_turn_id}",
        )
    return reference


def cancel_turn_if_absent(db_path: str, user_id: str,
                          client_turn_id: str) -> PersistedTurnState:
    """Atomically reserve an unseen turn UUID without overwriting existing state."""
    if not user_id or not client_turn_id:
        raise ValueError("user_id and client_turn_id must be non-empty")
    try:
        canonical_turn_id = str(uuid.UUID(client_turn_id))
    except (ValueError, AttributeError) as error:
        raise ValueError("client_turn_id must be a UUID") from error
    if canonical_turn_id != client_turn_id:
        raise ValueError("client_turn_id must be a canonical UUID")

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_turn_state_with_connection(conn, user_id, client_turn_id)
        if state != "absent":
            return state
        conn.execute(
            "INSERT INTO assistant_turn_cancellations "
            "(user_id, client_turn_id, created_time) VALUES (?, ?, ?)",
            (user_id, client_turn_id, now_iso()),
        )
    return "cancelled"


def cancel_running_turn(db_path: str, user_id: str, client_turn_id: str,
                        attempt_token: str) -> bool:
    """Replace the owned running row with an immutable cancellation tombstone."""
    if not user_id or not client_turn_id or not attempt_token:
        raise ValueError("turn cancellation identity must be non-empty")
    sidecar_key = _proposal_sidecar_key(user_id, client_turn_id)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            "DELETE FROM assistant_turns WHERE user_id=? AND client_turn_id=? "
            "AND state='running' AND attempt_token=?",
            (user_id, client_turn_id, attempt_token),
        ).rowcount
        if deleted != 1:
            return False
        conn.execute("DELETE FROM meta WHERE key=?", (sidecar_key,))
        conn.execute(
            "INSERT INTO assistant_turn_cancellations "
            "(user_id, client_turn_id, created_time) VALUES (?, ?, ?)",
            (user_id, client_turn_id, now_iso()),
        )
    return True


def claim_turn(db_path: str, user_id: str, client_turn_id: str,
               session_id: str, request_hash: str) -> TurnDecision:
    """Claim a new turn or return the stable existing turn/session decision.

    ``BEGIN IMMEDIATE`` serializes the primary-key check, session check, and
    insert at one SQLite write point; the partial unique index is the backstop.
    """
    _validate_identity(user_id, client_turn_id, session_id, request_hash)
    attempt_token = str(uuid.uuid4())
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        decision = _inspect_with_connection(
            conn, user_id, client_turn_id, session_id, request_hash,
        )
        if decision.status != "absent":
            return decision
        conn.execute(
            "INSERT INTO assistant_turns ("
            "user_id, client_turn_id, session_id, request_hash, state, attempt_token, "
            "created_time, updated_time) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (user_id, client_turn_id, session_id, request_hash,
             attempt_token, timestamp, timestamp),
        )
    return TurnDecision(status="execute", attempt_token=attempt_token)


def complete_turn(db_path: str, user_id: str, client_turn_id: str,
                  attempt_token: str, replay_events: list[dict]) -> list[dict[str, str]]:
    """Publish ``completed`` via CAS; callers may send ``done`` only afterward."""
    # Validate the proposal-independent replay shape first. The sidecar must be
    # read inside the owner's transaction.
    _encode_replay_events(_with_proposal_operations(replay_events, []))
    timestamp = now_iso()
    sidecar_key = _proposal_sidecar_key(user_id, client_turn_id)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT state, attempt_token FROM assistant_turns "
            "WHERE user_id=? AND client_turn_id=?",
            (user_id, client_turn_id),
        ).fetchone()
        if owner != ("running", attempt_token):
            raise TurnOwnershipLost(
                f"assistant turn ownership lost: {user_id}/{client_turn_id}",
            )
        row = conn.execute("SELECT value FROM meta WHERE key=?", (sidecar_key,)).fetchone()
        proposal_operations = (
            _decode_proposal_sidecar(row[0]) if row is not None else []
        )
        encoded = _encode_replay_events(
            _with_proposal_operations(replay_events, proposal_operations),
        )
        cursor = conn.execute(
            "UPDATE assistant_turns SET state='completed', attempt_token=NULL, "
            "replay_events_json=?, updated_time=?, finished_time=? "
            "WHERE user_id=? AND client_turn_id=? AND state='running' AND attempt_token=?",
            (encoded, timestamp, timestamp, user_id, client_turn_id, attempt_token),
        )
        if cursor.rowcount != 1:
            raise TurnOwnershipLost(
                f"assistant turn ownership lost: {user_id}/{client_turn_id}",
            )
        conn.execute("DELETE FROM meta WHERE key=?", (sidecar_key,))
    return proposal_operations


def mark_turn_unknown(db_path: str, user_id: str, client_turn_id: str,
                      attempt_token: str, error: dict) -> bool:
    """Close as ``unknown`` via CAS, returning false if ownership was lost."""
    encoded = _encode_unknown_error(error)
    timestamp = now_iso()
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE assistant_turns SET state='unknown', attempt_token=NULL, "
            "unknown_error_json=?, updated_time=?, finished_time=? "
            "WHERE user_id=? AND client_turn_id=? AND state='running' AND attempt_token=?",
            (encoded, timestamp, timestamp, user_id, client_turn_id, attempt_token),
        )
    return cursor.rowcount == 1


def recover_interrupted_turns(db_path: str) -> int:
    """Close running turns that lost their executor as ``unknown`` at startup."""
    error = {
        "code": "turn_outcome_unknown",
        "message": (
            "上次应用退出时这一轮尚未得到完整完成确认；"
            "部分操作可能已经执行，请先检查相关记录。"
        ),
        "retryable": False,
    }
    encoded = _encode_unknown_error(error)
    timestamp = now_iso()
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE assistant_turns SET state='unknown', attempt_token=NULL, "
            "unknown_error_json=?, updated_time=?, finished_time=? WHERE state='running'",
            (encoded, timestamp, timestamp),
        )
    return cursor.rowcount


def evict_expired_completed_replays(
    db_path: str,
    *,
    reference_time: datetime | None = None,
    retention_days: int = TURN_REPLAY_RETENTION_DAYS,
) -> int:
    """Replace expired completed-response copies with a safe replay notice.

    The row cannot be deleted: a late request with the same ``client_turn_id``
    could otherwise execute already-committed tools again. The completed state
    and history/attachment-consumption metadata in ``done`` must remain so the
    client does not restore consumed attachments. Small ``unknown`` payloads
    have different semantics and are not covered by this policy.
    """
    if (not isinstance(retention_days, int) or isinstance(retention_days, bool)
            or retention_days <= 0):
        raise ValueError("retention_days must be a positive integer")
    current = reference_time or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    cutoff = (current - timedelta(days=retention_days)).isoformat()
    timestamp = current.isoformat()
    replacement = (
        f"这轮已完成；为减少敏感信息留存，超过 {retention_days} 天的原回复内容已清理。"
        "这次重复请求不会再次执行任何操作；如曾保存记录，请到对应页面核对，"
        "或发送一条新消息继续。"
    )
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE assistant_turns SET "
            "replay_events_json=json_set(replay_events_json, '$[0].data.text', ?), "
            "replay_evicted_time=? "
            "WHERE state='completed' AND replay_evicted_time IS NULL "
            "AND finished_time < ? "
            "AND json_type(replay_events_json, '$[0].data.text')='text'",
            (replacement, timestamp, cutoff),
        )
    return cursor.rowcount


def _inspect_with_connection(conn: sqlite3.Connection, user_id: str, client_turn_id: str,
                             session_id: str, request_hash: str) -> TurnDecision:
    row = conn.execute(
        "SELECT session_id, request_hash, state, replay_events_json, unknown_error_json "
        "FROM assistant_turns WHERE user_id=? AND client_turn_id=?",
        (user_id, client_turn_id),
    ).fetchone()
    if row is not None:
        stored_session, stored_hash, state, replay_json, error_json = row
        if stored_session != session_id or stored_hash != request_hash:
            return TurnDecision(
                status="conflict",
                error={
                    "code": "idempotency_key_reused",
                    "message": "这个 client_turn_id 已经用于另一个请求。",
                    "retryable": False,
                },
            )
        if state == "running":
            return TurnDecision(
                status="running",
                error={
                    "code": "turn_in_progress",
                    "message": "这一轮仍在处理，请稍后用同一 client_turn_id 重试。",
                    "retryable": True,
                },
            )
        if state == "completed":
            return TurnDecision(
                status="completed",
                replay_events=_decode_replay_events(replay_json),
            )
        if state == "unknown":
            return TurnDecision(status="unknown", error=_decode_error(error_json))
        raise RuntimeError(f"unsupported assistant turn state: {state!r}")

    cancelled = conn.execute(
        "SELECT 1 FROM assistant_turn_cancellations "
        "WHERE user_id=? AND client_turn_id=?",
        (user_id, client_turn_id),
    ).fetchone()
    if cancelled is not None:
        return TurnDecision(
            status="cancelled",
            error={
                "code": "turn_cancelled",
                "message": "这一轮已被安全取消；请使用新的 client_turn_id 重新发送。",
                "retryable": False,
            },
        )

    running = conn.execute(
        "SELECT client_turn_id FROM assistant_turns "
        "WHERE user_id=? AND session_id=? AND state='running'",
        (user_id, session_id),
    ).fetchone()
    if running is not None:
        return TurnDecision(
            status="session_busy",
            error={
                "code": "session_busy",
                "message": "这个会话已有一轮在处理，请等它结束后再发送。",
                "retryable": True,
            },
        )
    return TurnDecision(status="absent")


def _read_turn_state_with_connection(
    conn: sqlite3.Connection,
    user_id: str,
    client_turn_id: str,
) -> PersistedTurnState:
    rows = conn.execute(
        "SELECT state FROM assistant_turns WHERE user_id=? AND client_turn_id=? "
        "UNION ALL "
        "SELECT 'cancelled' FROM assistant_turn_cancellations "
        "WHERE user_id=? AND client_turn_id=?",
        (user_id, client_turn_id, user_id, client_turn_id),
    ).fetchall()
    if not rows:
        return "absent"
    if len(rows) != 1:
        raise RuntimeError("assistant turn and cancellation tombstone overlap")
    state = rows[0][0]
    if state not in {"running", "completed", "unknown", "cancelled"}:
        raise RuntimeError(f"unsupported assistant turn state: {state!r}")
    return state


def _proposal_sidecar_key(user_id: str, client_turn_id: str) -> str:
    identity = (
        _PROPOSAL_SIDECAR_DOMAIN
        + user_id.encode("utf-8")
        + b"\0"
        + client_turn_id.encode("utf-8")
    )
    return _PROPOSAL_SIDECAR_KEY_PREFIX + hashlib.sha256(identity).hexdigest()


def _proposal_reference(surface: object, operation_id: object) -> dict[str, str]:
    reference = ChatProposalOperationReference.model_validate({
        "surface": surface,
        "operation_id": operation_id,
    })
    return reference.model_dump(mode="json")


def _validate_proposal_operations(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_CHAT_PROPOSAL_OPERATIONS:
        raise ValueError(
            "proposal_operations must be an array of at most "
            f"{MAX_CHAT_PROPOSAL_OPERATIONS} references",
        )
    operations = [
        _proposal_reference(
            item.get("surface") if isinstance(item, dict) else None,
            item.get("operation_id") if isinstance(item, dict) else None,
        )
        for item in value
    ]
    if any(
        not isinstance(item, dict)
        or set(item) != {"surface", "operation_id"}
        for item in value
    ):
        raise ValueError("proposal operation references require exact minimal fields")
    identities = [(item["surface"], item["operation_id"]) for item in operations]
    if len(set(identities)) != len(identities):
        raise ValueError("proposal_operations cannot contain duplicate references")
    operation_ids = [item["operation_id"] for item in operations]
    if len(set(operation_ids)) != len(operation_ids):
        raise ValueError("one proposal operation_id cannot use multiple surfaces")
    return operations


def _encode_proposal_sidecar(operations: list[dict[str, str]]) -> str:
    return _json_dumps({
        "proposal_operations": _validate_proposal_operations(operations),
        "version": 1,
    })


def _decode_proposal_sidecar(encoded: str) -> list[dict[str, str]]:
    loaded = json.loads(encoded)
    if (
        not isinstance(loaded, dict)
        or set(loaded) != {"proposal_operations", "version"}
        or loaded.get("version") != 1
    ):
        raise ValueError("invalid assistant proposal sidecar")
    return _validate_proposal_operations(loaded.get("proposal_operations"))


def _read_proposal_sidecar(
    conn: sqlite3.Connection,
    user_id: str,
    client_turn_id: str,
) -> list[dict[str, str]]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (_proposal_sidecar_key(user_id, client_turn_id),),
    ).fetchone()
    if row is None:
        return []
    try:
        return _decode_proposal_sidecar(row[0])
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as error:
        raise RuntimeError("assistant proposal sidecar is invalid") from error


def _with_proposal_operations(
    events: list[dict],
    proposal_operations: list[dict[str, str]],
) -> list[dict]:
    if not isinstance(events, list) or len(events) != 2:
        raise ValueError("replay events must contain exactly message_snapshot and done")
    copied = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            raise ValueError("replay events must be dictionaries with dictionary data")
        copied.append({**event, "data": dict(event["data"])})
    copied[1]["data"]["proposal_operations"] = _validate_proposal_operations(
        proposal_operations,
    )
    copied[1]["data"].setdefault("ui_actions", [])
    return copied


def _validate_ui_actions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("ui_actions must be an array of at most 8 actions")
    actions = [
        ChatUiActionReference.model_validate(item).model_dump(exclude_none=True)
        for item in value
    ]
    identities = {(item["kind"], item.get("resource_id")) for item in actions}
    if len(identities) != len(actions):
        raise ValueError("ui_actions cannot contain duplicate references")
    return actions


def _encode_replay_events(events: list[dict]) -> str:
    if not isinstance(events, list) or len(events) != 2:
        raise ValueError("replay events must contain exactly message_snapshot and done")
    if any(not isinstance(event, dict) or not isinstance(event.get("data"), dict)
           for event in events):
        raise ValueError("replay events must be dictionaries with dictionary data")
    if [event.get("event") for event in events] != ["message_snapshot", "done"]:
        raise ValueError("replay events must be message_snapshot followed by done")
    if not isinstance(events[0]["data"].get("text"), str):
        raise ValueError("message_snapshot requires string text")
    done = events[1]["data"]
    if any(not isinstance(done.get(key), str) or not done[key]
           for key in ("session", "message_id", "client_turn_id")):
        raise ValueError("replay done requires session, message_id and client_turn_id")
    if (done.get("history_committed") is not True
            or done.get("attachments") != "consumed"
            or done.get("replayed") is not True):
        raise ValueError("replay done must describe a committed, consumed replay")
    if "proposal_operations" not in done:
        raise ValueError("replay done requires proposal_operations")
    _validate_proposal_operations(done["proposal_operations"])
    if "ui_actions" not in done:
        raise ValueError("replay done requires ui_actions")
    done["ui_actions"] = _validate_ui_actions(done["ui_actions"])
    return _json_dumps(events)


def _decode_replay_events(encoded: str | None) -> list[dict]:
    loaded = json.loads(encoded or "null")
    if not isinstance(loaded, list):
        raise RuntimeError("completed assistant turn has invalid replay events")
    # Existing v23 completed rows predate proposal recovery. Normalize them to
    # an empty array on read; every new completion persists the field explicitly.
    if (
        len(loaded) == 2
        and isinstance(loaded[1], dict)
        and isinstance(loaded[1].get("data"), dict)
    ):
        loaded[1]["data"].setdefault("proposal_operations", [])
        loaded[1]["data"].setdefault("ui_actions", [])
    # Reuse write-side validation so manually corrupted rows are not trusted SSE.
    try:
        _encode_replay_events(loaded)
    except (TypeError, ValueError, ValidationError) as error:
        raise RuntimeError("completed assistant turn has invalid replay events") from error
    return loaded


def _proposal_operations_from_replay(
    encoded: str | None,
) -> list[dict[str, str]]:
    events = _decode_replay_events(encoded)
    try:
        return _validate_proposal_operations(
            events[1]["data"].get("proposal_operations"),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise RuntimeError("completed assistant turn has invalid proposal recovery") from error


def _encode_unknown_error(error: dict) -> str:
    if not isinstance(error, dict):
        raise TypeError("unknown error must be a dictionary")
    payload = dict(error)
    if (not isinstance(payload.get("code"), str) or not payload["code"]
            or not isinstance(payload.get("message"), str) or not payload["message"]):
        raise ValueError("unknown error requires non-empty code and message")
    payload["retryable"] = False
    return _json_dumps(payload)


def _decode_error(encoded: str | None) -> dict:
    loaded = json.loads(encoded or "null")
    if not isinstance(loaded, dict):
        raise RuntimeError("unknown assistant turn has invalid error payload")
    try:
        _encode_unknown_error(loaded)
    except (TypeError, ValueError) as error:
        raise RuntimeError("unknown assistant turn has invalid error payload") from error
    loaded["retryable"] = False
    return loaded


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_identity(user_id: str, client_turn_id: str,
                       session_id: str, request_hash: str) -> None:
    if not user_id or not client_turn_id or not session_id:
        raise ValueError("user_id, client_turn_id and session_id must be non-empty")
    if (len(request_hash) != 64
            or any(character not in "0123456789abcdef" for character in request_hash)):
        raise ValueError("request_hash must be a lowercase SHA-256 hex digest")
