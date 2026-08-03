"""Installed credential storage stays native, verified, and secret-free in errors.

All tests use in-memory backends.  They never read or mutate the developer's
real Keychain, Credential Locker, Secret Service, or KWallet.
"""

from __future__ import annotations

import json

import pytest

from careerdesk.platform import credentials


EXPECTED_CREDENTIAL_NAMES = {
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPUAI_API_KEY",
    "ZAI_API_KEY",
    "ZHIPU_API_KEY",
    "MODELSCOPE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "GOOGLE_PSE_API_KEY",
    "GOOGLE_PSE_ENGINE_ID",
    "SEARXNG_BASE_URL",
}


def _backend_type(module: str, *, priority=5):
    class Backend:
        def __init__(self):
            self.values: dict[tuple[str, str], str] = {}
            self.calls: list[tuple] = []

        def get_password(self, service: str, account: str) -> str | None:
            self.calls.append(("get", service, account))
            return self.values.get((service, account))

        def set_password(self, service: str, account: str, value: str) -> None:
            self.calls.append(("set", service, account, value))
            self.values[(service, account)] = value

        def delete_password(self, service: str, account: str) -> None:
            self.calls.append(("delete", service, account))
            del self.values[(service, account)]

    Backend.__module__ = module
    Backend.priority = priority
    return Backend


class MemoryStore:
    label = "Test OS Credential Store"

    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})
        self.fail_get: str | None = None
        self.fail_set: str | None = None
        self.fail_delete: str | None = None

    def get(self, name: str) -> str | None:
        if name == self.fail_get:
            raise credentials.CredentialStoreUnavailable("safe read failure")
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        if name == self.fail_set:
            raise credentials.CredentialStoreUnavailable("safe write failure")
        self.values[name] = value

    def delete(self, name: str) -> None:
        if name == self.fail_delete:
            raise credentials.CredentialStoreUnavailable("safe delete failure")
        self.values.pop(name, None)


@pytest.fixture(autouse=True)
def reset_cached_status(monkeypatch):
    monkeypatch.setattr(credentials, "_LAST_STATUS", None)


def test_managed_names_are_exact_and_never_include_server_gateway_secret():
    assert set(credentials.SUPPORTED_CREDENTIAL_NAMES) == EXPECTED_CREDENTIAL_NAMES
    assert len(credentials.SUPPORTED_CREDENTIAL_NAMES) == len(EXPECTED_CREDENTIAL_NAMES)
    assert "APP_GATEWAY_AUTH_SECRET" not in credentials.SUPPORTED_CREDENTIAL_NAMES


@pytest.mark.parametrize(
    ("platform_name", "module", "label"),
    [
        ("win32", "keyring.backends.Windows", "Windows Credential Locker"),
        ("linux", "keyring.backends.SecretService", "Linux Secret Service"),
        ("linux", "keyring.backends.kwallet", "KDE KWallet"),
    ],
)
def test_per_name_backends_use_one_service_and_verify_mutations(
    platform_name,
    module,
    label,
):
    backend = _backend_type(module)()
    store = credentials.SystemCredentialStore(backend, platform_name=platform_name)

    assert store.label == label
    assert store.get("OPENAI_API_KEY") is None
    store.set("OPENAI_API_KEY", "secret-value")
    assert store.get("OPENAI_API_KEY") == "secret-value"
    store.delete("OPENAI_API_KEY")
    assert store.get("OPENAI_API_KEY") is None
    assert all(call[1] == credentials.SERVICE_NAME for call in backend.calls)
    assert all(call[2] == "OPENAI_API_KEY" for call in backend.calls)


_MACOS_RECORD_KEY = (credentials.MACOS_SERVICE_NAME, credentials.MACOS_ACCOUNT_NAME)


def _macos_store(values: dict[tuple[str, str], str] | None = None):
    backend = _backend_type("keyring.backends.macOS")()
    backend.values.update(values or {})
    return backend, credentials.SystemCredentialStore(backend, platform_name="darwin")


def test_macos_keeps_every_credential_in_one_keychain_record():
    backend, store = _macos_store()

    assert store.label == "macOS Keychain"
    assert store.get("OPENAI_API_KEY") is None
    store.set("OPENAI_API_KEY", "secret-a")
    store.set("TAVILY_API_KEY", "secret-b")

    assert set(backend.values) == {_MACOS_RECORD_KEY}
    assert json.loads(backend.values[_MACOS_RECORD_KEY]) == {
        "OPENAI_API_KEY": "secret-a",
        "TAVILY_API_KEY": "secret-b",
    }
    assert store.get("OPENAI_API_KEY") == "secret-a"

    store.delete("OPENAI_API_KEY")
    assert store.get("OPENAI_API_KEY") is None
    assert store.get("TAVILY_API_KEY") == "secret-b"
    store.delete("TAVILY_API_KEY")
    assert backend.values == {}


def test_macos_reads_share_one_backend_query_per_store_instance():
    backend, store = _macos_store({
        _MACOS_RECORD_KEY: json.dumps({"OPENAI_API_KEY": "secret-a"}),
    })

    assert store.get("OPENAI_API_KEY") == "secret-a"
    for name in credentials.SUPPORTED_CREDENTIAL_NAMES:
        store.get(name)

    assert len([call for call in backend.calls if call[0] == "get"]) == 1


def test_macos_never_touches_the_per_name_service():
    backend, store = _macos_store({
        (credentials.SERVICE_NAME, "OPENAI_API_KEY"): "per-name-item",
    })

    assert store.get("OPENAI_API_KEY") is None
    store.set("OPENAI_API_KEY", "record-value")
    store.delete("OPENAI_API_KEY")

    assert backend.values == {
        (credentials.SERVICE_NAME, "OPENAI_API_KEY"): "per-name-item",
    }
    assert all(call[1] == credentials.MACOS_SERVICE_NAME for call in backend.calls)


@pytest.mark.parametrize(
    "raw",
    ["not-json", '["OPENAI_API_KEY"]', '{"OPENAI_API_KEY": 1}', '{"OPENAI_API_KEY": ""}'],
)
def test_macos_corrupt_record_fails_without_echoing_its_content(raw):
    backend, store = _macos_store({_MACOS_RECORD_KEY: raw})

    with pytest.raises(credentials.CredentialStoreUnavailable) as caught:
        store.get("OPENAI_API_KEY")

    assert raw not in str(caught.value)
    assert credentials.MACOS_SERVICE_NAME in str(caught.value)


def test_macos_write_readback_mismatch_fails_without_disclosing_the_secret():
    backend, store = _macos_store()
    backend.set_password = lambda _service, _account, _value: None

    with pytest.raises(credentials.CredentialStoreUnavailable) as caught:
        store.set("OPENAI_API_KEY", "do-not-leak-me")

    assert "do-not-leak-me" not in str(caught.value)


def test_chainer_selection_uses_only_its_highest_priority_native_child():
    third_party = _backend_type("third_party.file_keyring", priority=99)()
    native = _backend_type("keyring.backends.SecretService", priority=5)()
    chainer = _backend_type("keyring.backends.chainer", priority=10)()
    chainer.backends = [third_party, native]

    store = credentials.SystemCredentialStore(chainer, platform_name="linux")
    store.set("OPENAI_API_KEY", "native-only")

    assert store.label == "Linux Secret Service"
    assert native.values == {
        (credentials.SERVICE_NAME, "OPENAI_API_KEY"): "native-only"
    }
    assert third_party.values == {}
    assert chainer.calls == []


@pytest.mark.parametrize(
    ("platform_name", "module", "priority"),
    [
        ("darwin", "keyring.backends.null", 0),
        ("linux", "keyring.backends.fail", 0),
        ("linux", "keyring.backends.chainer", 10),
        ("linux", "third_party.file_keyring", 5),
        ("win32", "keyring.backends.macOS", 5),
        ("linux", "keyring.backends.SecretService", 0),
    ],
)
def test_null_fail_unsafe_chained_third_party_wrong_platform_and_disabled_backends_are_rejected(
    platform_name,
    module,
    priority,
):
    with pytest.raises(credentials.CredentialStoreUnavailable) as caught:
        credentials.SystemCredentialStore(
            _backend_type(module, priority=priority)(),
            platform_name=platform_name,
        )

    assert module not in str(caught.value)


def test_write_readback_mismatch_fails_without_disclosing_the_secret():
    backend = _backend_type("keyring.backends.Windows")()
    backend.get_password = lambda _service, _account: "different"
    store = credentials.SystemCredentialStore(backend, platform_name="win32")

    with pytest.raises(credentials.CredentialStoreUnavailable) as caught:
        store.set("OPENAI_API_KEY", "do-not-leak-me")

    assert "do-not-leak-me" not in str(caught.value)


def test_injection_preserves_external_environment_and_loads_only_absent_values(
    tmp_path,
    monkeypatch,
):
    store = MemoryStore({
        "OPENAI_API_KEY": "stored-openai",
        "TAVILY_API_KEY": "stored-tavily",
    })
    monkeypatch.setattr(credentials, "SystemCredentialStore", lambda: store)
    environment = {"OPENAI_API_KEY": "external-openai"}

    status = credentials.inject_system_credentials(
        config_file=tmp_path / "missing.env",
        environment=environment,
    )

    assert status.available is True and status.label == store.label
    assert environment == {
        "OPENAI_API_KEY": "external-openai",
        "TAVILY_API_KEY": "stored-tavily",
    }


def test_partial_injection_failure_removes_only_values_added_by_this_attempt(
    tmp_path,
    monkeypatch,
):
    store = MemoryStore({"OPENAI_API_KEY": "injected-secret"})
    store.fail_get = "DEEPSEEK_API_KEY"
    monkeypatch.setattr(credentials, "SystemCredentialStore", lambda: store)
    environment = {"ANTHROPIC_API_KEY": "external-secret"}

    status = credentials.inject_system_credentials(
        config_file=tmp_path / "missing.env",
        environment=environment,
    )

    assert status.available is False
    assert environment == {"ANTHROPIC_API_KEY": "external-secret"}
    assert "injected-secret" not in (status.issue or "")
    assert "external-secret" not in (status.issue or "")


def test_installed_plaintext_secret_is_rejected_without_modifying_file(tmp_path):
    config_file = tmp_path / "settings.env"
    original = (
        "APP_TIMEZONE=Asia/Shanghai\n"
        "OPENAI_API_KEY=plaintext-must-stay-untouched\n"
        "TAVILY_API_KEY=\n"
    ).encode()
    config_file.write_bytes(original)

    with pytest.raises(RuntimeError) as caught:
        credentials.reject_plaintext_credentials(config_file)

    assert config_file.read_bytes() == original
    assert "OPENAI_API_KEY" in str(caught.value)
    assert "plaintext-must-stay-untouched" not in str(caught.value)


@pytest.mark.parametrize(
    "content",
    [
        "# OPENAI_API_KEY=comment-only\n",
        "OPENAI_API_KEY=\nTAVILY_API_KEY='   '\n",
        "APP_LLM_MODEL=ollama:qwen3\n",
    ],
)
def test_comments_empty_credentials_and_non_secret_config_are_accepted(
    tmp_path,
    content,
):
    config_file = tmp_path / "settings.env"
    config_file.write_text(content, encoding="utf-8")

    credentials.reject_plaintext_credentials(config_file)


def test_only_supported_credential_accounts_can_be_accessed():
    backend = _backend_type("keyring.backends.macOS")()
    store = credentials.SystemCredentialStore(backend, platform_name="darwin")

    with pytest.raises(ValueError, match="不支持"):
        store.get("APP_GATEWAY_AUTH_SECRET")
    with pytest.raises(ValueError, match="不支持"):
        store.set("PATH", "/tmp/evil")

    assert backend.calls == []
