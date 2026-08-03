"""Cooperative cancellation for active Assistant turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import Event, RLock
from typing import Literal

from ...features.applications.public import (
    ApplicationService,
    get_application_delete_operation,
    get_application_merge_operation,
    reject_application_delete_operation,
    reject_application_merge_operation,
)
from ...features.reviews.public import (
    get_review_operation,
    reject_review_operation,
    reject_review_record_operations_for_turn,
)
from . import ledger


class TurnCancellationRequested(RuntimeError):
    """Stop the current turn at a cooperative execution boundary."""


class TurnCancellationNotReversible(RuntimeError):
    """The turn has a committed result that cannot be hidden as cancelled."""


@dataclass(slots=True)
class TurnCancellationControl:
    """Track cancellation, active tools, and committed effects for one turn."""

    _requested: Event = field(default_factory=Event)
    _lock: RLock = field(default_factory=RLock)
    _active_tools: int = 0
    _committed_effects: bool = False
    _finalizing: bool = False
    _task: asyncio.Task | None = None

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    @property
    def committed_effects(self) -> bool:
        with self._lock:
            return self._committed_effects

    def request(self) -> bool:
        with self._lock:
            if self._finalizing:
                return False
            self._requested.set()
            task = self._task if self._active_tools == 0 else None
        if task is not None and not task.done():
            task.cancel()
        return True

    def attach_task(self, task: asyncio.Task) -> None:
        with self._lock:
            self._task = task
            cancel_now = self.requested and self._active_tools == 0
        if cancel_now and not task.done():
            task.cancel()

    def detach_task(self, task: asyncio.Task) -> None:
        with self._lock:
            if self._task is task:
                self._task = None

    def checkpoint(self) -> None:
        if self.requested:
            raise TurnCancellationRequested

    def begin_commit(self) -> None:
        with self._lock:
            if self.requested:
                raise TurnCancellationRequested
            self._finalizing = True

    def begin_tool(self) -> None:
        with self._lock:
            if self.requested:
                raise TurnCancellationRequested
            self._active_tools += 1

    def finish_tool(self, *, committed: bool = False) -> None:
        with self._lock:
            if committed:
                self._committed_effects = True
            if self._active_tools > 0:
                self._active_tools -= 1
            stop_now = self.requested and self._active_tools == 0
        if stop_now:
            raise TurnCancellationRequested

    def mark_committed(self) -> None:
        with self._lock:
            self._committed_effects = True


_ACTIVE_TURNS: dict[tuple[str, str, str], TurnCancellationControl] = {}
_ACTIVE_TURNS_LOCK = RLock()


def _key(db_path: str, user_id: str, client_turn_id: str) -> tuple[str, str, str]:
    return db_path, user_id, client_turn_id


def register_active_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str,
    control: TurnCancellationControl,
) -> None:
    key = _key(db_path, user_id, client_turn_id)
    with _ACTIVE_TURNS_LOCK:
        current = _ACTIVE_TURNS.get(key)
        if current is not None and current is not control:
            raise RuntimeError("assistant turn already has an active cancellation owner")
        _ACTIVE_TURNS[key] = control


def unregister_active_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str,
    control: TurnCancellationControl,
) -> None:
    key = _key(db_path, user_id, client_turn_id)
    with _ACTIVE_TURNS_LOCK:
        if _ACTIVE_TURNS.get(key) is control:
            del _ACTIVE_TURNS[key]


def request_active_turn_cancel(
    db_path: str,
    user_id: str,
    client_turn_id: str,
) -> Literal["accepted", "finalizing", "missing"]:
    with _ACTIVE_TURNS_LOCK:
        control = _ACTIVE_TURNS.get(_key(db_path, user_id, client_turn_id))
    if control is None:
        return "missing"
    return "accepted" if control.request() else "finalizing"


def _reject_application_proposal(
    db_path: str,
    user_id: str,
    surface: str,
    operation_id: str,
) -> None:
    if surface == "intake":
        service = ApplicationService(db_path, None)
        operation = service.get_intake_operation(user_id, operation_id)
        if operation is None:
            raise RuntimeError("cancelled intake proposal is missing")
        if operation["state"] == "completed":
            raise TurnCancellationNotReversible
        if operation["state"] == "pending":
            service.reject_intake_operation(user_id, operation_id)
        return
    if surface == "application_merge":
        operation = get_application_merge_operation(db_path, user_id, operation_id)
        if operation is None:
            raise RuntimeError("cancelled merge proposal is missing")
        if operation["state"] == "completed":
            raise TurnCancellationNotReversible
        if operation["state"] == "pending":
            reject_application_merge_operation(db_path, user_id, operation_id)
        return
    if surface == "application_delete":
        operation = get_application_delete_operation(db_path, user_id, operation_id)
        if operation is None:
            raise RuntimeError("cancelled delete proposal is missing")
        if operation["state"] == "completed":
            raise TurnCancellationNotReversible
        if operation["state"] == "pending":
            reject_application_delete_operation(db_path, user_id, operation_id)
        return
    if surface == "review_undo":
        operation = get_review_operation(db_path, user_id, operation_id)
        if operation is None:
            raise RuntimeError("cancelled review proposal is missing")
        if operation["state"] == "completed":
            raise TurnCancellationNotReversible
        if operation["state"] == "pending":
            reject_review_operation(db_path, user_id, operation_id)
        return
    raise RuntimeError("unsupported Assistant proposal surface")


def reject_cancelled_turn_proposals(
    db_path: str,
    user_id: str,
    client_turn_id: str,
) -> None:
    """Reject every reversible proposal owned by one stopped turn."""
    review_operations = reject_review_record_operations_for_turn(
        db_path,
        user_id,
        client_turn_id,
    )
    if any(operation["state"] == "completed" for operation in review_operations):
        raise TurnCancellationNotReversible

    snapshot = ledger.read_turn_status(db_path, user_id, client_turn_id)
    for reference in snapshot.proposal_operations:
        _reject_application_proposal(
            db_path,
            user_id,
            reference["surface"],
            reference["operation_id"],
        )
