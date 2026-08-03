"""Private on-disk storage primitives for locally managed personal data.

The helpers deliberately avoid a process-wide ``umask``: changing it is
process-global and races with unrelated threads.  New paths are born private,
while existing paths only lose group/other access (owner permissions are never
broadened).  Managed final files must be unique regular files; following a
symlink or writing through a hard link would move the trust boundary elsewhere.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class UnsafeManagedPath(ValueError):
    """A managed path is not a private regular file/directory."""


class _VolatileFileReplaced(RuntimeError):
    """A validated SQLite sidecar was replaced by another safe-shaped inode."""


def _strip_non_owner_bits(mode: int, allowed: int) -> int:
    """Remove group/other access without adding missing owner permissions."""
    return stat.S_IMODE(mode) & allowed


def _harden_existing_directory(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeManagedPath(f"受管目录不能是符号链接：{path}")
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeManagedPath(f"受管目录路径不是目录：{path}")
    if os.name == "posix":
        tightened = _strip_non_owner_bits(info.st_mode, PRIVATE_DIRECTORY_MODE)
        if tightened != stat.S_IMODE(info.st_mode):
            os.chmod(path, tightened)


def ensure_private_directory(path: str | Path, *, resolve: bool = False) -> Path:
    """Create/harden one managed directory and return its canonical path.

    ``resolve=True`` is reserved for a configured storage root: it preserves a
    user-selected root symlink while applying the managed boundary to its real
    target.  Internal managed directories should already be below that root and
    therefore keep the default fail-closed symlink behavior.
    """
    requested = Path(path).expanduser()
    target = requested.resolve(strict=False) if resolve else requested.absolute()
    if target.parent == target:
        # A malformed standalone DB/data path must never turn a helper call into
        # chmod(2) against '/' or a Windows drive root.
        raise UnsafeManagedPath(f"受管目录不能是文件系统根：{target}")

    missing: list[Path] = []
    cursor = target
    while not os.path.lexists(cursor):
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent

    for directory in reversed(missing):
        try:
            os.mkdir(directory, PRIVATE_DIRECTORY_MODE)
            if os.name == "posix":
                # A restrictive caller umask may remove owner bits.  This path
                # is new, so setting the intended owner-only mode broadens no
                # pre-existing user policy and never creates a public window.
                os.chmod(directory, PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            # A concurrent creator is acceptable only if it produced the same
            # safe directory shape.
            _harden_existing_directory(directory)

    _harden_existing_directory(target)
    return target


def _validate_regular_file(info: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeManagedPath(f"敏感文件不能是符号链接：{path}")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeManagedPath(f"敏感文件必须是普通文件：{path}")
    # An opened SQLite WAL can legitimately reach link-count zero when the last
    # connection concurrently unlinks it.  Only counts above one represent an
    # externally reachable hard-link alias and cross the managed boundary.
    if info.st_nlink > 1:
        raise UnsafeManagedPath(f"敏感文件不能有多个硬链接：{path}")


def _harden_existing_file(path: Path, *, retry_volatile_replacement: bool = False) -> None:
    """Validate and tighten one existing file without replacing its inode.

    The lstat/open/fstat/lstat sequence is intentional.  ``O_NOFOLLOW`` is not
    available on every supported platform (notably Windows), and a path can be
    replaced between any two calls.  No chmod happens until the descriptor and
    current directory entry are proven to identify the same single-link file.
    """
    before = path.lstat()
    _validate_regular_file(before, path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        # A replacement may have happened after the first lstat.  Give unsafe
        # shapes a stable application error; preserve ordinary permission/I/O.
        try:
            current = path.lstat()
        except OSError:
            raise error
        _validate_regular_file(current, path)
        raise error
    try:
        descriptor_info = os.fstat(descriptor)
        current = path.lstat()
        _validate_regular_file(descriptor_info, path)
        _validate_regular_file(current, path)
        if not os.path.samestat(descriptor_info, current):
            if retry_volatile_replacement:
                # Both sides have already passed the regular/single-link proof.
                # A WAL/SHM may have been legitimately recreated; only its
                # dedicated caller may restart validation from the beginning.
                raise _VolatileFileReplaced
            raise UnsafeManagedPath(f"敏感文件在打开期间发生了替换：{path}")
        if os.name == "posix":
            tightened = _strip_non_owner_bits(descriptor_info.st_mode, PRIVATE_FILE_MODE)
            if tightened != stat.S_IMODE(descriptor_info.st_mode):
                os.fchmod(descriptor, tightened)
    finally:
        os.close(descriptor)


def canonical_private_file(path: str | Path, *, private_parent: bool = True) -> Path:
    """Return a managed final-file path without resolving that final component."""
    requested = Path(path).expanduser()
    parent = requested.parent.resolve(strict=False)
    if private_parent:
        ensure_private_directory(parent)
    return parent / requested.name


def _harden_existing_file_if_present(path: Path, *, volatile: bool = False) -> bool:
    """Harden an existing final component without following it.

    Strict callers only accept absence at the initial lstat.  Once a main
    database, config, trace, or upload file was observed, later disappearance
    and inode replacement remain errors rather than silently creating a new
    empty file.

    ``volatile=True`` is reserved for SQLite WAL/SHM sidecars.  They may be
    unlinked and recreated by the last concurrent connection between any two
    validation syscalls.  Safe-shaped recreations are revalidated from scratch
    a bounded number of times; links, hard links, special files, and sustained
    churn remain fail-closed.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return False

    if not volatile:
        _harden_existing_file(path)
        return True

    for _attempt in range(3):
        try:
            _harden_existing_file(path, retry_volatile_replacement=True)
        except FileNotFoundError:
            try:
                current = path.lstat()
            except FileNotFoundError:
                return False
            # A newly recreated regular single-link sidecar is legitimate, but
            # never trust it from this stale attempt: validate its shape, then
            # restart the complete inode-stability proof.
            _validate_regular_file(current, path)
            continue
        except _VolatileFileReplaced:
            continue
        return True
    raise UnsafeManagedPath(f"敏感文件在校验期间反复发生替换：{path}")


def prepare_private_file(path: str | Path, *, private_parent: bool = True,
                         create: bool = True) -> Path:
    """Validate/harden a sensitive file, securely creating it when requested."""
    target = canonical_private_file(path, private_parent=private_parent)
    if not create:
        _harden_existing_file_if_present(target)
        return target
    if _harden_existing_file_if_present(target):
        return target
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
    except FileExistsError:
        _harden_existing_file(target)
    except PermissionError:
        # On Windows an existing directory/special final component reaches
        # this branch. Preserve a genuine parent/ACL denial when no entry is
        # present, while giving existing unsafe shapes the stable app error.
        if not _harden_existing_file_if_present(target):
            raise
    else:
        try:
            if os.name == "posix":
                os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
    return target


def harden_private_file_if_exists(path: str | Path) -> None:
    """Tighten an existing sidecar without creating an otherwise absent file."""
    target = canonical_private_file(path, private_parent=False)
    # Only SQLite's volatile sidecars get disappearance/recreation retries.
    # Other callers retain the same strict semantics as prepare_private_file.
    volatile = target.name.endswith(("-wal", "-shm"))
    _harden_existing_file_if_present(target, volatile=volatile)


def _is_sensitive_data_root_file(name: str) -> bool:
    """Return whether a data-root entry is an application-managed secret."""
    return (
        name == ".careerdesk.instance.lock"
        or name.startswith(("careerdesk.db", "scheduler.db", "traces.jsonl"))
        or name.endswith((".db", ".db-wal", ".db-shm", ".db-journal", ".backup"))
    )


def _harden_upload_tree(directory: Path) -> None:
    """Harden every existing upload path without following directory links."""
    ensure_private_directory(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise UnsafeManagedPath(f"上传目录中不能包含符号链接：{path}")
            if stat.S_ISDIR(info.st_mode):
                _harden_upload_tree(path)
            elif stat.S_ISREG(info.st_mode):
                _harden_existing_file(path)
            else:
                raise UnsafeManagedPath(f"上传目录中不能包含特殊文件：{path}")


def harden_managed_data_tree(data_dir: str | Path) -> Path:
    """Validate and tighten all known sensitive paths below ``data_dir``.

    The configured data root may intentionally be a symlink, so only that root
    is canonicalized.  Every application-owned layer below it is checked one
    component at a time and internal symlinks fail closed.  Uploads contain
    personal documents and are fully traversed.
    """
    root = ensure_private_directory(data_dir, resolve=True)
    with os.scandir(root) as entries:
        for entry in entries:
            if _is_sensitive_data_root_file(entry.name):
                _harden_existing_file(Path(entry.path))

    uploads = ensure_private_directory(root / "uploads")
    _harden_upload_tree(uploads)
    return root


def open_private_binary_exclusive(path: str | Path, *, private_parent: bool = True) -> BinaryIO:
    """Open a brand-new sensitive file for binary writes without following links."""
    target = canonical_private_file(path, private_parent=private_parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
    if os.name == "posix":
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    return os.fdopen(descriptor, "wb")


def open_private_text_append(path: str | Path):
    """Open a validated sensitive file for UTF-8 append through its descriptor."""
    target = prepare_private_file(path)
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags)
    try:
        descriptor_info = os.fstat(descriptor)
        current = target.lstat()
        _validate_regular_file(descriptor_info, target)
        _validate_regular_file(current, target)
        if not os.path.samestat(descriptor_info, current):
            raise UnsafeManagedPath(f"敏感文件在追加打开期间发生了替换：{target}")
    except Exception:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "a", encoding="utf-8")
