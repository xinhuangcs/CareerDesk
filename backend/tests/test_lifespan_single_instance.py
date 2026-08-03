
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.orchestration.assistant.ledger import chat_request_hash, claim_turn, complete_turn
from careerdesk.platform.runtime import (InstanceAlreadyRunningError, InstanceLockError,
                                        acquire_instance_lock)


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("APP_RUNTIME_MODE", "test")
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    yield data_dir
    get_settings.cache_clear()


def _turn_state(db_path: str, user_id: str, turn_id: str) -> str | None:
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT state FROM assistant_turns WHERE user_id=? AND client_turn_id=?",
            (user_id, turn_id),
        ).fetchone()
    return None if row is None else row[0]


def test_second_app_cannot_recover_live_turn_but_later_restart_can(isolated_runtime):
    from careerdesk.bootstrap.app import create_app

    settings = get_settings()
    user_id = "me"
    session_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    request_hash = chat_request_hash(session_id, "仍在执行", [])
    first_app = create_app()

    with TestClient(first_app):
        claimed = claim_turn(
            settings.db_path, user_id, turn_id, session_id, request_hash,
        )
        assert claimed.status == "execute"
        assert _turn_state(settings.db_path, user_id, turn_id) == "running"

        with pytest.raises(InstanceAlreadyRunningError):
            with TestClient(create_app()):
                pytest.fail("第二个应用不应进入已启动状态")

        assert _turn_state(settings.db_path, user_id, turn_id) == "running"

    assert _turn_state(settings.db_path, user_id, turn_id) == "running"
    with TestClient(create_app()):
        assert _turn_state(settings.db_path, user_id, turn_id) == "unknown"


def test_startup_exception_releases_acquired_lock(isolated_runtime, monkeypatch):
    from careerdesk.bootstrap import lifespan as lifespan_module
    from careerdesk.bootstrap.app import create_app

    original_init_db = lifespan_module.init_db

    def fail_startup(_db_path: str) -> None:
        raise RuntimeError("simulated init failure")

    monkeypatch.setattr(lifespan_module, "init_db", fail_startup)
    with pytest.raises(RuntimeError, match="simulated init failure"):
        with TestClient(create_app()):
            pytest.fail("初始化失败的应用不应进入已启动状态")

    monkeypatch.setattr(lifespan_module, "init_db", original_init_db)
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200


def test_startup_evicts_old_completed_reply_but_preserves_done_semantics(isolated_runtime):
    from careerdesk.bootstrap.app import create_app

    settings = get_settings()
    init_db(settings.db_path)
    session_id = "old-session"
    turn_id = "old-turn"
    digest = chat_request_hash(session_id, "旧请求", [])
    claimed = claim_turn(settings.db_path, "me", turn_id, session_id, digest)
    complete_turn(settings.db_path, "me", turn_id, claimed.attempt_token, [
        {"event": "message_snapshot", "data": {"text": "PRIVATE-OLD-REPLY"}},
        {"event": "done", "data": {
            "session": session_id,
            "message_id": "message-1",
            "client_turn_id": turn_id,
            "history_committed": True,
            "attachments": "consumed",
            "replayed": True,
        }},
    ])
    aged = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with transaction(settings.db_path) as conn:
        conn.execute(
            "UPDATE assistant_turns SET finished_time=?, updated_time=? "
            "WHERE user_id='me' AND client_turn_id=?",
            (aged, aged, turn_id),
        )

    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200

    with read_connection(settings.db_path) as conn:
        state, encoded, evicted = conn.execute(
            "SELECT state, replay_events_json, replay_evicted_time "
            "FROM assistant_turns WHERE user_id='me' AND client_turn_id=?",
            (turn_id,),
        ).fetchone()
    events = json.loads(encoded)
    assert state == "completed" and evicted is not None
    assert "PRIVATE-OLD-REPLY" not in encoded
    assert "超过 30 天的原回复内容已清理" in events[0]["data"]["text"]
    assert events[1]["data"]["attachments"] == "consumed"
    assert events[1]["data"]["history_committed"] is True


def test_preacquired_lock_is_transferred_without_reacquire_and_released(
    isolated_runtime, monkeypatch,
):
    from careerdesk.bootstrap import lifespan as lifespan_module
    from careerdesk.bootstrap.app import create_app

    lock = acquire_instance_lock(isolated_runtime, entrypoint="launcher")
    app = create_app(instance_lock=lock)

    def unexpected_reacquire(*_args, **_kwargs):
        raise AssertionError("lifespan 不得重复获取 launcher 已转交的锁")

    monkeypatch.setattr(lifespan_module, "acquire_instance_lock", unexpected_reacquire)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.instance_lock is lock
        assert lock.released is False

    assert lock.released is True
    assert not hasattr(app.state, "instance_lock")
    with acquire_instance_lock(isolated_runtime, entrypoint="after-shutdown"):
        pass


def test_released_or_wrong_root_transferred_lock_is_rejected_fail_closed(
    isolated_runtime, tmp_path,
):
    from careerdesk.bootstrap.app import create_app

    released = acquire_instance_lock(isolated_runtime, entrypoint="released")
    released.release()
    with pytest.raises(InstanceLockError, match="交付的实例锁无效"):
        with TestClient(create_app(instance_lock=released)):
            pytest.fail("已释放的锁不得跳过 lifespan acquire")

    other_root = tmp_path / "other-data"
    wrong = acquire_instance_lock(other_root, entrypoint="wrong-root")
    try:
        with pytest.raises(InstanceLockError, match="不属于当前数据目录"):
            with TestClient(create_app(instance_lock=wrong)):
                pytest.fail("其他数据根的锁不得被当作当前 owner")
        assert wrong.released is False
        with pytest.raises(InstanceAlreadyRunningError):
            acquire_instance_lock(other_root, entrypoint="still-held")
    finally:
        wrong.release()
