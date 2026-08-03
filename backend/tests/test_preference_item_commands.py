
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.features.preferences.item_commands import (
    PreferenceItemCommandConflict,
    cancel_preference_item_command_if_absent,
    canonical_cancel_command,
    canonical_item_command,
    execute_preference_item_command,
    get_preference_item_command_status,
    get_preference_item_operation,
)
from careerdesk.features.preferences.operations import execute_preference_update_operation
from careerdesk.features.preferences.repository import (
    PreferenceProjectionConflict,
    list_current_preferences,
    list_preferences_for_settings,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "preferences.db")
    init_db(path)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client, str(tmp_path / "careerdesk.db")
    get_settings.cache_clear()


def seed(db_path: str, *, user_id: str = "u1", value: str = "旧值") -> dict:
    operation = execute_preference_update_operation(
        db_path,
        user_id,
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        changes=[{"op": "set", "key": "城市", "value": value}],
    )
    return {
        "id": operation["effects"][0]["final_id"],
        "revision": operation["effects"][0]["final_revision"],
    }


def scalar(db_path: str, sql: str, *parameters):
    with read_connection(db_path) as conn:
        return conn.execute(sql, parameters).fetchone()[0]


def chain_text(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    parts = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(repr(current))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(parts)


def test_update_is_atomic_value_free_and_replays_first_value(db_path):
    target = seed(db_path)
    command_id = uuid4()
    command = canonical_item_command({
        "action": "set", "target": target, "value": "首笔值",
    })
    status, created = execute_preference_item_command(
        db_path, "u1", command_id, command,
    )

    assert created is True
    assert status["state"] == "completed"
    assert status["result"] == {
        "outcome": "updated",
        "before": target,
        "final": {"id": target["id"], "revision": 2},
    }
    assert status["operation_id"] is not None
    assert list_current_preferences(db_path, "u1")["items"][0]["value"] == "首笔值"
    operation = get_preference_item_operation(
        db_path, "u1", status["operation_id"],
    )
    assert operation["command_id"] == str(command_id)
    assert operation["current"] is True
    assert operation["key"] == "城市"

    replay, replay_created = execute_preference_item_command(
        db_path,
        "u1",
        command_id,
        canonical_item_command({
            "action": "set", "target": target, "value": "不得覆盖的第二值",
        }),
    )
    assert replay_created is False and replay == status
    assert list_current_preferences(db_path, "u1")["items"][0]["value"] == "首笔值"
    assert scalar(db_path, "SELECT COUNT(*) FROM preference_item_commands") == 1
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE operation_id=?",
        status["operation_id"],
    ) == 1
    with read_connection(db_path) as conn:
        stored = repr(conn.execute(
            "SELECT * FROM preference_item_commands",
        ).fetchall()) + repr(conn.execute(
            "SELECT extraction_json, derivation_json, content FROM journal "
            "WHERE operation_id=?",
            (status["operation_id"],),
        ).fetchall())
    assert "首笔值" not in stored and "第二值" not in stored


def test_no_change_only_writes_terminal_command_and_first_wins(db_path):
    target = seed(db_path, value="相同值")
    command_id = uuid4()
    before_journal = scalar(db_path, "SELECT COUNT(*) FROM journal")
    before_revision = scalar(
        db_path, "SELECT revision FROM preferences WHERE id=?", target["id"],
    )
    before_updated = scalar(
        db_path, "SELECT updated_time FROM preferences WHERE id=?", target["id"],
    )
    status, created = execute_preference_item_command(
        db_path,
        "u1",
        command_id,
        canonical_item_command({
            "action": "set", "target": target, "value": "相同值",
        }),
    )
    assert created is True
    assert status["result"]["outcome"] == "no_change"
    assert status["operation_id"] is None
    assert scalar(db_path, "SELECT COUNT(*) FROM journal") == before_journal
    assert scalar(
        db_path, "SELECT revision FROM preferences WHERE id=?", target["id"],
    ) == before_revision
    assert scalar(
        db_path, "SELECT updated_time FROM preferences WHERE id=?", target["id"],
    ) == before_updated

    replay, _created = execute_preference_item_command(
        db_path,
        "u1",
        command_id,
        canonical_item_command({
            "action": "set", "target": target, "value": "不同值",
        }),
    )
    assert replay == status
    assert list_current_preferences(db_path, "u1")["items"][0]["value"] == "相同值"


def test_delete_keeps_owner_tombstone_and_blocks_id_aba(db_path):
    target = seed(db_path)
    status, _created = execute_preference_item_command(
        db_path,
        "u1",
        uuid4(),
        canonical_item_command({"action": "delete", "target": target}),
    )
    assert status["result"]["outcome"] == "deleted"
    assert list_current_preferences(db_path, "u1")["items"] == []
    assert scalar(
        db_path, "SELECT COUNT(*) FROM preference_owners WHERE preference_id=?",
        target["id"],
    ) == 1
    with pytest.raises(
        sqlite3.IntegrityError,
        match=r"UNIQUE constraint failed: preference_owners\.preference_id",
    ):
        with transaction(db_path) as conn:
            conn.execute(
                "INSERT INTO preferences "
                "(id,user_id,key,value,revision,created_time,updated_time) "
                "VALUES (?, 'u1', '重放', '值', 1, ?, ?)",
                (target["id"], status["finished_time"], status["finished_time"]),
            )


def test_rejected_and_cancelled_are_terminal_without_business_writes(db_path):
    target = seed(db_path)
    stale_id = uuid4()
    stale, _created = execute_preference_item_command(
        db_path,
        "u1",
        stale_id,
        canonical_item_command({
            "action": "set",
            "target": {**target, "revision": target["revision"] + 1},
            "value": "不写入",
        }),
    )
    assert stale["state"] == "rejected"
    assert stale["error"]["code"] == "target_changed"
    assert stale["operation_id"] is None

    cancelled_id = uuid4()
    cancel_command = canonical_cancel_command({"action": "delete", "target": target})
    cancelled, created = cancel_preference_item_command_if_absent(
        db_path, "u1", cancelled_id, cancel_command,
    )
    assert created is True and cancelled["state"] == "cancelled"
    late, late_created = execute_preference_item_command(
        db_path,
        "u1",
        cancelled_id,
        canonical_item_command({"action": "delete", "target": target}),
    )
    assert late_created is False and late == cancelled
    assert list_current_preferences(db_path, "u1")["items"][0]["value"] == "旧值"


def test_cross_tenant_target_and_command_are_indistinguishable_from_missing(db_path):
    target = seed(db_path, user_id="u1")
    command_id = uuid4()
    status, _created = execute_preference_item_command(
        db_path,
        "u2",
        command_id,
        canonical_item_command({
            "action": "set", "target": target, "value": "不得读取",
        }),
    )
    assert status["state"] == "rejected"
    assert status["error"]["code"] == "target_missing"
    assert get_preference_item_command_status(db_path, "u1", command_id) is None
    assert get_preference_item_operation(db_path, "u2", str(uuid4())) is None


def test_owner_misbinding_blocks_both_tenants(db_path):
    target = seed(db_path, user_id="u1")
    with transaction(db_path) as conn:
        conn.execute("DROP TRIGGER trg_preferences_update_contract")
        conn.execute(
            "UPDATE preferences SET user_id='u2' WHERE id=?",
            (target["id"],),
        )
    for user_id in ("u1", "u2"):
        with pytest.raises(PreferenceProjectionConflict, match="owner"):
            list_current_preferences(db_path, user_id)


def test_concurrent_distinct_commands_on_one_revision_have_one_mutation(db_path):
    target = seed(db_path)

    def apply(value: str):
        return execute_preference_item_command(
            db_path,
            "u1",
            uuid4(),
            canonical_item_command({
                "action": "set", "target": target, "value": value,
            }),
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, ["A", "B"]))
    assert sorted(item["state"] for item in results) == ["completed", "rejected"]
    assert next(item for item in results if item["state"] == "rejected")["error"][
        "code"
    ] == "target_changed"
    assert scalar(
        db_path, "SELECT revision FROM preferences WHERE id=?", target["id"],
    ) == 2
    assert scalar(db_path, "SELECT COUNT(*) FROM preference_item_commands") == 2


def test_concurrent_same_command_and_cancel_race_are_first_terminal_wins(db_path):
    target = seed(db_path)
    same_id = uuid4()

    def same_command(value: str):
        return execute_preference_item_command(
            db_path,
            "u1",
            same_id,
            canonical_item_command({
                "action": "set", "target": target, "value": value,
            }),
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_results = list(pool.map(same_command, ["先手A", "先手B"]))
    assert same_results[0] == same_results[1]
    assert scalar(db_path, "SELECT COUNT(*) FROM preference_item_commands") == 1
    assert scalar(db_path, "SELECT revision FROM preferences") == 2

    current = {"id": target["id"], "revision": 2}
    race_id = uuid4()

    def apply_or_cancel(kind: str):
        if kind == "apply":
            return execute_preference_item_command(
                db_path,
                "u1",
                race_id,
                canonical_item_command({
                    "action": "set", "target": current, "value": "竞态值",
                }),
            )[0]
        return cancel_preference_item_command_if_absent(
            db_path,
            "u1",
            race_id,
            canonical_cancel_command({"action": "set", "target": current}),
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        race_results = list(pool.map(apply_or_cancel, ["apply", "cancel"]))
    assert race_results[0] == race_results[1]
    assert race_results[0]["state"] in {"completed", "cancelled"}
    expected_revision = 3 if race_results[0]["state"] == "completed" else 2
    assert scalar(db_path, "SELECT revision FROM preferences") == expected_revision


def test_linked_journal_damage_is_conflict_not_404(db_path):
    target = seed(db_path)
    status, _created = execute_preference_item_command(
        db_path,
        "u1",
        uuid4(),
        canonical_item_command({
            "action": "set", "target": target, "value": "新值",
        }),
    )
    with transaction(db_path) as conn:
        conn.execute("DROP TRIGGER trg_preference_item_command_journal_immutable_update")
        conn.execute(
            "UPDATE journal SET user_id='other' WHERE operation_id=?",
            (status["operation_id"],),
        )
    with pytest.raises(PreferenceItemCommandConflict):
        get_preference_item_operation(db_path, "u1", status["operation_id"])
    with pytest.raises(PreferenceItemCommandConflict):
        get_preference_item_command_status(db_path, "u1", status["command_id"])


@pytest.mark.parametrize("terminal", ["no_change", "cancelled", "rejected"])
def test_terminal_command_id_damage_cannot_turn_old_id_absent_or_write(
    db_path,
    terminal,
):
    target = seed(db_path, value="原值")
    command_id = uuid4()
    if terminal == "no_change":
        status, _created = execute_preference_item_command(
            db_path,
            "u1",
            command_id,
            canonical_item_command({
                "action": "set", "target": target, "value": "原值",
            }),
        )
        replay_payload = {
            "action": "set", "target": target, "value": "不得写入的新值",
        }
    elif terminal == "cancelled":
        status, _created = cancel_preference_item_command_if_absent(
            db_path,
            "u1",
            command_id,
            canonical_cancel_command({"action": "delete", "target": target}),
        )
        replay_payload = {"action": "delete", "target": target}
    else:
        stale = {**target, "revision": target["revision"] + 1}
        status, _created = execute_preference_item_command(
            db_path,
            "u1",
            command_id,
            canonical_item_command({
                "action": "set", "target": stale, "value": "不得写入的新值",
            }),
        )
        replay_payload = {
            "action": "set", "target": stale, "value": "不得写入的新值",
        }
    replacement = str(uuid4())
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_preference_item_command_immutable_update")
        conn.execute(
            "UPDATE preference_item_commands SET command_id=? "
            "WHERE user_id='u1' AND command_id=?",
            (replacement, str(command_id)),
        )

    with pytest.raises(PreferenceItemCommandConflict, match="owner/terminal"):
        get_preference_item_command_status(db_path, "u1", command_id)
    with pytest.raises(PreferenceItemCommandConflict, match="owner/terminal"):
        execute_preference_item_command(
            db_path, "u1", command_id, canonical_item_command(replay_payload),
        )
    with pytest.raises(PreferenceItemCommandConflict, match="owner/terminal"):
        get_preference_item_command_status(db_path, "u1", replacement)
    current = list_current_preferences(db_path, "u1")["items"][0]
    assert current["value"] == "原值" and current["revision"] == 1
    assert status["state"] in {"completed", "cancelled", "rejected"}


def test_terminal_user_damage_blocks_old_and_new_tenant(db_path):
    target = seed(db_path)
    command_id = uuid4()
    cancel_preference_item_command_if_absent(
        db_path,
        "u1",
        command_id,
        canonical_cancel_command({"action": "delete", "target": target}),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_preference_item_command_immutable_update")
        conn.execute(
            "UPDATE preference_item_commands SET user_id='u2' "
            "WHERE user_id='u1' AND command_id=?",
            (str(command_id),),
        )
    for user_id in ("u1", "u2"):
        with pytest.raises(PreferenceItemCommandConflict, match="owner/terminal"):
            get_preference_item_command_status(db_path, user_id, command_id)


def test_settings_snapshot_has_id_and_stable_scope_without_changing_agent_dto(db_path):
    target = seed(db_path)
    settings = list_preferences_for_settings(
        db_path, "u1", recovery_scope="a" * 64,
    )
    assert settings["items"][0]["id"] == target["id"]
    assert settings["recovery_scope"] == "a" * 64
    assert "id" not in list_current_preferences(db_path, "u1")["items"][0]


def test_http_contract_recovery_and_malformed_json_are_value_private(client):
    test_client, db_path = client
    target = seed(db_path, user_id="me")
    command_id = uuid4()
    response = test_client.put(
        f"/api/preferences/item-commands/{command_id}",
        json={"action": "set", "target": target, "value": "HTTP新值"},
    )
    assert response.status_code == 201
    status = response.json()
    assert test_client.get(
        f"/api/preferences/item-commands/{command_id}",
    ).json() == status
    assert test_client.get("/api/preferences").json()["items"][0]["id"] == target["id"]
    assert len(test_client.get("/api/preferences").json()["recovery_scope"]) == 64
    assert test_client.get(
        f"/api/preferences/item-commands/{command_id}",
        headers={"Remote-User": "other"},
    ).status_code == 404

    sentinel = "PRIVATE-MALFORMED-JSON-SENTINEL"
    invalid = test_client.put(
        f"/api/preferences/item-commands/{uuid4()}",
        content=b'{"action":"set","value":"PRIVATE-MALFORMED-JSON-SENTINEL"',
        headers={"content-type": "application/json"},
    )
    assert invalid.status_code == 422
    assert sentinel not in invalid.text

    secret = "PRIVATE-VALIDATION-SENTINEL-" + "x" * 2_100
    with pytest.raises(ValueError) as captured:
        canonical_item_command({
            "action": "set", "target": target, "value": secret,
        })
    assert secret not in chain_text(captured.value)

    with pytest.raises(ValueError, match="参数无效"):
        canonical_item_command({
            "action": "set",
            "target": {"id": 9_007_199_254_740_992, "revision": 1},
            "value": "不会进入 SQLite bind",
        })
