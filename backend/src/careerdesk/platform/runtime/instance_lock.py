"""Cross-process single-instance lock scoped to a data directory.

The file carries diagnostic metadata only; the OS lock is authoritative. It remains
after release so another process cannot lock the old inode while a new file is created.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.paths import canonical_data_dir

_LOCK_FILE_NAME = ".careerdesk.instance.lock"
_LOCK_BYTE = b" "
_METADATA_OFFSET = len(_LOCK_BYTE)
_MAX_METADATA_BYTES = 4096
_UNSAFE_LOCK_FILESYSTEMS = {
    "9p", "afs", "ceph", "ceph-fuse", "cifs", "davfs", "davfs2", "exfat",
    "fakeowner", "fuse", "fuseblk", "glusterfs", "lustre", "nfs", "nfs4",
    "ntfs", "ntfs3", "osxfs", "smb3", "smbfs", "sshfs", "vfat", "virtiofs",
}
_CONTENDED_ERRNOS = {errno.EACCES, errno.EAGAIN}
if hasattr(errno, "EDEADLK"):
    _CONTENDED_ERRNOS.add(errno.EDEADLK)

_registry_guard = threading.Lock()
_held_fds: dict[str, int | None] = {}


class InstanceLockError(RuntimeError):
    """Raised when the single-instance boundary cannot be established safely."""

    def __init__(self, message: str, *, lock_path: Path):
        super().__init__(message)
        self.lock_path = lock_path


class InstanceAlreadyRunningError(InstanceLockError):
    """Raised when another live instance owns the same data directory."""

    def __init__(self, lock_path: Path, owner: dict[str, Any] | None = None):
        message = f"另一个 CareerDesk 实例正在使用数据目录：{lock_path.parent}"
        if owner and isinstance(owner.get("pid"), int):
            message += f"（PID {owner['pid']}）"
        super().__init__(message, lock_path=lock_path)
        self.owner = owner


class _LockContended(Exception):
    """Internal signal that the operating system reports a held lock."""


def _registry_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _reserve_process_slot(key: str, lock_path: Path) -> None:
    with _registry_guard:
        if key in _held_fds:
            raise InstanceAlreadyRunningError(lock_path, _read_owner_path(lock_path))
        _held_fds[key] = None


def _register_fd(key: str, fd: int) -> None:
    with _registry_guard:
        if key not in _held_fds or _held_fds[key] is not None:
            raise RuntimeError("单实例锁的进程内注册状态损坏")
        _held_fds[key] = fd


def _forget_process_slot(key: str, fd: int | None) -> None:
    with _registry_guard:
        if key not in _held_fds:
            return
        registered = _held_fds[key]
        if registered is None or fd is None or registered == fd:
            del _held_fds[key]


def _after_fork_in_child() -> None:
    """Prevent child processes from extending the parent's lock lifetime."""
    global _registry_guard

    for fd in tuple(value for value in _held_fds.values() if value is not None):
        try:
            os.close(fd)
        except OSError:
            pass
    _held_fds.clear()
    _registry_guard = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _unescape_mountinfo_path(value: str) -> str:
    """Decode the four octal escapes defined by Linux proc mountinfo."""
    return (
        value.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def _filesystem_type(path: Path) -> str | None:
    """Return the Linux mount type for ``path``; other platforms stay unknown."""
    if sys.platform != "linux":
        if os.name == "nt" and str(path).startswith("\\\\"):
            return "smbfs"
        return None
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    selected: tuple[int, str] | None = None
    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            fields = left.split()
            mountpoint = Path(_unescape_mountinfo_path(fields[4]))
            filesystem = right.split()[0].lower()
        except (IndexError, ValueError):
            continue
        if path != mountpoint and not path.is_relative_to(mountpoint):
            continue
        specificity = len(mountpoint.parts)
        if selected is None or specificity > selected[0]:
            selected = (specificity, filesystem)
    return selected[1] if selected is not None else None


def _ensure_supported_lock_filesystem(path: Path) -> None:
    """Reject mounts whose lock/private-mode semantics cannot uphold the app contract."""
    filesystem = _filesystem_type(path)
    normalized = (filesystem or "").lower()
    unsafe = normalized in _UNSAFE_LOCK_FILESYSTEMS or normalized.startswith("fuse.")
    if unsafe:
        raise InstanceLockError(
            f"数据目录位于不支持可靠单实例/私有权限契约的文件系统：{filesystem}。"
            "请使用本地磁盘上的专用目录；Docker Desktop 请使用 named volume，"
            "不要把 macOS/Windows 宿主目录 bind mount 为活动 data。",
            lock_path=path / _LOCK_FILE_NAME,
        )


def _canonical_data_dir(data_dir: str | Path) -> Path:
    raw = Path(data_dir).expanduser()
    try:
        validated = canonical_data_dir(raw)
        validated.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = validated.resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(str(resolved))
        _ensure_supported_lock_filesystem(resolved)
        # The data root contains databases, uploads, and traces, so tighten existing roots
        # without recursively changing user files. Windows uses ACLs rather than POSIX mode.
        if os.name != "nt":
            os.chmod(resolved, 0o700)
        return resolved
    except (OSError, ValueError) as error:
        fallback = raw.absolute() / _LOCK_FILE_NAME
        raise InstanceLockError(
            f"无法准备 CareerDesk 数据目录：{raw}（{error}）", lock_path=fallback,
        ) from error


def _open_lock_file(lock_path: Path) -> int:
    try:
        try:
            existing = os.lstat(lock_path)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise InstanceLockError("实例锁文件不能是符号链接", lock_path=lock_path)

        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
    except InstanceLockError:
        raise
    except OSError as error:
        raise InstanceLockError("无法安全打开实例锁文件", lock_path=lock_path) from error

    try:
        descriptor_stat = os.fstat(fd)
        path_stat = os.lstat(lock_path)
        if stat.S_ISLNK(path_stat.st_mode):
            raise InstanceLockError("实例锁文件不能是符号链接", lock_path=lock_path)
        if not stat.S_ISREG(descriptor_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise InstanceLockError("实例锁路径必须是普通文件", lock_path=lock_path)
        if not os.path.samestat(descriptor_stat, path_stat):
            raise InstanceLockError("实例锁文件在打开期间发生了替换", lock_path=lock_path)
        if descriptor_stat.st_nlink != 1 or path_stat.st_nlink != 1:
            # Metadata and chmod happen after this point.  A hard-linked lock
            # would otherwise let a pre-existing external file be overwritten
            # before startup's generic data-tree hardener gets a chance to fail.
            raise InstanceLockError("实例锁文件不能有额外硬链接", lock_path=lock_path)
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _ensure_windows_lock_byte(fd: int) -> None:
    if os.name != "nt" or os.fstat(fd).st_size:
        return
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, _LOCK_BYTE)
    os.fsync(fd)


def _lock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        _ensure_windows_lock_byte(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in _CONTENDED_ERRNOS:
                raise _LockContended from error
            raise
        return

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in _CONTENDED_ERRNOS:
            raise _LockContended from error
        raise


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _read_owner_fd(fd: int) -> dict[str, Any] | None:
    try:
        # Windows byte-range locks are mandatory. Store diagnostic JSON after locked byte
        # zero so contenders can read metadata without touching the locked range.
        os.lseek(fd, _METADATA_OFFSET, os.SEEK_SET)
        raw = os.read(fd, _MAX_METADATA_BYTES + 1)
        if len(raw) > _MAX_METADATA_BYTES:
            return None
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return _sanitize_owner(decoded)


def _read_owner_path(lock_path: Path) -> dict[str, Any] | None:
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            return None
        with lock_path.open("rb") as handle:
            handle.seek(_METADATA_OFFSET)
            raw = handle.read(_MAX_METADATA_BYTES + 1)
        if len(raw) > _MAX_METADATA_BYTES:
            return None
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return _sanitize_owner(decoded)


def _sanitize_owner(decoded: dict[str, Any]) -> dict[str, Any] | None:
    owner: dict[str, Any] = {}
    if isinstance(decoded.get("pid"), int) and decoded["pid"] > 0:
        owner["pid"] = decoded["pid"]
    for key, maximum in (("started_at", 64), ("entrypoint", 64), ("url", 256)):
        value = decoded.get(key)
        if isinstance(value, str) and len(value) <= maximum:
            owner[key] = value
    return owner or None


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("实例锁元数据写入未取得进展")
        offset += written


def _write_owner(fd: int, owner: dict[str, Any]) -> None:
    payload = json.dumps(owner, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_METADATA_BYTES:
        raise ValueError("实例锁元数据过大")
    os.lseek(fd, 0, os.SEEK_SET)
    _write_all(fd, _LOCK_BYTE + payload)
    os.ftruncate(fd, _METADATA_OFFSET + len(payload))
    os.fsync(fd)


def _clean_optional_text(value: str | None, *, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or any(ord(char) < 32 for char in cleaned):
        raise ValueError(f"{field_name} 必须是 1 到 {maximum} 个无控制字符的文本")
    return cleaned


@dataclass(slots=True)
class InstanceLock:
    """Held instance lock whose lifetime covers all process-level resources."""

    path: Path
    owner: dict[str, Any]
    _fd: int
    _key: str
    _owner_pid: int
    _released: bool = False
    _release_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def released(self) -> bool:
        return self._released

    def fileno(self) -> int:
        if self._released:
            raise ValueError("实例锁已经释放")
        return self._fd

    def release(self) -> None:
        """Release idempotently; descriptor close remains the final exception safeguard."""
        with self._release_guard:
            if self._released:
                return
            self._released = True
            fd = self._fd
            self._fd = -1
            unlock_error: OSError | None = None
            try:
                if os.getpid() == self._owner_pid:
                    try:
                        _unlock_fd(fd)
                    except OSError as error:
                        unlock_error = error
            finally:
                close_error: OSError | None = None
                try:
                    os.close(fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        close_error = error
                finally:
                    _forget_process_slot(self._key, fd)
            if unlock_error is not None:
                raise InstanceLockError(
                    "实例锁关闭时无法显式解锁", lock_path=self.path,
                ) from unlock_error
            if close_error is not None:
                raise InstanceLockError(
                    "实例锁关闭时无法关闭文件描述符", lock_path=self.path,
                ) from close_error

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


def acquire_instance_lock(
    data_dir: str | Path,
    *,
    entrypoint: str = "server",
    url: str | None = None,
) -> InstanceLock:
    """Acquire the data directory lock immediately and fail closed on uncertainty."""
    cleaned_entrypoint = _clean_optional_text(
        entrypoint, field_name="entrypoint", maximum=64,
    )
    if cleaned_entrypoint is None:
        raise ValueError("entrypoint 不能为空")
    cleaned_url = _clean_optional_text(url, field_name="url", maximum=256)

    canonical_dir = _canonical_data_dir(data_dir)
    lock_path = canonical_dir / _LOCK_FILE_NAME
    key = _registry_key(lock_path)
    _reserve_process_slot(key, lock_path)
    fd: int | None = None
    locked = False
    succeeded = False
    try:
        fd = _open_lock_file(lock_path)
        _register_fd(key, fd)
        try:
            _lock_fd(fd)
        except _LockContended as error:
            owner = _read_owner_fd(fd)
            raise InstanceAlreadyRunningError(lock_path, owner) from error
        locked = True

        if os.name != "nt":
            os.fchmod(fd, 0o600)
        owner: dict[str, Any] = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "entrypoint": cleaned_entrypoint,
        }
        if cleaned_url is not None:
            owner["url"] = cleaned_url
        _write_owner(fd, owner)
        result = InstanceLock(lock_path, owner, fd, key, os.getpid())
        succeeded = True
        return result
    except InstanceAlreadyRunningError:
        raise
    except InstanceLockError:
        raise
    except (OSError, ValueError) as error:
        raise InstanceLockError(
            "无法建立 CareerDesk 单实例锁", lock_path=lock_path,
        ) from error
    finally:
        if fd is not None and not succeeded:
            if locked:
                try:
                    _unlock_fd(fd)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
        if not succeeded:
            _forget_process_slot(key, fd)
