"""Read-only resources and writable platform roots must not collapse together."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from careerdesk.core.config import Settings
from careerdesk.core.paths import platform_directories


def test_macos_platform_directories_are_separate_and_conventional() -> None:
    roots = platform_directories(platform="darwin", environment={}, home=Path("/Users/tester"))

    assert roots.data == Path("/Users/tester/Library/Application Support/CareerDesk/data")
    assert roots.config == Path("/Users/tester/Library/Application Support/CareerDesk/config")
    assert roots.log == Path("/Users/tester/Library/Logs/CareerDesk")
    assert len({roots.data, roots.config, roots.log}) == 3


def test_windows_platform_directories_honor_local_app_data() -> None:
    roots = platform_directories(
        platform="win32",
        environment={"LOCALAPPDATA": "C:/Users/tester/AppData/Local"},
        home=Path("C:/Users/tester"),
    )

    assert roots.data == Path("C:/Users/tester/AppData/Local/CareerDesk/data")
    assert roots.config == Path("C:/Users/tester/AppData/Local/CareerDesk/config")
    assert roots.log == Path("C:/Users/tester/AppData/Local/CareerDesk/logs")


def test_linux_platform_directories_honor_all_xdg_roots() -> None:
    roots = platform_directories(
        platform="linux",
        environment={
            "XDG_DATA_HOME": "/var/user-data",
            "XDG_CONFIG_HOME": "/var/user-config",
            "XDG_STATE_HOME": "/var/user-state",
        },
        home=Path("/home/tester"),
    )

    assert roots.data == Path("/var/user-data/careerdesk")
    assert roots.config == Path("/var/user-config/careerdesk")
    assert roots.log == Path("/var/user-state/careerdesk/logs")


def test_custom_data_root_gets_a_separate_sibling_log_root(tmp_path) -> None:
    settings = Settings(_env_file=None, data_dir=str(tmp_path / "portable" / "data"))

    assert settings.data_dir == str((tmp_path / "portable" / "data").resolve())
    assert settings.log_dir == str((tmp_path / "portable" / "logs").resolve())
    assert settings.trace_path == str((tmp_path / "portable" / "logs" / "traces.jsonl").resolve())


@pytest.mark.parametrize("relative", [".", "logs", "logs/nested"])
def test_log_root_cannot_equal_or_nest_within_data_root(tmp_path, relative) -> None:
    data = tmp_path / "data"
    requested_log = data / relative

    with pytest.raises(ValidationError, match="必须与 APP_DATA_DIR 分离"):
        Settings(_env_file=None, data_dir=str(data), log_dir=str(requested_log))


def test_data_root_cannot_nest_within_log_root(tmp_path) -> None:
    with pytest.raises(ValidationError, match="必须与 APP_DATA_DIR 分离"):
        Settings(
            _env_file=None,
            data_dir=str(tmp_path / "runtime" / "data"),
            log_dir=str(tmp_path / "runtime"),
        )
