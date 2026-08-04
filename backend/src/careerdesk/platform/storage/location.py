"""Desktop storage disclosure, directory reveal, and non-destructive relocation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Literal

from dotenv import set_key

from ...core.config import externally_managed_environment_variable, get_settings
from ...core.paths import DEFAULT_DATA_DIR, ENV_FILE, SOURCE_LAYOUT, canonical_data_dir
from .. import credentials
from ..database.backup import create_backup, restore_backup
from .private import ensure_private_directory


MIGRATION_REQUEST = "data-directory-migration.json"
_SYNC_DIRECTORY_MARKERS = (
    "dropbox",
    "google drive",
    "googledrive",
    "mobile documents",
    "onedrive",
)


class StorageLocationError(RuntimeError):
    """A requested desktop storage action is unavailable or unsafe."""


def _migration_request_path() -> Path:
    return ENV_FILE.parent / MIGRATION_REQUEST


def _customization_issue() -> str | None:
    settings = get_settings()
    if SOURCE_LAYOUT or settings.runtime_mode != "desktop":
        return "自定义数据目录只在安装式桌面版中提供。"
    if externally_managed_environment_variable("APP_DATA_DIR"):
        return "APP_DATA_DIR 由启动环境托管，请先完全退出并在启动环境中修改。"
    return None


def _read_request() -> dict[str, str] | None:
    path = _migration_request_path()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StorageLocationError("数据目录迁移请求文件不安全，已拒绝读取。")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StorageLocationError("数据目录迁移请求损坏，旧数据目录保持不变。") from error
    required = {"source", "destination", "requested_at", "phase"}
    if (
        not isinstance(raw, dict)
        or set(raw) - (required | {"issue"})
        or not required.issubset(raw)
        or not all(isinstance(value, str) for value in raw.values())
        or raw["phase"] not in {"requested", "restored"}
    ):
        raise StorageLocationError("数据目录迁移请求格式无效，旧数据目录保持不变。")
    return raw


def _write_request(payload: dict[str, str]) -> None:
    path = _migration_request_path()
    ensure_private_directory(path.parent)
    descriptor, staged_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(staged_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, path)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def storage_state() -> dict[str, object]:
    settings = get_settings()
    data_dir = canonical_data_dir(settings.data_dir)
    request = _read_request()
    credentials_managed_by_environment = any(
        externally_managed_environment_variable(name)
        for name in credentials.SUPPORTED_CREDENTIAL_NAMES
    )
    if credentials_managed_by_environment:
        credential_kind = "server_environment"
        credential_location = "CareerDesk 启动环境（未写入应用数据目录）"
    elif not SOURCE_LAYOUT and settings.runtime_mode == "desktop":
        credential_status = credentials.current_system_status()
        credential_kind = "system"
        credential_location = credential_status.label
    else:
        credential_kind = "configuration_file"
        credential_location = str(ENV_FILE)
    return {
        "data_dir": str(data_dir),
        "config_dir": str(ENV_FILE.parent),
        "log_dir": str(Path(settings.log_dir).expanduser().resolve()),
        "uses_default_data_dir": data_dir == Path(DEFAULT_DATA_DIR).resolve(),
        "can_customize": _customization_issue() is None,
        "customization_issue": _customization_issue(),
        "migration_pending": request["destination"] if request else None,
        "migration_issue": request.get("issue") if request else None,
        "credential_storage_kind": credential_kind,
        "credential_location": credential_location,
    }


def _reveal_environment() -> dict[str, str]:
    allowed = {
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "XDG_CURRENT_DESKTOP",
        "XDG_RUNTIME_DIR",
    }
    return {name: value for name, value in os.environ.items() if name in allowed}


def reveal_directory(target: Literal["data", "config", "logs"]) -> Path:
    if get_settings().runtime_mode == "server":
        raise StorageLocationError("远程服务模式不能打开服务器上的文件管理器。")
    state = storage_state()
    key = {"data": "data_dir", "config": "config_dir", "logs": "log_dir"}[target]
    path = Path(str(state[key]))
    reveal = path if path.exists() else path.parent
    try:
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", str(reveal)], check=True, env=_reveal_environment())
        elif sys.platform == "win32":
            os.startfile(reveal)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(reveal)], check=True, env=_reveal_environment())
    except (OSError, subprocess.CalledProcessError) as error:
        raise StorageLocationError(f"无法打开目录：{reveal}") from error
    return reveal


def request_data_directory_migration(destination: str) -> dict[str, object]:
    issue = _customization_issue()
    if issue:
        raise StorageLocationError(issue)
    if not destination.strip():
        raise StorageLocationError("请选择一个全新的专用数据目录。")
    current = canonical_data_dir(get_settings().data_dir)
    target = canonical_data_dir(destination)
    if target == current:
        raise StorageLocationError("目标目录与当前数据目录相同。")
    if target.is_relative_to(current) or current.is_relative_to(target):
        raise StorageLocationError("新旧数据目录不能互相包含。")
    if any(
        marker in part.casefold()
        for part in target.parts
        for marker in _SYNC_DIRECTORY_MARKERS
    ):
        raise StorageLocationError(
            "活动数据库不能放在 iCloud、Dropbox、OneDrive 或 Google Drive 同步目录；"
            "如需同步，请同步导出的 .jpbak 备份文件。"
        )
    if target.exists():
        raise StorageLocationError("目标目录必须尚不存在，CareerDesk 不会覆盖或合并现有目录。")
    if not target.parent.is_dir():
        raise StorageLocationError("目标目录的上级文件夹不存在，请先在 Finder 中创建。")
    if not os.access(target.parent, os.W_OK | os.X_OK):
        raise StorageLocationError("目标目录的上级文件夹不可写。")
    existing = _read_request()
    if existing is not None:
        raise StorageLocationError(
            f"已有迁移等待应用关闭：{existing['destination']}。请先关闭并重新打开 CareerDesk。"
        )
    _write_request({
        "source": str(current),
        "destination": str(target),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "phase": "requested",
    })
    return storage_state()


def cancel_pending_migration() -> dict[str, object]:
    request = _read_request()
    if request is None:
        raise StorageLocationError("当前没有等待中的数据目录迁移。")
    _migration_request_path().unlink()
    return storage_state()


def record_migration_failure(error: BaseException) -> None:
    """Persist an actionable, secret-free failure for the next settings view."""
    request = _read_request()
    if request is None:
        return
    request["issue"] = f"上次迁移未完成：{error}。旧数据目录仍在使用，未被删除或覆盖。"
    _write_request(request)


def _write_configured_data_dir(destination: Path) -> None:
    path = ENV_FILE
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StorageLocationError("配置文件不安全，拒绝切换数据目录。")
    ensure_private_directory(path.parent)
    descriptor, staged_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(staged_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(path.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        set_key(str(staged), "APP_DATA_DIR", str(destination), quote_mode="always")
        os.chmod(staged, 0o600)
        # A read-only handle cannot be flushed on Windows, and directories cannot
        # be opened there at all; both durability steps are POSIX-only semantics.
        with staged.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(staged, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def perform_pending_migration() -> Path | None:
    """Run after the server stopped and released the source data-directory lock."""
    request = _read_request()
    if request is None:
        return None
    if _customization_issue():
        raise StorageLocationError(_customization_issue() or "当前不能迁移数据目录。")
    source = canonical_data_dir(request["source"])
    destination = canonical_data_dir(request["destination"])
    if canonical_data_dir(get_settings().data_dir) != source:
        raise StorageLocationError("当前数据目录已变化，迁移请求未执行。")

    verification: Path | None = None
    try:
        if request["phase"] == "requested":
            if destination.exists():
                raise StorageLocationError("目标目录已被其他程序创建，迁移未执行。")
            descriptor, backup_name = tempfile.mkstemp(
                suffix=".jpbak",
                prefix=".careerdesk-migration-",
                dir=ENV_FILE.parent,
            )
            os.close(descriptor)
            verification = Path(backup_name)
            verification.unlink()
            create_backup(source, verification)
            restore_backup(verification, destination)
            request["phase"] = "restored"
            request.pop("issue", None)
            _write_request(request)
        else:
            if not destination.is_dir():
                raise StorageLocationError("已恢复的数据目录不再存在，未切换配置。")
            descriptor, backup_name = tempfile.mkstemp(
                suffix=".jpbak",
                prefix=".careerdesk-migration-verify-",
                dir=ENV_FILE.parent,
            )
            os.close(descriptor)
            verification = Path(backup_name)
            verification.unlink()
            create_backup(destination, verification)

        _write_configured_data_dir(destination)
        _migration_request_path().unlink()
        return destination
    finally:
        if verification is not None:
            try:
                verification.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "StorageLocationError",
    "cancel_pending_migration",
    "perform_pending_migration",
    "record_migration_failure",
    "request_data_directory_migration",
    "reveal_directory",
    "storage_state",
]
