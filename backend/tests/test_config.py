
from datetime import datetime
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from fastapi import HTTPException

from careerdesk.auth import current_user_id, verify_auth_config
from careerdesk.core.config import (Settings, local_today,
                                   resolve_openai_compatible_endpoint)


def test_invalid_timezone_fails_fast():
    with pytest.raises(ValidationError, match="未知时区"):
        Settings(_env_file=None, timezone="Mars/Olympus_Mons")


def test_local_today_uses_requested_iana_timezone():
    timezone = "Pacific/Kiritimati"

    assert local_today(timezone) == datetime.now(ZoneInfo(timezone)).date()


def test_outbound_permissions_default_off_even_without_keys(monkeypatch):
    for name in (
        "APP_STRICT_OFFLINE",
        "APP_ALLOW_CONVERSATION_EMBEDDING",
        "APP_ALLOW_WEB_RESEARCH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "credential-is-not-consent")
    monkeypatch.setenv("TAVILY_API_KEY", "credential-is-not-consent")

    settings = Settings(_env_file=None)

    assert settings.strict_offline is False
    assert settings.allow_conversation_embedding is False
    assert settings.allow_web_research is False
    assert settings.conversation_embedding_enabled is False
    assert settings.web_research_enabled is False


def test_strict_offline_masks_but_does_not_erase_child_permissions():
    settings = Settings(
        _env_file=None,
        strict_offline=True,
        allow_conversation_embedding=True,
        allow_web_research=True,
    )

    assert settings.allow_conversation_embedding is True
    assert settings.allow_web_research is True
    assert settings.conversation_embedding_enabled is False
    assert settings.web_research_enabled is False

    restored = settings.model_copy(update={"strict_offline": False})
    assert restored.conversation_embedding_enabled is True
    assert restored.web_research_enabled is True


def _verify(monkeypatch, **overrides):
    values = {
        "_env_file": None,
        "runtime_mode": "server",
        "debug": False,
        "gateway_auth_secret": "server-secret",
        "dev_fake_user": None,
        "allowed_hosts": "jobs.example.com",
        "allowed_origins": "https://jobs.example.com",
    }
    values.update(overrides)
    settings = Settings(**values)
    monkeypatch.setattr("careerdesk.auth.get_settings", lambda: settings)
    return verify_auth_config()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"debug": True}, "APP_DEBUG=false"),
        ({"gateway_auth_secret": None}, "APP_GATEWAY_AUTH_SECRET"),
        ({"dev_fake_user": "me"}, "APP_DEV_FAKE_USER"),
        ({"allowed_hosts": ""}, "APP_ALLOWED_HOSTS"),
        ({"allowed_hosts": "*"}, "APP_ALLOWED_HOSTS"),
        ({"allowed_origins": ""}, "APP_ALLOWED_ORIGINS"),
        ({"allowed_origins": "http://jobs.example.com"}, "HTTPS origin"),
        ({"allowed_origins": "https://jobs.example.com/"}, "HTTPS origin"),
    ],
)
def test_server_runtime_security_matrix_fails_closed(monkeypatch, overrides, message):
    with pytest.raises(RuntimeError, match=message):
        _verify(monkeypatch, **overrides)


def test_server_runtime_security_matrix_accepts_explicit_safe_config(monkeypatch):
    assert _verify(monkeypatch) is None


def test_debug_no_longer_controls_non_server_security_mode(monkeypatch):
    assert _verify(
        monkeypatch,
        runtime_mode="development",
        debug=False,
        gateway_auth_secret=None,
        dev_fake_user="me",
        allowed_hosts="",
        allowed_origins="",
    ) is None


def test_server_requests_never_fail_open_when_lifespan_is_disabled(monkeypatch):
    settings = Settings(
        _env_file=None,
        runtime_mode="server",
        gateway_auth_secret=None,
        dev_fake_user="me",
    )
    monkeypatch.setattr("careerdesk.auth.get_settings", lambda: settings)

    with pytest.raises(HTTPException) as caught:
        current_user_id(remote_user="forged", gateway_secret=None)

    assert caught.value.status_code == 503


def test_data_dir_is_canonical_and_server_lists_are_trimmed(tmp_path):
    configured = tmp_path / "nested" / ".." / "data"
    settings = Settings(
        _env_file=None,
        data_dir=str(configured),
        runtime_mode="server",
        allowed_hosts=" Jobs.Example.com , api.example.com ",
        allowed_origins=" https://jobs.example.com ,https://api.example.com ",
    )

    assert settings.data_dir == str((tmp_path / "data").resolve())
    assert settings.allowed_host_list == ["jobs.example.com", "api.example.com"]
    assert settings.allowed_origin_list == [
        "https://jobs.example.com", "https://api.example.com",
    ]


@pytest.mark.parametrize(
    "unsafe",
    [Path(Path.cwd().anchor), Path.home(), Path(tempfile.gettempdir())],
)
def test_data_dir_must_be_a_dedicated_subdirectory(unsafe):
    with pytest.raises(ValidationError, match="专用子目录"):
        Settings(_env_file=None, data_dir=str(unsafe))


def test_blank_data_dir_is_rejected_instead_of_becoming_repository_root():
    with pytest.raises(ValidationError, match="不能为空"):
        Settings(_env_file=None, data_dir="")


def test_openai_compatible_endpoint_matches_agentmaker_priority_and_keeps_path(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "HTTPS://Gateway.Example/v1/team")
    monkeypatch.setenv("LLM_BASE_URL", "https://ignored.example/v1")

    endpoint = resolve_openai_compatible_endpoint()

    assert endpoint.status == "configured"
    assert endpoint.url == "https://gateway.example/v1/team"
    assert endpoint.source == "OPENAI_BASE_URL"


def test_openai_compatible_endpoint_skips_empty_primary_value(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9000/v1")

    endpoint = resolve_openai_compatible_endpoint()

    assert endpoint.status == "configured"
    assert endpoint.url == "http://127.0.0.1:9000/v1"
    assert endpoint.source == "LLM_BASE_URL"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://gateway.example/v1",
        "https:///v1",
        "https://user:secret@gateway.example/v1",
        "https://gateway.example/v1?token=secret",
        "https://gateway.example/v1#secret",
        "https://gateway.example/v1\nsecond",
        "https://gateway.example:99999/v1",
    ],
)
def test_openai_compatible_endpoint_rejects_unsafe_urls_without_echoing_value(
    monkeypatch,
    value,
):
    monkeypatch.setenv("OPENAI_BASE_URL", value)
    monkeypatch.setenv("LLM_BASE_URL", "https://lower-priority.example/v1")

    endpoint = resolve_openai_compatible_endpoint()

    assert endpoint.status == "invalid"
    assert endpoint.url is None
    assert endpoint.source == "OPENAI_BASE_URL"
    assert value not in (endpoint.issue or "")
