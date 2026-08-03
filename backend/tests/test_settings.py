
import os
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from careerdesk.core import config as config_module
from careerdesk.core.config import get_settings
from careerdesk.features.settings import service as settings_service
from careerdesk.features.settings.service import ALLOWED_KEY_VARS
from careerdesk.platform import credentials

POLICY_ENV_VARS = (
    "APP_STRICT_OFFLINE",
    "APP_ALLOW_CONVERSATION_EMBEDDING",
    "APP_ALLOW_WEB_RESEARCH",
)
CAPABILITY_ENV_VARS = (
    "APP_LLM_CONTEXT_WINDOW",
    "APP_LLM_MAX_OUTPUT_TOKENS",
)
DEFAULT_POLICY = {
    "strict_offline": False,
    "allow_conversation_embedding": False,
    "allow_web_research": False,
    "allow_deep_research": False,
    "allow_ddg_fallback": True,
}
CUSTOM_MODEL_CAPABILITIES = {
    "context_window": 131_072,
    "max_output_tokens": 8_192,
}


class _TestCredentialStore:
    """In-memory installed-store double; never touches the host keychain."""

    label = "Test OS Credential Store"

    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})
        self.fail_set_once: str | None = None

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        if name == self.fail_set_once:
            self.fail_set_once = None
            raise credentials.CredentialStoreUnavailable("safe test failure")
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def _use_test_system_store(monkeypatch, store: _TestCredentialStore) -> None:
    status = credentials.CredentialStoreStatus(
        kind="system",
        available=True,
        label=store.label,
        issue=None,
    )
    monkeypatch.setattr(settings_service, "_uses_system_credential_store", lambda _settings=None: True)
    monkeypatch.setattr(settings_service, "_open_system_credential_store", lambda: store)
    monkeypatch.setattr(credentials, "current_system_status", lambda: status)
    monkeypatch.setattr(credentials, "available_status", lambda _store: status)


def policy(**overrides) -> dict[str, bool]:
    return {**DEFAULT_POLICY, **overrides}


def put_settings(client, payload: dict, *, revision: str | None = None, headers=None):
    if revision is None:
        state = client.get("/api/settings", headers=headers)
        assert state.status_code == 200
        revision = state.json()["revision"]
    return client.put(
        "/api/settings",
        json={**payload, "revision": revision},
        headers=headers,
    )


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr("careerdesk.core.config._ENV_FILE", path)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch, env_file):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Shanghai")
    for name in CAPABILITY_ENV_VARS:
        # Empty values both override the developer's real .env and let
        # monkeypatch restore absence after settings_service writes os.environ.
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("APP_DEBUG", "true")
    monkeypatch.delenv("APP_GATEWAY_AUTH_SECRET", raising=False)
    for name in POLICY_ENV_VARS:
        monkeypatch.setenv(name, "false")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    for name in ALLOWED_KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_system_timezone_sync_persists_and_applies_immediately(client, env_file):
    before = client.get("/api/settings").json()["revision"]

    response = client.post(
        "/api/settings/system-timezone",
        json={"timezone": "Europe/Copenhagen"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "updated",
        "timezone": "Europe/Copenhagen",
    }
    assert "APP_TIMEZONE='Europe/Copenhagen'" in env_file.read_text(encoding="utf-8")
    assert get_settings().timezone == "Europe/Copenhagen"
    assert client.get("/api/settings").json()["revision"] != before

    unchanged_revision = client.get("/api/settings").json()["revision"]
    unchanged = client.post(
        "/api/settings/system-timezone",
        json={"timezone": "Europe/Copenhagen"},
    )
    assert unchanged.json() == {
        "status": "unchanged",
        "timezone": "Europe/Copenhagen",
    }
    assert client.get("/api/settings").json()["revision"] == unchanged_revision


def test_system_timezone_sync_rejects_unknown_or_malformed_zones(client, env_file):
    for value, expected_status in (
        ("Mars/Olympus", 400),
        ("", 422),
        ("Europe/Copenhagen\nAPP_DEBUG=false", 400),
    ):
        response = client.post(
            "/api/settings/system-timezone",
            json={"timezone": value},
        )
        assert response.status_code == expected_status
    assert not env_file.exists()
    assert get_settings().timezone == "Asia/Shanghai"


def test_system_timezone_sync_respects_explicit_environment_override(
    client,
    env_file,
    monkeypatch,
):
    monkeypatch.setattr(
        config_module,
        "_DOTENV_PRELOAD_ENV_KEYS",
        frozenset({"APP_TIMEZONE"}),
    )

    response = client.post(
        "/api/settings/system-timezone",
        json={"timezone": "America/New_York"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "external_override",
        "timezone": "Asia/Shanghai",
    }
    assert not env_file.exists()


def test_system_timezone_sync_is_closed_in_server_managed_mode(client, monkeypatch):
    monkeypatch.setattr(settings_service, "ui_editable", lambda: False)

    response = client.post(
        "/api/settings/system-timezone",
        json={"timezone": "Europe/Copenhagen"},
    )

    assert response.status_code == 403


def test_state_shape(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["editable"] is True
    assert body["llm_model"] is None
    assert body["llm_model_local"] is None
    assert body["llm_capabilities"] == {
        "context_window": None,
        "max_output_tokens": None,
        "source": None,
    }
    assert isinstance(body["revision"], str) and len(body["revision"]) >= 16
    assert body["persistence_warning"] is None
    assert body["credential_storage"] == {
        "kind": "configuration_file",
        "available": True,
        "label": "私有配置文件",
        "issue": None,
    }
    assert body["environment_managed"]["llm_model"] is False
    assert body["environment_managed"]["llm_capabilities"] == {
        "context_window": False,
        "max_output_tokens": False,
    }
    assert all(value is False for value in body["environment_managed"]["keys"].values())
    assert all(value is False for value in body["environment_managed"]["outbound_policy"].values())
    assert body["openai_compatible_endpoint"] == {
        "status": "missing",
        "url": None,
        "source": None,
        "externally_managed": False,
        "issue": None,
    }
    assert body["outbound_policy"] == DEFAULT_POLICY
    assert set(body["keys"]) == set(ALLOWED_KEY_VARS)
    assert "SERPAPI_API_KEY" not in body["keys"]
    assert {"BRAVE_API_KEY", "GOOGLE_PSE_API_KEY", "SEARXNG_BASE_URL"} <= set(body["keys"])
    assert all(configured is False for configured in body["keys"].values())
    providers = {p["name"]: p for p in body["providers"]}
    assert providers["anthropic"]["default_model"] and providers["anthropic"]["key_vars"] == ["ANTHROPIC_API_KEY"]
    assert providers["ollama"]["local"] is True and providers["ollama"]["key_vars"] == []
    assert providers["gemini"]["key_vars"] == ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
    assert providers["openai_compatible"]["default_model"] is None
    assert providers["anthropic"]["context_window"]
    assert providers["anthropic"]["max_output_tokens"]
    assert providers["ollama"]["context_window"] is None
    assert "gemini_openai" not in providers and "gemini" in providers


def test_storage_disclosure_is_claimed_once_per_data_root(client):
    first = client.post("/api/settings/storage-disclosure/claim", json={})
    second = client.post("/api/settings/storage-disclosure/claim", json={})

    assert first.status_code == 200
    assert first.json() == {"should_show": True}
    assert second.status_code == 200
    assert second.json() == {"should_show": False}
    with sqlite3.connect(get_settings().db_path) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='ui.storage_disclosure_shown.v1'"
        ).fetchone() == ("shown",)


def test_clear_conversation_history_removes_truth_and_all_local_indexes(client, monkeypatch):
    import agentmaker
    from agentmaker import Message, Scope
    from agentmaker.testing import FakeEmbedder
    from careerdesk.agentic.memory import build_conversation_memory
    from careerdesk.platform.database import derived_db_path

    settings = get_settings()
    user_id = settings.dev_fake_user
    closers = []
    conversation, _ = build_conversation_memory(
        settings.db_path,
        embedding_enabled=False,
        user_id=user_id,
        resource_closers=closers,
    )
    conversation.append_many(
        [
            Message(role="user", content="我承诺周五完成简历"),
            Message(role="assistant", content="已记下这个约定"),
        ],
        scope=Scope(user=user_id, app="careerdesk", session="history-to-delete"),
    )
    for close in reversed(closers):
        close()

    monkeypatch.setenv("OPENAI_API_KEY", "hermetic-history-key")
    monkeypatch.setattr(agentmaker, "OpenAIEmbedder", lambda **_kwargs: FakeEmbedder())
    semantic_closers = []
    build_conversation_memory(
        settings.db_path,
        embedding_enabled=True,
        user_id=user_id,
        resource_closers=semantic_closers,
    )
    for close in reversed(semantic_closers):
        close()

    response = client.post("/api/settings/conversation-history/clear")
    assert response.status_code == 200
    assert response.json() == {"status": "completed", "deleted_messages": 2}
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM session_messages WHERE sc_user = ?",
            (user_id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] > 0
    with sqlite3.connect(derived_db_path(settings.db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_keyword_items WHERE sc_user = ?",
            (user_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_hybrid_keyword_items WHERE sc_user = ?",
            (user_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_hybrid_bookkeeping WHERE sc_user = ?",
            (user_id,),
        ).fetchone()[0] == 0


def test_local_settings_editability_does_not_depend_on_debug(monkeypatch):
    from careerdesk.features.settings.service import ui_editable

    monkeypatch.setenv("APP_RUNTIME_MODE", "desktop")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.delenv("APP_GATEWAY_AUTH_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        assert ui_editable() is True
    finally:
        get_settings.cache_clear()


def test_save_model_and_key_applies_immediately(client, env_file):
    r = put_settings(client, {
        "llm_model": "deepseek:deepseek-chat",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        "keys": {"DEEPSEEK_API_KEY": "sk-test-123"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["llm_model"] == "deepseek:deepseek-chat"
    assert body["llm_capabilities"] == {
        **CUSTOM_MODEL_CAPABILITIES,
        "source": "configured",
    }
    assert body["keys"]["DEEPSEEK_API_KEY"] is True
    content = env_file.read_text(encoding="utf-8")
    assert "APP_LLM_MODEL" in content and "deepseek:deepseek-chat" in content
    assert "APP_LLM_CONTEXT_WINDOW='131072'" in content
    assert "APP_LLM_MAX_OUTPUT_TOKENS='8192'" in content
    assert "sk-test-123" in content
    assert get_settings().llm_model == "deepseek:deepseek-chat"
    assert get_settings().llm_context_window == 131_072
    assert get_settings().llm_max_output_tokens == 8_192
    if os.name == "posix":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_installed_credential_only_save_uses_system_store_without_creating_config(
    client,
    env_file,
    monkeypatch,
):
    store = _TestCredentialStore()
    _use_test_system_store(monkeypatch, store)

    response = put_settings(client, {"keys": {"TAVILY_API_KEY": "stored-by-os"}})

    assert response.status_code == 200
    body = response.json()
    assert body["keys"]["TAVILY_API_KEY"] is True
    assert body["credential_storage"] == {
        "kind": "system",
        "available": True,
        "label": store.label,
        "issue": None,
    }
    assert store.values == {"TAVILY_API_KEY": "stored-by-os"}
    assert os.environ["TAVILY_API_KEY"] == "stored-by-os"
    assert not env_file.exists()


def test_installed_model_config_never_contains_system_stored_credential(
    client,
    env_file,
    monkeypatch,
):
    store = _TestCredentialStore()
    _use_test_system_store(monkeypatch, store)

    response = put_settings(client, {
        "llm_model": "ollama:qwen3",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        "keys": {"TAVILY_API_KEY": "not-in-file"},
    })

    assert response.status_code == 200
    content = env_file.read_text(encoding="utf-8")
    assert "APP_LLM_MODEL='ollama:qwen3'" in content
    assert "TAVILY_API_KEY" not in content
    assert "not-in-file" not in content
    assert store.values == {"TAVILY_API_KEY": "not-in-file"}


def test_installed_config_commit_failure_restores_system_credential_and_process_state(
    client,
    env_file,
    monkeypatch,
):
    store = _TestCredentialStore({"TAVILY_API_KEY": "old-secret"})
    _use_test_system_store(monkeypatch, store)
    monkeypatch.setenv("TAVILY_API_KEY", "old-secret")
    revision = client.get("/api/settings").json()["revision"]
    monkeypatch.setattr(
        settings_service.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        settings_service.save(
            expected_revision=revision,
            model_given=True,
            llm_model="ollama:qwen3",
            capabilities_given=True,
            llm_capabilities=CUSTOM_MODEL_CAPABILITIES,
            keys={"TAVILY_API_KEY": "new-secret"},
        )

    assert store.values == {"TAVILY_API_KEY": "old-secret"}
    assert os.environ["TAVILY_API_KEY"] == "old-secret"
    assert not env_file.exists()
    assert settings_service.read_state()["revision"] == revision


def test_installed_partial_credential_failure_rolls_back_all_prior_updates(
    client,
    env_file,
    monkeypatch,
):
    store = _TestCredentialStore({
        "OPENAI_API_KEY": "old-openai",
        "TAVILY_API_KEY": "old-tavily",
    })
    store.fail_set_once = "TAVILY_API_KEY"
    _use_test_system_store(monkeypatch, store)
    monkeypatch.setenv("OPENAI_API_KEY", "old-openai")
    monkeypatch.setenv("TAVILY_API_KEY", "old-tavily")

    response = put_settings(client, {
        "keys": {
            "OPENAI_API_KEY": "new-openai",
            "TAVILY_API_KEY": "new-tavily",
        },
    })

    assert response.status_code == 400
    assert store.values == {
        "OPENAI_API_KEY": "old-openai",
        "TAVILY_API_KEY": "old-tavily",
    }
    assert os.environ["OPENAI_API_KEY"] == "old-openai"
    assert os.environ["TAVILY_API_KEY"] == "old-tavily"
    assert not env_file.exists()


def test_installed_unavailable_system_store_rejects_credential_write_without_side_effects(
    client,
    env_file,
    monkeypatch,
):
    monkeypatch.setattr(
        credentials,
        "_LAST_STATUS",
        credentials.CredentialStoreStatus(
            kind="system",
            available=True,
            label="Test OS Credential Store",
            issue=None,
        ),
    )
    monkeypatch.setattr(settings_service, "_uses_system_credential_store", lambda _settings=None: True)
    monkeypatch.setattr(
        settings_service,
        "_open_system_credential_store",
        lambda: (_ for _ in ()).throw(
            credentials.CredentialStoreUnavailable("系统凭据存储不可用")
        ),
    )

    response = put_settings(client, {"keys": {"TAVILY_API_KEY": "must-not-stick"}})

    assert response.status_code == 400
    assert "must-not-stick" not in response.text
    assert "TAVILY_API_KEY" not in os.environ
    assert not env_file.exists()
    status = client.get("/api/settings").json()["credential_storage"]
    assert status["available"] is False
    assert status["issue"] == "系统凭据存储不可用"


def test_settings_reject_env_symlink_without_touching_target(env_file, tmp_path):
    target = tmp_path / "shared-secrets.env"
    target.write_text("KEEP=this-target\n", encoding="utf-8")
    before_mode = stat.S_IMODE(target.stat().st_mode)
    try:
        env_file.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="符号链接"):
        settings_service.save(
            expected_revision=settings_service.read_state()["revision"],
            model_given=True,
            llm_model="ollama:qwen3",
            capabilities_given=True,
            llm_capabilities=CUSTOM_MODEL_CAPABILITIES,
            keys={},
        )

    assert env_file.is_symlink()
    assert target.read_text(encoding="utf-8") == "KEEP=this-target\n"
    assert stat.S_IMODE(target.stat().st_mode) == before_mode


def test_settings_service_creates_secret_file_privately(env_file, monkeypatch):
    monkeypatch.setenv("APP_LLM_MODEL", "")
    for name in CAPABILITY_ENV_VARS:
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()
    try:
        settings_service.save(
            expected_revision=settings_service.read_state()["revision"],
            model_given=True,
            llm_model="ollama:qwen3",
            capabilities_given=True,
            llm_capabilities=CUSTOM_MODEL_CAPABILITIES,
            keys={},
        )
    finally:
        get_settings.cache_clear()

    assert env_file.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_clear_model_and_key(client, env_file):
    put_settings(client, {"llm_model": "anthropic", "keys": {"ANTHROPIC_API_KEY": "sk-a"}})
    r = put_settings(client, {"llm_model": None, "keys": {"ANTHROPIC_API_KEY": None}})
    body = r.json()
    assert body["llm_model"] is None
    assert body["keys"]["ANTHROPIC_API_KEY"] is False
    assert "sk-a" not in env_file.read_text(encoding="utf-8")
    assert get_settings().llm_model is None


def test_nondefault_model_requires_explicit_capabilities_before_any_write(client, env_file):
    response = put_settings(client, {
        "llm_model": "ollama:qwen3",
    })

    assert response.status_code == 400
    assert "context window" in response.json()["detail"]
    assert not env_file.exists()
    state = client.get("/api/settings").json()
    assert state["llm_model"] is None
    assert state["llm_capabilities"]["source"] is None


def test_model_switch_clears_bound_override_and_uses_exact_provider_default(client, env_file):
    first = put_settings(client, {
        "llm_model": "deepseek:deepseek-chat",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        "keys": {"DEEPSEEK_API_KEY": "deepseek-key"},
    })
    assert first.status_code == 200

    switched = put_settings(client, {
        "llm_model": "anthropic",
        "keys": {"ANTHROPIC_API_KEY": "anthropic-key"},
    })

    assert switched.status_code == 200
    body = switched.json()
    assert body["llm_model"] == "anthropic"
    assert body["llm_capabilities"]["source"] == "provider"
    assert body["llm_capabilities"]["context_window"] > 0
    assert body["llm_capabilities"]["max_output_tokens"] > 0
    content = env_file.read_text(encoding="utf-8")
    assert "APP_LLM_CONTEXT_WINDOW" not in content
    assert "APP_LLM_MAX_OUTPUT_TOKENS" not in content
    assert get_settings().llm_context_window is None
    assert get_settings().llm_max_output_tokens is None


@pytest.mark.parametrize(
    "capabilities",
    [
        {"context_window": 8_192, "max_output_tokens": None},
        {"context_window": None, "max_output_tokens": 2_048},
        {"context_window": 8_192, "max_output_tokens": 16_384},
        {"context_window": "8192", "max_output_tokens": 2_048},
        {"context_window": True, "max_output_tokens": 2_048},
        {"context_window": 8_192, "max_output_tokens": 255},
        {"context_window": 1_023, "max_output_tokens": 256},
        {"context_window": 8_192, "max_output_tokens": 2_048, "extra": 1},
    ],
)
def test_model_capability_api_rejects_partial_coerced_invalid_and_extra_values(
    client,
    env_file,
    capabilities,
):
    response = put_settings(client, {
        "llm_model": "ollama:qwen3",
        "llm_capabilities": capabilities,
    })

    assert response.status_code == 422
    assert not env_file.exists()


def test_partial_update_leaves_other_fields(client):
    put_settings(client, {
        "llm_model": "anthropic", "keys": {"ANTHROPIC_API_KEY": "sk-a"},
    })
    r = put_settings(client, {"keys": {"TAVILY_API_KEY": "tvly-x"}})
    body = r.json()
    assert body["llm_model"] == "anthropic"
    assert body["keys"]["TAVILY_API_KEY"] is True


def test_put_requires_revision_without_creating_env(client, env_file):
    response = client.put("/api/settings", json={"outbound_policy": DEFAULT_POLICY})

    assert response.status_code == 422
    assert not env_file.exists()


def test_stale_revision_cannot_restore_revoked_outbound_consent(client):
    initial = client.get("/api/settings").json()
    first_policy = policy(allow_web_research=True)
    first = put_settings(
        client,
        {"outbound_policy": first_policy},
        revision=initial["revision"],
    )
    assert first.status_code == 200
    assert first.json()["revision"] != initial["revision"]

    stale_policy = policy(allow_conversation_embedding=True)
    stale = put_settings(
        client,
        {"outbound_policy": stale_policy},
        revision=initial["revision"],
    )

    assert stale.status_code == 409
    assert "另一个窗口" in stale.json()["detail"]
    current = client.get("/api/settings").json()
    assert current["outbound_policy"] == first_policy
    assert current["revision"] == first.json()["revision"]


def test_staging_failure_leaves_file_environment_cache_and_revision_unchanged(
    client,
    env_file,
    monkeypatch,
):
    dormant = policy(
        strict_offline=True,
        allow_conversation_embedding=True,
        allow_web_research=True,
    )
    saved = put_settings(client, {"outbound_policy": dormant}).json()
    file_before = env_file.read_bytes()
    environment_before = {name: os.environ.get(name) for name in POLICY_ENV_VARS}
    cached_before = get_settings()

    real_set_key = settings_service.set_key
    calls = 0

    def fail_during_staging(path, name, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staging failure")
        return real_set_key(path, name, value)

    monkeypatch.setattr(settings_service, "set_key", fail_during_staging)
    with pytest.raises(OSError, match="injected staging failure"):
        settings_service.save(
            expected_revision=saved["revision"],
            model_given=False,
            llm_model=None,
            keys={},
            outbound_policy=policy(),
        )

    assert env_file.read_bytes() == file_before
    assert {name: os.environ.get(name) for name in POLICY_ENV_VARS} == environment_before
    assert get_settings() is cached_before
    assert settings_service.read_state()["revision"] == saved["revision"]
    assert list(env_file.parent.glob(".env.staged-*")) == []


def test_directory_fsync_failure_keeps_disk_process_cache_and_revision_consistent(
    client,
    env_file,
    monkeypatch,
):
    initial = client.get("/api/settings").json()

    def fail_directory_sync(_directory):
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(settings_service, "_fsync_directory", fail_directory_sync)
    strict = policy(strict_offline=True, allow_web_research=True)
    saved = put_settings(
        client,
        {"outbound_policy": strict},
        revision=initial["revision"],
    )

    assert saved.status_code == 200
    body = saved.json()
    assert body["outbound_policy"] == strict
    assert body["revision"] != initial["revision"]
    assert "未能确认目录项已持久化" in body["persistence_warning"]
    assert "APP_STRICT_OFFLINE='true'" in env_file.read_text(encoding="utf-8")
    assert os.environ["APP_STRICT_OFFLINE"] == "true"
    assert get_settings().strict_offline is True
    assert settings_service.read_state()["persistence_warning"] == body["persistence_warning"]


@pytest.mark.parametrize(
    "payload",
    [
        {"keys": {"OPENAI_API_KEY": "sk-before\x00after"}},
        {"keys": {"OPENAI_API_KEY": "sk-before\tafter"}},
        {"llm_model": "ollama:qwen3\x00remote"},
        {"llm_model": "ollama:qwen3\x7fremote"},
    ],
)
def test_settings_reject_control_characters_before_replace(client, env_file, payload):
    response = put_settings(client, payload)

    assert response.status_code == 400
    assert "控制字符" in response.json()["detail"]
    assert not env_file.exists()


def test_outbound_policy_is_complete_persisted_and_effective_immediately(client, env_file):
    configured = policy(
        allow_conversation_embedding=True,
        allow_web_research=True,
    )
    response = put_settings(client, {"outbound_policy": configured})

    assert response.status_code == 200
    assert response.json()["outbound_policy"] == configured
    settings = get_settings()
    assert settings.conversation_embedding_enabled is True
    assert settings.web_research_enabled is True
    content = env_file.read_text(encoding="utf-8")
    assert all(name in content for name in POLICY_ENV_VARS)

    dormant = policy(
        strict_offline=True,
        allow_conversation_embedding=True,
        allow_web_research=True,
    )
    response = put_settings(client, {"outbound_policy": dormant})

    assert response.status_code == 200
    assert response.json()["outbound_policy"] == dormant
    settings = get_settings()
    assert settings.allow_conversation_embedding is True
    assert settings.allow_web_research is True
    assert settings.conversation_embedding_enabled is False
    assert settings.web_research_enabled is False


def test_api_keys_never_grant_outbound_consent(client):
    response = put_settings(client, {
        "keys": {
            "OPENAI_API_KEY": "embedding-credential",
            "TAVILY_API_KEY": "search-credential",
        },
    })

    assert response.status_code == 200
    assert response.json()["outbound_policy"] == DEFAULT_POLICY
    settings = get_settings()
    assert settings.conversation_embedding_enabled is False
    assert settings.web_research_enabled is False


@pytest.mark.parametrize(
    "payload",
    [
        {"outbound_policy": None},
        {"outbound_policy": {"strict_offline": False}},
        {"outbound_policy": {**DEFAULT_POLICY, "unexpected": False}},
        {"outbound_policy": {**DEFAULT_POLICY, "strict_offline": "false"}},
        {"unexpected": True},
    ],
)
def test_outbound_policy_rejects_null_partial_coerced_and_unknown_input(client, env_file, payload):
    response = put_settings(client, payload)

    assert response.status_code == 422
    assert not env_file.exists()


def test_strict_offline_keeps_cloud_config_dormant_and_reenable_requires_key(client, env_file):
    saved = put_settings(client, {
        "llm_model": "anthropic",
        "keys": {"ANTHROPIC_API_KEY": "sk-a"},
        "outbound_policy": policy(
            allow_conversation_embedding=True,
        ),
    })
    assert saved.status_code == 200

    dormant_policy = policy(
        strict_offline=True,
        allow_conversation_embedding=True,
    )
    dormant = put_settings(client, {
        "keys": {"ANTHROPIC_API_KEY": None},
        "outbound_policy": dormant_policy,
    })
    assert dormant.status_code == 200
    assert dormant.json()["llm_model"] == "anthropic"
    assert dormant.json()["keys"]["ANTHROPIC_API_KEY"] is False
    assert dormant.json()["outbound_policy"] == dormant_policy

    enabled_policy = policy(
        allow_conversation_embedding=True,
    )
    blocked = put_settings(client, {"outbound_policy": enabled_policy})
    assert blocked.status_code == 400
    assert "ANTHROPIC_API_KEY" in blocked.json()["detail"]
    state = client.get("/api/settings").json()
    assert state["outbound_policy"] == dormant_policy
    assert state["keys"]["ANTHROPIC_API_KEY"] is False

    restored = put_settings(client, {
        "keys": {"ANTHROPIC_API_KEY": "sk-restored"},
        "outbound_policy": enabled_policy,
    })
    assert restored.status_code == 200
    assert restored.json()["outbound_policy"] == enabled_policy
    assert get_settings().conversation_embedding_enabled is True
    assert "sk-restored" in env_file.read_text(encoding="utf-8")


def test_cloud_model_requires_matching_key_without_partial_write(client, env_file):
    blocked = put_settings(client, {"llm_model": "anthropic"})
    assert blocked.status_code == 400
    assert "ANTHROPIC_API_KEY" in blocked.json()["detail"]
    assert get_settings().llm_model is None
    assert not env_file.exists()

    saved = put_settings(client, {
        "llm_model": "anthropic", "keys": {"ANTHROPIC_API_KEY": "sk-a"},
    })
    assert saved.status_code == 200
    assert saved.json()["llm_model"] == "anthropic"
    assert saved.json()["keys"]["ANTHROPIC_API_KEY"] is True


def test_cloud_model_accepts_any_registered_credential_alias(client):
    saved = put_settings(client, {
        "llm_model": "gemini",
        "keys": {"GOOGLE_API_KEY": "google-alias-key"},
    })

    assert saved.status_code == 200
    assert saved.json()["llm_model"] == "gemini"
    assert saved.json()["llm_model_local"] is False
    assert saved.json()["keys"]["GOOGLE_API_KEY"] is True


def test_cannot_clear_key_required_by_current_cloud_model(client, env_file):
    put_settings(client, {
        "llm_model": "anthropic", "keys": {"ANTHROPIC_API_KEY": "sk-a"},
    })
    blocked = put_settings(client, {"keys": {"ANTHROPIC_API_KEY": None}})
    assert blocked.status_code == 400
    assert "ANTHROPIC_API_KEY" in blocked.json()["detail"]
    assert "sk-a" in env_file.read_text(encoding="utf-8")

    switched = put_settings(client, {
        "llm_model": "ollama:qwen3", "keys": {"ANTHROPIC_API_KEY": None},
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
    })
    assert switched.status_code == 200
    assert switched.json()["llm_model"] == "ollama:qwen3"
    assert switched.json()["keys"]["ANTHROPIC_API_KEY"] is False


def test_key_allowlist_enforced(client):
    r = put_settings(client, {"keys": {"APP_GATEWAY_AUTH_SECRET": "evil"}})
    assert r.status_code == 400


def test_bad_model_string_rejected(client):
    assert put_settings(client, {"llm_model": ":gpt-4o"}).status_code == 400
    assert put_settings(client, {"llm_model": "no-such-provider-xyz"}).status_code == 400
    assert put_settings(client, {"llm_model": "ollama"}).status_code == 400
    assert put_settings(client, {
        "llm_model": "ollama:qwen3",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
    }).status_code == 200


def test_model_string_is_normalized_before_persisting_and_running(client, env_file):
    saved = put_settings(client, {
        "llm_model": "  ollama : qwen3  ",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
    })

    assert saved.status_code == 200
    assert saved.json()["llm_model"] == "ollama:qwen3"
    assert saved.json()["llm_model_local"] is True
    assert "ollama:qwen3" in env_file.read_text(encoding="utf-8")
    assert "ollama : qwen3" not in env_file.read_text(encoding="utf-8")


def test_multiline_value_rejected(client):
    r = put_settings(client, {"keys": {"OPENAI_API_KEY": "a\nb"}})
    assert r.status_code == 400


def test_deployed_mode_locks_settings(tmp_path, monkeypatch, env_file):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "ollama:qwen3")
    monkeypatch.setenv("APP_GATEWAY_AUTH_SECRET", "supersecretsharedvalue")
    for name in POLICY_ENV_VARS:
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("APP_STRICT_OFFLINE", "true")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    gateway_headers = {"X-Gateway-Auth": "supersecretsharedvalue", "Remote-User": "me"}
    with TestClient(create_app()) as test_client:
        state = test_client.get("/api/settings", headers=gateway_headers)
        assert state.status_code == 200
        body = state.json()
        assert body["editable"] is False
        assert body["llm_model"] == "ollama:qwen3"
        assert body["llm_model_local"] is True
        assert body["keys"] == {}
        assert body["credential_storage"] == {
            "kind": "server_environment",
            "available": True,
            "label": "服务器环境变量/Secret",
            "issue": None,
        }
        assert body["providers"] == []
        assert body["outbound_policy"] == policy(strict_offline=True)
        assert isinstance(body["revision"], str)
        r = put_settings(
            test_client,
            {"llm_model": "anthropic"},
            revision=body["revision"],
            headers=gateway_headers,
        )
        assert r.status_code == 403
    get_settings.cache_clear()


def test_environment_managed_model_key_and_policy_are_disclosed_and_rejected_atomically(
    client,
    env_file,
    monkeypatch,
):
    managed = {
        "APP_LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "APP_ALLOW_WEB_RESEARCH",
    }
    monkeypatch.setattr(config_module, "_DOTENV_PRELOAD_ENV_KEYS", frozenset(managed))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "external-secret")

    state = client.get("/api/settings").json()
    assert state["environment_managed"]["llm_model"] is True
    assert state["environment_managed"]["keys"]["ANTHROPIC_API_KEY"] is True
    assert state["environment_managed"]["outbound_policy"]["allow_web_research"] is True
    assert "external-secret" not in str(state)

    response = put_settings(client, {
        "llm_model": "ollama:qwen3",
        "keys": {"ANTHROPIC_API_KEY": None},
        "outbound_policy": policy(allow_web_research=True),
    })

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "APP_LLM_MODEL" in detail
    assert "ANTHROPIC_API_KEY" in detail
    assert "APP_ALLOW_WEB_RESEARCH" in detail
    assert "external-secret" not in detail
    assert not env_file.exists()
    assert os.environ["ANTHROPIC_API_KEY"] == "external-secret"


def test_managed_fields_do_not_block_non_conflicting_updates(client, env_file, monkeypatch):
    monkeypatch.setattr(
        config_module,
        "_DOTENV_PRELOAD_ENV_KEYS",
        frozenset({"APP_STRICT_OFFLINE"}),
    )

    response = put_settings(client, {
        "keys": {"TAVILY_API_KEY": "search-key"},
        "outbound_policy": policy(allow_web_research=True),
    })

    assert response.status_code == 200
    assert response.json()["keys"]["TAVILY_API_KEY"] is True
    assert response.json()["outbound_policy"]["allow_web_research"] is True
    content = env_file.read_text(encoding="utf-8")
    assert "TAVILY_API_KEY" in content
    assert "APP_ALLOW_WEB_RESEARCH='true'" in content
    assert "APP_STRICT_OFFLINE" not in content


def test_environment_managed_capabilities_block_model_switch_even_with_same_numbers(
    client,
    env_file,
    monkeypatch,
):
    monkeypatch.setattr(
        config_module,
        "_DOTENV_PRELOAD_ENV_KEYS",
        frozenset(CAPABILITY_ENV_VARS),
    )
    monkeypatch.setenv("APP_LLM_MODEL", "ollama:qwen3")
    monkeypatch.setenv("APP_LLM_CONTEXT_WINDOW", "131072")
    monkeypatch.setenv("APP_LLM_MAX_OUTPUT_TOKENS", "8192")
    get_settings.cache_clear()
    try:
        state = client.get("/api/settings").json()
        assert state["environment_managed"]["llm_capabilities"] == {
            "context_window": True,
            "max_output_tokens": True,
        }

        response = put_settings(client, {
            "llm_model": "ollama:qwen4",
            "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        })

        assert response.status_code == 400
        assert "APP_LLM_CONTEXT_WINDOW" in response.json()["detail"]
        assert "APP_LLM_MAX_OUTPUT_TOKENS" in response.json()["detail"]
        assert not env_file.exists()
        assert get_settings().llm_model == "ollama:qwen3"
    finally:
        get_settings.cache_clear()


def test_managed_noop_does_not_touch_env_or_advance_revision(client, env_file, monkeypatch):
    monkeypatch.setattr(
        config_module,
        "_DOTENV_PRELOAD_ENV_KEYS",
        frozenset({"APP_STRICT_OFFLINE"}),
    )
    initial = client.get("/api/settings").json()

    response = put_settings(
        client,
        {"outbound_policy": DEFAULT_POLICY},
        revision=initial["revision"],
    )

    assert response.status_code == 200
    assert response.json()["revision"] == initial["revision"]
    assert not env_file.exists()


def test_openai_compatible_requires_endpoint_when_active(client, env_file):
    response = put_settings(client, {
        "llm_model": "openai_compatible:custom-model",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        "keys": {"LLM_API_KEY": "proxy-key"},
    })

    assert response.status_code == 400
    assert "OPENAI_BASE_URL" in response.json()["detail"]
    assert "LLM_BASE_URL" in response.json()["detail"]
    assert not env_file.exists()


def test_openai_compatible_endpoint_uses_primary_priority_and_is_safely_disclosed(
    client,
    env_file,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_BASE_URL", "HTTPS://Gateway.Example/v1/team")
    monkeypatch.setenv("LLM_BASE_URL", "https://ignored.example/v1")

    response = put_settings(client, {
        "llm_model": "openai_compatible:custom-model",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        "keys": {"LLM_API_KEY": "proxy-key"},
    })

    assert response.status_code == 200
    endpoint = response.json()["openai_compatible_endpoint"]
    assert endpoint == {
        "status": "configured",
        "url": "https://gateway.example/v1/team",
        "source": "OPENAI_BASE_URL",
        "externally_managed": False,
        "issue": None,
    }
    assert "ignored.example" not in str(endpoint)
    assert "proxy-key" not in str(response.json())
    assert "openai_compatible:custom-model" in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:super-secret@gateway.example/v1",
        "https://gateway.example/v1?token=super-secret",
        "https://gateway.example/v1#super-secret",
        "file:///tmp/socket",
    ],
)
def test_openai_compatible_rejects_unsafe_endpoint_without_leaking_it(
    client,
    env_file,
    monkeypatch,
    endpoint,
):
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    monkeypatch.setenv("LLM_BASE_URL", "https://lower-priority.example/v1")

    state = client.get("/api/settings").json()["openai_compatible_endpoint"]
    assert state["status"] == "invalid"
    assert state["url"] is None
    assert endpoint not in str(state)
    response = put_settings(client, {
        "llm_model": "openai_compatible:custom-model",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        "keys": {"LLM_API_KEY": "proxy-key"},
    })

    assert response.status_code == 400
    assert endpoint not in response.json()["detail"]
    assert "super-secret" not in response.json()["detail"]
    assert not env_file.exists()


def test_strict_offline_allows_dormant_openai_compatible_with_invalid_endpoint(
    client,
    env_file,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://user:secret@gateway.example/v1")

    response = put_settings(client, {
        "llm_model": "openai_compatible:custom-model",
        "llm_capabilities": CUSTOM_MODEL_CAPABILITIES,
        "outbound_policy": policy(strict_offline=True),
    })

    assert response.status_code == 200
    assert response.json()["llm_model"] == "openai_compatible:custom-model"
    assert response.json()["openai_compatible_endpoint"]["status"] == "invalid"
    content = env_file.read_text(encoding="utf-8")
    assert "openai_compatible:custom-model" in content
    assert "secret" not in content
