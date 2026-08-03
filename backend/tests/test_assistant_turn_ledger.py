
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.orchestration.assistant.ledger import (
    TurnOwnershipLost,
    cancel_running_turn,
    cancel_turn_if_absent,
    chat_request_hash,
    claim_turn,
    complete_turn,
    evict_expired_completed_replays,
    inspect_turn,
    mark_turn_unknown,
    read_turn_status,
    read_turn_state,
    record_proposal_operation,
    record_proposal_operation_in_transaction,
    recover_interrupted_turns,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "assistant-turns.db")
    init_db(path)
    return path


def request_hash(session: str = "session-1", message: str = "你好") -> str:
    return chat_request_hash(session, message, [])


def replay(session: str, turn: str, text: str = "完成", *, ui_actions=None) -> list[dict]:
    return [
        {"event": "message_snapshot", "data": {"text": text}},
        {"event": "done", "data": {
            "session": session,
            "message_id": "message-1",
            "client_turn_id": turn,
            "history_committed": True,
            "attachments": "consumed",
            "replayed": True,
            "proposal_operations": [],
            "ui_actions": ui_actions or [],
        }},
    ]


def test_claim_complete_and_replay_are_monotonic(db_path):
    session = "session-1"
    turn = "turn-1"
    digest = request_hash(session)

    assert inspect_turn(db_path, "u1", turn, session, digest).status == "absent"
    claimed = claim_turn(db_path, "u1", turn, session, digest)
    assert claimed.status == "execute" and claimed.attempt_token

    running = inspect_turn(db_path, "u1", turn, session, digest)
    assert running.status == "running"
    assert running.attempt_token is None
    assert running.error["code"] == "turn_in_progress"

    events = replay(session, turn)
    complete_turn(db_path, "u1", turn, claimed.attempt_token, events)
    completed = inspect_turn(db_path, "u1", turn, session, digest)
    assert completed.status == "completed"
    assert completed.replay_events == events

    duplicate = claim_turn(db_path, "u1", turn, session, digest)
    assert duplicate.status == "completed"
    assert duplicate.replay_events == events


def test_completed_replay_persists_valid_ui_actions(db_path):
    session = "session-actions"
    turn = "turn-actions"
    claimed = claim_turn(db_path, "u1", turn, session, request_hash(session))
    events = replay(session, turn, ui_actions=[
        {"kind": "open_application_research", "resource_id": 7},
        {"kind": "open_questions"},
    ])
    complete_turn(db_path, "u1", turn, claimed.attempt_token, events)
    assert inspect_turn(
        db_path, "u1", turn, session, request_hash(session),
    ).replay_events == events


def test_read_turn_state_is_minimal_tenant_scoped_and_monotonic(db_path):
    completed_turn = "completed-turn"
    completed_session = "completed-session"
    unknown_turn = "unknown-turn"
    unknown_session = "unknown-session"

    assert read_turn_state(db_path, "u1", completed_turn) == "absent"
    completed = claim_turn(
        db_path,
        "u1",
        completed_turn,
        completed_session,
        request_hash(completed_session),
    )
    assert read_turn_state(db_path, "u1", completed_turn) == "running"
    assert read_turn_state(db_path, "u2", completed_turn) == "absent"

    complete_turn(
        db_path,
        "u1",
        completed_turn,
        completed.attempt_token,
        replay(completed_session, completed_turn),
    )
    assert read_turn_state(db_path, "u1", completed_turn) == "completed"
    assert read_turn_state(db_path, "u2", completed_turn) == "absent"

    unknown = claim_turn(
        db_path,
        "u1",
        unknown_turn,
        unknown_session,
        request_hash(unknown_session),
    )
    assert mark_turn_unknown(
        db_path,
        "u1",
        unknown_turn,
        unknown.attempt_token,
        {"code": "turn_outcome_unknown", "message": "未确认", "retryable": False},
    )
    assert read_turn_state(db_path, "u1", unknown_turn) == "unknown"
    assert read_turn_state(db_path, "u2", unknown_turn) == "absent"


def test_proposal_reference_survives_unknown_and_is_tenant_scoped(db_path):
    turn = "proposal-turn"
    session = "proposal-session"
    operation_id = "00000000-0000-4000-8000-000000000101"
    claimed = claim_turn(db_path, "u1", turn, session, request_hash(session))

    recorded = record_proposal_operation(
        db_path,
        "u1",
        turn,
        claimed.attempt_token,
        "application_delete",
        operation_id,
    )
    assert recorded == {
        "surface": "application_delete",
        "operation_id": operation_id,
    }
    running = read_turn_status(db_path, "u1", turn)
    assert running.state == "running"
    assert running.proposal_operations == (recorded,)
    other_tenant = read_turn_status(db_path, "u2", turn)
    assert other_tenant.state == "absent" and other_tenant.proposal_operations == ()

    assert mark_turn_unknown(
        db_path,
        "u1",
        turn,
        claimed.attempt_token,
        {"code": "turn_outcome_unknown", "message": "未确认", "retryable": False},
    )
    unknown = read_turn_status(db_path, "u1", turn)
    assert unknown.state == "unknown"
    assert unknown.proposal_operations == (recorded,)


def test_completed_proposal_reference_moves_into_replay_atomically(db_path):
    turn = "completed-proposal-turn"
    session = "completed-proposal-session"
    operation_id = "00000000-0000-4000-8000-000000000102"
    claimed = claim_turn(db_path, "u1", turn, session, request_hash(session))
    record_proposal_operation(
        db_path,
        "u1",
        turn,
        claimed.attempt_token,
        "intake",
        operation_id,
    )

    recorded = complete_turn(
        db_path,
        "u1",
        turn,
        claimed.attempt_token,
        replay(session, turn),
    )
    assert recorded == [{"surface": "intake", "operation_id": operation_id}]
    snapshot = read_turn_status(db_path, "u1", turn)
    assert snapshot.state == "completed"
    assert snapshot.proposal_operations == tuple(recorded)
    completed = inspect_turn(db_path, "u1", turn, session, request_hash(session))
    assert completed.replay_events[1]["data"]["proposal_operations"] == recorded
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'assistant_turn_proposals.v1.%'",
        ).fetchone() == (0,)


def test_proposal_reference_rejects_invalid_or_stale_identity(db_path):
    turn = "invalid-proposal-turn"
    session = "invalid-proposal-session"
    claimed = claim_turn(db_path, "u1", turn, session, request_hash(session))
    with pytest.raises(ValueError, match="UUID"):
        record_proposal_operation(
            db_path, "u1", turn, claimed.attempt_token, "intake", "not-a-uuid",
        )
    with pytest.raises(TurnOwnershipLost):
        record_proposal_operation(
            db_path,
            "u1",
            turn,
            "stale-token",
            "intake",
            "00000000-0000-4000-8000-000000000103",
        )


def test_proposal_reference_and_business_write_share_caller_transaction(db_path):
    turn = "atomic-proposal-turn"
    session = "atomic-proposal-session"
    operation_id = "00000000-0000-4000-8000-000000000104"
    claimed = claim_turn(db_path, "u1", turn, session, request_hash(session))

    with pytest.raises(RuntimeError, match="force rollback"):
        with transaction(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, state, "
                "operation_id) VALUES ('u1', 'correction', 'proposal', "
                "'2026-07-17T12:00:00+00:00', 'awaiting_user', ?)",
                (operation_id,),
            )
            record_proposal_operation_in_transaction(
                conn,
                "u1",
                turn,
                claimed.attempt_token,
                "application_delete",
                operation_id,
            )
            raise RuntimeError("force rollback")

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE operation_id=?",
            (operation_id,),
        ).fetchone() == (0,)
    assert read_turn_status(db_path, "u1", turn).proposal_operations == ()

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, state, "
            "operation_id) VALUES ('u1', 'correction', 'proposal', "
            "'2026-07-17T12:00:00+00:00', 'awaiting_user', ?)",
            (operation_id,),
        )
        record_proposal_operation_in_transaction(
            conn,
            "u1",
            turn,
            claimed.attempt_token,
            "application_delete",
            operation_id,
        )

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE operation_id=?",
            (operation_id,),
        ).fetchone() == (1,)
    assert read_turn_status(db_path, "u1", turn).proposal_operations == ({
        "surface": "application_delete",
        "operation_id": operation_id,
    },)
    assert record_proposal_operation(
        db_path,
        "u1",
        turn,
        claimed.attempt_token,
        "application_delete",
        operation_id,
    ) == {
        "surface": "application_delete",
        "operation_id": operation_id,
    }
    assert len(read_turn_status(db_path, "u1", turn).proposal_operations) == 1


@pytest.mark.parametrize("user_id,client_turn_id", [("", "turn"), ("u1", "")])
def test_read_turn_state_rejects_empty_identity(db_path, user_id, client_turn_id):
    with pytest.raises(ValueError, match="non-empty"):
        read_turn_state(db_path, user_id, client_turn_id)


def test_cancel_if_absent_is_permanent_tenant_scoped_turn_fence(db_path):
    turn = str(uuid4())
    session = "cancelled-session"
    digest = request_hash(session)

    assert cancel_turn_if_absent(db_path, "u1", turn) == "cancelled"
    assert cancel_turn_if_absent(db_path, "u1", turn) == "cancelled"
    assert read_turn_state(db_path, "u1", turn) == "cancelled"
    assert read_turn_state(db_path, "u2", turn) == "absent"

    inspected = inspect_turn(db_path, "u1", turn, session, digest)
    assert inspected.status == "cancelled"
    assert inspected.error == {
        "code": "turn_cancelled",
        "message": "这一轮已被安全取消；请使用新的 client_turn_id 重新发送。",
        "retryable": False,
    }
    assert claim_turn(db_path, "u1", turn, session, digest).status == "cancelled"
    assert claim_turn(db_path, "u1", turn, "other", request_hash("other")).status == (
        "cancelled"
    )
    assert claim_turn(db_path, "u2", turn, session, digest).status == "execute"

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM assistant_turn_cancellations "
            "WHERE user_id='u1' AND client_turn_id=?",
            (turn,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM assistant_turns "
            "WHERE user_id='u1' AND client_turn_id=?",
            (turn,),
        ).fetchone() == (0,)


def test_cancel_if_absent_never_overwrites_an_existing_turn(db_path):
    running_turn = str(uuid4())
    completed_turn = str(uuid4())
    unknown_turn = str(uuid4())

    claim_turn(db_path, "u1", running_turn, "running", request_hash("running"))
    completed = claim_turn(
        db_path, "u1", completed_turn, "completed", request_hash("completed"),
    )
    complete_turn(
        db_path,
        "u1",
        completed_turn,
        completed.attempt_token,
        replay("completed", completed_turn),
    )
    unknown = claim_turn(db_path, "u1", unknown_turn, "unknown", request_hash("unknown"))
    mark_turn_unknown(
        db_path,
        "u1",
        unknown_turn,
        unknown.attempt_token,
        {"code": "turn_outcome_unknown", "message": "未确认", "retryable": False},
    )

    assert cancel_turn_if_absent(db_path, "u1", running_turn) == "running"
    assert cancel_turn_if_absent(db_path, "u1", completed_turn) == "completed"
    assert cancel_turn_if_absent(db_path, "u1", unknown_turn) == "unknown"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM assistant_turn_cancellations WHERE user_id='u1'",
        ).fetchone() == (0,)


def test_cancel_running_turn_replaces_only_its_owner_and_clears_sidecar(db_path):
    turn = str(uuid4())
    operation_id = str(uuid4())
    claimed = claim_turn(db_path, "u1", turn, "session", request_hash("session"))
    record_proposal_operation(
        db_path,
        "u1",
        turn,
        claimed.attempt_token,
        "application_delete",
        operation_id,
    )

    assert cancel_running_turn(db_path, "u1", turn, "wrong-owner") is False
    assert read_turn_state(db_path, "u1", turn) == "running"
    assert cancel_running_turn(db_path, "u1", turn, claimed.attempt_token) is True
    assert read_turn_status(db_path, "u1", turn).state == "cancelled"
    assert read_turn_status(db_path, "u1", turn).proposal_operations == ()
    with pytest.raises(TurnOwnershipLost):
        complete_turn(
            db_path,
            "u1",
            turn,
            claimed.attempt_token,
            replay("session", turn),
        )


@pytest.mark.parametrize("turn", ["not-a-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])
def test_cancel_if_absent_requires_canonical_uuid(db_path, turn):
    with pytest.raises(ValueError, match="UUID"):
        cancel_turn_if_absent(db_path, "u1", turn)


def test_turn_id_reuse_with_different_request_is_conflict(db_path):
    first = request_hash("session-1", "第一条")
    claim_turn(db_path, "u1", "turn-1", "session-1", first)

    different_message = inspect_turn(
        db_path, "u1", "turn-1", "session-1", request_hash("session-1", "第二条"),
    )
    different_session = inspect_turn(
        db_path, "u1", "turn-1", "session-2", request_hash("session-2", "第一条"),
    )
    assert different_message.status == "conflict"
    assert different_session.status == "conflict"
    assert different_message.error["retryable"] is False


def test_same_session_has_one_running_turn_but_tenants_are_independent(db_path):
    first = claim_turn(db_path, "u1", "turn-1", "shared", request_hash("shared", "one"))
    busy = claim_turn(db_path, "u1", "turn-2", "shared", request_hash("shared", "two"))
    other_user = claim_turn(db_path, "u2", "turn-1", "shared", request_hash("shared", "one"))

    assert first.status == "execute"
    assert busy.status == "session_busy" and busy.error["retryable"] is True
    assert other_user.status == "execute"


def test_attempt_token_fences_completion_and_unknown(db_path):
    session = "fenced-session"
    turn = "fenced-turn"
    digest = request_hash(session)
    claimed = claim_turn(db_path, "u1", turn, session, digest)

    with pytest.raises(TurnOwnershipLost):
        complete_turn(db_path, "u1", turn, "stale-token", replay(session, turn))
    assert inspect_turn(db_path, "u1", turn, session, digest).status == "running"
    assert mark_turn_unknown(
        db_path, "u1", turn, "stale-token",
        {"code": "internal", "message": "未确认", "retryable": True},
    ) is False

    assert mark_turn_unknown(
        db_path, "u1", turn, claimed.attempt_token,
        {"code": "turn_outcome_unknown", "message": "未确认", "retryable": True},
    ) is True
    unknown = inspect_turn(db_path, "u1", turn, session, digest)
    assert unknown.status == "unknown"
    assert unknown.error == {
        "code": "turn_outcome_unknown", "message": "未确认", "retryable": False,
    }

    assert claim_turn(db_path, "u1", turn, session, digest).status == "unknown"
    with pytest.raises(TurnOwnershipLost):
        complete_turn(db_path, "u1", turn, claimed.attempt_token, replay(session, turn))


def test_recovery_marks_only_running_turns_unknown_and_is_idempotent(db_path):
    completed = claim_turn(db_path, "u1", "done", "s-done", request_hash("s-done"))
    complete_turn(db_path, "u1", "done", completed.attempt_token, replay("s-done", "done"))
    claim_turn(db_path, "u1", "running-1", "s1", request_hash("s1"))
    claim_turn(db_path, "u2", "running-2", "s2", request_hash("s2"))

    assert recover_interrupted_turns(db_path) == 2
    assert recover_interrupted_turns(db_path) == 0
    assert inspect_turn(
        db_path, "u1", "done", "s-done", request_hash("s-done"),
    ).status == "completed"
    recovered = inspect_turn(db_path, "u1", "running-1", "s1", request_hash("s1"))
    assert recovered.status == "unknown"
    assert recovered.error["code"] == "turn_outcome_unknown"
    assert recovered.error["retryable"] is False


def test_completed_replay_payload_is_evicted_without_losing_completion_semantics(db_path):
    reference = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)

    def finish_completed(turn: str, session: str, text: str = "完成") -> None:
        claimed = claim_turn(db_path, "u1", turn, session, request_hash(session, turn))
        complete_turn(db_path, "u1", turn, claimed.attempt_token, replay(session, turn, text))

    finish_completed("old-completed", "old-session", "PRIVATE-REPLY-BODY")
    finish_completed("at-cutoff", "cutoff-session")
    finish_completed("recent-completed", "recent-session")
    unknown = claim_turn(
        db_path, "u1", "old-unknown", "unknown-session",
        request_hash("unknown-session", "old-unknown"),
    )
    assert mark_turn_unknown(
        db_path, "u1", "old-unknown", unknown.attempt_token,
        {"code": "turn_outcome_unknown", "message": "旧错误正文", "retryable": False},
    )
    claim_turn(db_path, "u1", "still-running", "running-session", request_hash("running-session"))

    aged = {
        "old-completed": reference - timedelta(days=31),
        "old-unknown": reference - timedelta(days=90),
        "at-cutoff": reference - timedelta(days=30),
        "recent-completed": reference - timedelta(days=29),
    }
    with transaction(db_path) as conn:
        for turn, finished in aged.items():
            conn.execute(
                "UPDATE assistant_turns SET finished_time=?, updated_time=? "
                "WHERE user_id='u1' AND client_turn_id=?",
                (finished.isoformat(), finished.isoformat(), turn),
            )

    assert evict_expired_completed_replays(db_path, reference_time=reference) == 1
    assert evict_expired_completed_replays(db_path, reference_time=reference) == 0

    with read_connection(db_path) as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT client_turn_id, state, replay_events_json, unknown_error_json, "
                "replay_evicted_time, finished_time, updated_time "
                "FROM assistant_turns WHERE user_id='u1'"
            )
        }
    old_replay = json.loads(rows["old-completed"][1])
    assert rows["old-completed"][0] == "completed"
    assert "超过 30 天的原回复内容已清理" in old_replay[0]["data"]["text"]
    assert "PRIVATE-REPLY-BODY" not in rows["old-completed"][1]
    assert old_replay[1] == replay("old-session", "old-completed")[1]
    assert rows["old-completed"][3] == reference.isoformat()
    assert rows["old-completed"][4:] == (
        aged["old-completed"].isoformat(), aged["old-completed"].isoformat(),
    )
    assert rows["old-unknown"][:4] == (
        "unknown", None,
        '{"code":"turn_outcome_unknown","message":"旧错误正文","retryable":false}',
        None,
    )
    assert rows["at-cutoff"][0] == "completed"
    assert rows["recent-completed"][0] == "completed"
    assert rows["still-running"][0] == "running"

    digest = request_hash("old-session", "old-completed")
    retained = inspect_turn(db_path, "u1", "old-completed", "old-session", digest)
    assert retained.status == "completed"
    assert retained.replay_events == old_replay
    assert retained.replay_events[1]["data"]["attachments"] == "consumed"
    assert claim_turn(db_path, "u1", "old-completed", "old-session", digest).status == "completed"
    assert claim_turn(
        db_path, "u1", "old-completed", "old-session",
        request_hash("old-session", "different"),
    ).status == "conflict"
    assert claim_turn(
        db_path, "u1", "new-turn", "old-session", request_hash("old-session", "new"),
    ).status == "execute"


def test_turn_replay_eviction_rejects_ambiguous_policy_inputs(db_path):
    with pytest.raises(ValueError, match="positive integer"):
        evict_expired_completed_replays(db_path, retention_days=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        evict_expired_completed_replays(
            db_path, reference_time=datetime(2026, 7, 13),
        )


def test_concurrent_same_turn_has_exactly_one_owner(db_path):
    session = "concurrent-session"
    turn = "same-turn"
    digest = request_hash(session)

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(
            lambda _: claim_turn(db_path, "u1", turn, session, digest),
            range(32),
        ))

    assert [decision.status for decision in decisions].count("execute") == 1
    assert [decision.status for decision in decisions].count("running") == 31


def test_concurrent_cancel_and_claim_have_one_permanent_winner(db_path):
    for index in range(16):
        turn = str(uuid4())
        session = f"cancel-race-{index}"
        digest = request_hash(session)
        barrier = Barrier(2)

        def cancel():
            barrier.wait()
            return cancel_turn_if_absent(db_path, "u1", turn)

        def claim():
            barrier.wait()
            return claim_turn(db_path, "u1", turn, session, digest).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_future = pool.submit(cancel)
            claim_future = pool.submit(claim)
            outcome = (cancel_future.result(), claim_future.result())

        assert outcome in {("cancelled", "cancelled"), ("running", "execute")}
        with read_connection(db_path) as conn:
            turn_count = conn.execute(
                "SELECT COUNT(*) FROM assistant_turns "
                "WHERE user_id='u1' AND client_turn_id=?",
                (turn,),
            ).fetchone()[0]
            cancellation_count = conn.execute(
                "SELECT COUNT(*) FROM assistant_turn_cancellations "
                "WHERE user_id='u1' AND client_turn_id=?",
                (turn,),
            ).fetchone()[0]
        assert turn_count + cancellation_count == 1
        if cancellation_count:
            assert claim_turn(db_path, "u1", turn, session, digest).status == "cancelled"


def test_concurrent_distinct_turns_in_one_session_have_one_owner(db_path):
    session = "one-at-a-time"

    def claim(index: int):
        turn = f"turn-{index}"
        return claim_turn(db_path, "u1", turn, session, request_hash(session, turn))

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(claim, range(32)))

    assert [decision.status for decision in decisions].count("execute") == 1
    assert [decision.status for decision in decisions].count("session_busy") == 31
    with read_connection(db_path) as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM assistant_turns WHERE user_id='u1' AND state='running'"
        ).fetchone()[0]
    assert running == 1


def test_request_hash_is_canonical_but_attachment_order_is_significant():
    left = chat_request_hash("s1", "m", [{"kind": "document", "text": "A", "filename": "a"}])
    keys_reordered = chat_request_hash(
        "s1", "m", [{"filename": "a", "text": "A", "kind": "document"}],
    )
    attachment_order = chat_request_hash("s1", "m", [
        {"kind": "document", "text": "B", "filename": "b"},
        {"kind": "document", "text": "A", "filename": "a"},
    ])
    reversed_order = chat_request_hash("s1", "m", [
        {"kind": "document", "text": "A", "filename": "a"},
        {"kind": "document", "text": "B", "filename": "b"},
    ])
    assert left == keys_reordered
    assert attachment_order != reversed_order
    assert left != chat_request_hash("s2", "m", [{"kind": "document", "text": "A", "filename": "a"}])


def test_request_hash_binds_optional_review_supplement_reference(db_path):
    first_reference = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    second_reference = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
    baseline = chat_request_hash("session", "补充回答", [])
    explicit_none = chat_request_hash(
        "session",
        "补充回答",
        [],
        review_supplement_reference=None,
    )
    first = chat_request_hash(
        "session",
        "补充回答",
        [],
        review_supplement_reference=first_reference,
    )
    second = chat_request_hash(
        "session",
        "补充回答",
        [],
        review_supplement_reference=second_reference,
    )

    assert explicit_none == baseline
    assert len({baseline, first, second}) == 3
    claim_turn(db_path, "u1", "turn", "session", first)
    assert inspect_turn(db_path, "u1", "turn", "session", first).status == "running"
    assert inspect_turn(db_path, "u1", "turn", "session", baseline).status == "conflict"
    assert inspect_turn(db_path, "u1", "turn", "session", second).status == "conflict"

    with pytest.raises(ValueError, match="canonical UUID"):
        chat_request_hash(
            "session",
            "补充回答",
            [],
            review_supplement_reference=first_reference.upper(),
        )


def test_request_hash_binds_frozen_output_locale():
    chinese = chat_request_hash("session", "hello", [], output_locale="zh-CN")
    english = chat_request_hash("session", "hello", [], output_locale="en")

    assert chinese != english
    assert chinese == chat_request_hash("session", "hello", [])


@pytest.mark.parametrize("events", [
    [],
    [{"event": "done", "data": {}}],
    [
        {"event": "tool_status", "data": {"label": "落盘中"}},
        {"event": "done", "data": {}},
    ],
    [
        {"event": "message_snapshot", "data": {"text": "完成"}},
        {"event": "done", "data": {
            "session": "session", "message_id": "message", "client_turn_id": "turn",
            "replayed": True,
        }},
    ],
])
def test_completed_cache_accepts_only_snapshot_then_done(db_path, events):
    claimed = claim_turn(db_path, "u1", "turn", "session", request_hash("session"))
    with pytest.raises(ValueError, match="replay"):
        complete_turn(db_path, "u1", "turn", claimed.attempt_token, events)
    assert inspect_turn(
        db_path, "u1", "turn", "session", request_hash("session"),
    ).status == "running"
