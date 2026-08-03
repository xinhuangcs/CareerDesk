"""Read-only resources and writable platform paths have one owner.

Source checkouts intentionally keep their existing ``.env`` and ``data/``
locations.  An installed wheel instead uses the operating system's data,
configuration, and log homes; code and bundled frontend assets remain under
the read-only Python package root.
"""

from dataclasses import dataclass
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping


APP_DIRECTORY_NAME = "CareerDesk"
LINUX_APP_DIRECTORY_NAME = "careerdesk"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
# Captured before the installed launcher injects OS-keyring values.  Config uses
# this immutable set to distinguish real shell/container ownership from values
# loaded by CareerDesk itself.
STARTUP_ENV_KEYS = frozenset(os.environ)


@dataclass(frozen=True, slots=True)
class PlatformDirectories:
    """Writable roots for one installed desktop application."""

    data: Path
    config: Path
    log: Path


def platform_directories(
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> PlatformDirectories:
    """Resolve platform conventions without adding a runtime dependency."""
    platform_name = platform or sys.platform
    values = os.environ if environment is None else environment
    user_home = (home or Path.home()).expanduser().resolve()

    if platform_name == "darwin":
        application_support = user_home / "Library" / "Application Support" / APP_DIRECTORY_NAME
        return PlatformDirectories(
            data=application_support / "data",
            config=application_support / "config",
            log=user_home / "Library" / "Logs" / APP_DIRECTORY_NAME,
        )
    if platform_name == "win32":
        local_app_data = Path(
            values.get("LOCALAPPDATA", str(user_home / "AppData" / "Local"))
        ).expanduser()
        application_root = local_app_data / APP_DIRECTORY_NAME
        return PlatformDirectories(
            data=application_root / "data",
            config=application_root / "config",
            log=application_root / "logs",
        )

    data_home = Path(
        values.get("XDG_DATA_HOME", str(user_home / ".local" / "share"))
    ).expanduser()
    config_home = Path(
        values.get("XDG_CONFIG_HOME", str(user_home / ".config"))
    ).expanduser()
    state_home = Path(
        values.get("XDG_STATE_HOME", str(user_home / ".local" / "state"))
    ).expanduser()
    return PlatformDirectories(
        data=data_home / LINUX_APP_DIRECTORY_NAME,
        config=config_home / LINUX_APP_DIRECTORY_NAME,
        log=state_home / LINUX_APP_DIRECTORY_NAME / "logs",
    )


def _resolve_resource_root() -> Path:
    configured = os.environ.get("CAREERDESK_RESOURCE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
# parents[4] assumes source layout depth; shallow installs fall back to PACKAGE_ROOT.
    parents = Path(__file__).resolve().parents
    source_candidate = parents[4] if len(parents) > 4 else None
    if (
        source_candidate is not None
        and (source_candidate / "backend" / "src" / "careerdesk").is_dir()
        and (source_candidate / "frontend" / "package.json").is_file()
    ):
        return source_candidate
    return PACKAGE_ROOT


RESOURCE_ROOT = _resolve_resource_root()
SOURCE_LAYOUT = (
    (RESOURCE_ROOT / "backend" / "src" / "careerdesk").is_dir()
    and (RESOURCE_ROOT / "frontend" / "package.json").is_file()
)
PLATFORM_DIRECTORIES = platform_directories()
DEFAULT_DATA_DIR = RESOURCE_ROOT / "data" if SOURCE_LAYOUT else PLATFORM_DIRECTORIES.data
DEFAULT_CONFIG_DIR = RESOURCE_ROOT if SOURCE_LAYOUT else PLATFORM_DIRECTORIES.config
DEFAULT_LOG_DIR = RESOURCE_ROOT / "logs" if SOURCE_LAYOUT else PLATFORM_DIRECTORIES.log
DEFAULT_ENV_TEMPLATE = (
    RESOURCE_ROOT / ".env.example" if SOURCE_LAYOUT else PACKAGE_ROOT / "default.env"
)
ENV_FILE = Path(os.environ.get(
    "CAREERDESK_CONFIG_FILE",
    str(DEFAULT_CONFIG_DIR / (".env" if SOURCE_LAYOUT else "settings.env")),
)).expanduser().resolve()
DEFAULT_FRONTEND_DIST_DIR = (
    RESOURCE_ROOT / "frontend" / "dist"
    if SOURCE_LAYOUT
    else PACKAGE_ROOT / "frontend_dist"
)


def _canonical_managed_directory(value: str | Path, *, variable: str) -> Path:
    """Canonicalize one dedicated writable root and reject dangerous broad paths."""
    text = str(value).strip()
    if not text:
        raise ValueError(f"{variable} 不能为空")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = RESOURCE_ROOT / candidate
    resolved = candidate.resolve()
    reserved = {
        Path(resolved.anchor),
        Path.home().resolve(),
        Path(tempfile.gettempdir()).resolve(),
        RESOURCE_ROOT.resolve(),
    }
    if resolved in reserved:
        raise ValueError(
            f"{variable} 必须指向专用子目录，不能是文件系统根、HOME、"
            "系统临时根或 CareerDesk 资源根"
        )
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"{variable} 已存在但不是目录：{resolved}")
    return resolved


def canonical_data_dir(value: str | Path) -> Path:
    """Return a dedicated data root, rejecting broad system/repository directories.

    The instance lock tightens root permissions, so APP_DATA_DIR must be a dedicated
    subdirectory rather than slash, home, system temp, or repository root.
    """
    return _canonical_managed_directory(value, variable="APP_DATA_DIR")


def canonical_log_dir(value: str | Path) -> Path:
    """Return the dedicated runtime-log root."""
    return _canonical_managed_directory(value, variable="APP_LOG_DIR")
