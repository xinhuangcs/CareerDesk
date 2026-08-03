"""Installed storage location disclosure and non-destructive relocation."""

from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values
import pytest

from careerdesk.platform import credentials
from careerdesk.platform.database import init_db
from careerdesk.platform.storage import location


def _installed_location(tmp_path: Path, monkeypatch):
    source = tmp_path / "current-data"
    init_db(str(source / "careerdesk.db"))
    upload = source / "uploads" / "resumes" / "u1" / "resume.md"
    upload.parent.mkdir(parents=True)
    upload.write_text("private resume", encoding="utf-8")
    config = tmp_path / "config" / "settings.env"
    config.parent.mkdir()
    config.write_text("APP_TIMEZONE=Asia/Shanghai\n", encoding="utf-8")
    config.chmod(0o600)
    logs = tmp_path / "logs"

    monkeypatch.setattr(location, "SOURCE_LAYOUT", False)
    monkeypatch.setattr(location, "DEFAULT_DATA_DIR", source)
    monkeypatch.setattr(location, "ENV_FILE", config)
    monkeypatch.setattr(
        location,
        "get_settings",
        lambda: SimpleNamespace(
            data_dir=str(source),
            log_dir=str(logs),
            runtime_mode="desktop",
        ),
    )
    monkeypatch.setattr(
        location,
        "externally_managed_environment_variable",
        lambda _name: False,
    )
    monkeypatch.setattr(
        credentials,
        "current_system_status",
        lambda: credentials.CredentialStoreStatus(
            kind="system",
            available=True,
            label="测试系统凭据存储",
            issue=None,
        ),
    )
    return source, config, logs


def test_storage_state_discloses_paths_but_never_credentials(tmp_path, monkeypatch):
    source, config, logs = _installed_location(tmp_path, monkeypatch)

    state = location.storage_state()

    assert state == {
        "data_dir": str(source),
        "config_dir": str(config.parent),
        "log_dir": str(logs),
        "uses_default_data_dir": True,
        "can_customize": True,
        "customization_issue": None,
        "migration_pending": None,
        "migration_issue": None,
        "credential_storage_kind": "system",
        "credential_location": "测试系统凭据存储",
    }


def test_migration_is_verified_switched_last_and_keeps_source(tmp_path, monkeypatch):
    source, config, _logs = _installed_location(tmp_path, monkeypatch)
    destination = tmp_path / "relocated-data"

    pending = location.request_data_directory_migration(str(destination))
    assert pending["migration_pending"] == str(destination)
    request_file = config.parent / location.MIGRATION_REQUEST
    assert request_file.is_file()

    migrated = location.perform_pending_migration()

    assert migrated == destination
    assert source.is_dir()
    assert (source / "uploads/resumes/u1/resume.md").read_text() == "private resume"
    assert (destination / "uploads/resumes/u1/resume.md").read_text() == "private resume"
    assert dotenv_values(config)["APP_DATA_DIR"] == str(destination)
    assert not request_file.exists()


def test_migration_retries_a_verified_restore_before_switching_config(tmp_path, monkeypatch):
    source, config, _logs = _installed_location(tmp_path, monkeypatch)
    destination = tmp_path / "relocated-data"
    request_file = config.parent / location.MIGRATION_REQUEST
    location.request_data_directory_migration(str(destination))
    write_config = location._write_configured_data_dir
    monkeypatch.setattr(
        location,
        "_write_configured_data_dir",
        lambda _destination: (_ for _ in ()).throw(OSError("simulated config failure")),
    )

    with pytest.raises(OSError, match="simulated config failure"):
        location.perform_pending_migration()

    assert source.is_dir()
    assert (destination / "uploads/resumes/u1/resume.md").read_text() == "private resume"
    assert dotenv_values(config).get("APP_DATA_DIR") is None
    assert location._read_request()["phase"] == "restored"

    monkeypatch.setattr(location, "_write_configured_data_dir", write_config)
    assert location.perform_pending_migration() == destination
    assert dotenv_values(config)["APP_DATA_DIR"] == str(destination)
    assert not request_file.exists()


def test_cancel_pending_migration_keeps_every_directory(tmp_path, monkeypatch):
    source, _config, _logs = _installed_location(tmp_path, monkeypatch)
    destination = tmp_path / "relocated-data"
    location.request_data_directory_migration(str(destination))

    state = location.cancel_pending_migration()

    assert state["migration_pending"] is None
    assert source.is_dir()
    assert not destination.exists()


def test_migration_rejects_existing_nested_and_sync_targets(tmp_path, monkeypatch):
    source, _config, _logs = _installed_location(tmp_path, monkeypatch)
    existing = tmp_path / "existing"
    existing.mkdir()

    for target, message in (
        (existing, "尚不存在"),
        (source / "nested", "互相包含"),
        (tmp_path / "OneDrive - Example" / "CareerDesk", "活动数据库不能放"),
    ):
        try:
            location.request_data_directory_migration(str(target))
        except location.StorageLocationError as error:
            assert message in str(error)
        else:
            raise AssertionError(f"unsafe target accepted: {target}")


def test_reveal_uses_only_resolved_product_paths(tmp_path, monkeypatch):
    source, _config, _logs = _installed_location(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-open")
    monkeypatch.setattr(location.sys, "platform", "darwin")
    monkeypatch.setattr(
        location.subprocess,
        "run",
        lambda command, check, env: calls.append((command, check, env)),
    )

    revealed = location.reveal_directory("data")

    assert revealed == source
    assert calls[0][0:2] == (["/usr/bin/open", str(source)], True)
    assert "OPENAI_API_KEY" not in calls[0][2]


def test_reveal_is_unavailable_in_server_mode(tmp_path, monkeypatch):
    _source, _config, _logs = _installed_location(tmp_path, monkeypatch)
    monkeypatch.setattr(
        location,
        "get_settings",
        lambda: SimpleNamespace(runtime_mode="server"),
    )
    calls = []
    monkeypatch.setattr(location.subprocess, "run", lambda *_args, **_kwargs: calls.append(True))

    try:
        location.reveal_directory("data")
    except location.StorageLocationError as error:
        assert "远程服务模式" in str(error)
    else:
        raise AssertionError("server mode was allowed to open the host file manager")
    assert calls == []
