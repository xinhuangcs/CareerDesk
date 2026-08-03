"""Portable, checksummed backups for the complete CareerDesk business-data root.

The package deliberately includes only the SQLite truth source plus irreplaceable
uploads.  Configuration, OS credentials, logs, traces,
instance-lock metadata, and old development snapshots stay outside this boundary.

Restore is non-destructive: a fully verified package is atomically installed at a
new data-root path.  Existing paths are never merged, replaced, or removed.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
import errno
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import BinaryIO, Iterator
from uuid import uuid4
import zipfile

from ..runtime.instance_lock import acquire_instance_lock
from ..storage.private import harden_managed_data_tree, prepare_private_file
from ...core.paths import canonical_data_dir
from . import schema as database_schema


BACKUP_SCHEMA_VERSION = 2
BACKUP_EXTENSION = ".jpbak"
DATABASE_PATH = "careerdesk.db"
MANIFEST_PATH = "manifest.json"
MANAGED_DIRECTORIES = ("uploads",)
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    """A backup package or requested backup/restore operation is unsafe."""


@dataclass(frozen=True, slots=True)
class BackupSummary:
    path: Path
    created_at: str
    file_count: int
    total_bytes: int


def _application_version() -> str:
    try:
        return package_version("careerdesk")
    except PackageNotFoundError:
        return "development"


def _private_mode(info: os.stat_result) -> int:
    return 0o700 if info.st_mode & stat.S_IXUSR else 0o600


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BackupError("备份清单包含无效路径")
    pure = PurePosixPath(value)
    parts = value.split("/")
    if (
        "\\" in value
        or "\x00" in value
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise BackupError("备份清单包含不安全路径")
    return value


def _strict_json(payload: bytes) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BackupError(f"备份清单包含重复字段：{key}")
            result[key] = value
        return result

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except BackupError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupError("备份清单不是有效的 UTF-8 JSON") from error


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise BackupError("备份清单缺少有效创建时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BackupError("备份清单创建时间无效") from error
    if parsed.tzinfo is None:
        raise BackupError("备份清单创建时间必须包含时区")
    return value


def _validate_manifest(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "product",
        "app_version",
        "created_at",
        "database_schema_version",
        "entries",
    }:
        raise BackupError("备份清单顶层字段不完整或包含未知字段")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != BACKUP_SCHEMA_VERSION
    ):
        raise BackupError("备份包版本不受当前程序支持")
    if raw["product"] != "CareerDesk":
        raise BackupError("该文件不是 CareerDesk 备份包")
    if (
        not isinstance(raw["app_version"], str)
        or not raw["app_version"]
        or len(raw["app_version"]) > 64
    ):
        raise BackupError("备份清单应用版本无效")
    _validate_timestamp(raw["created_at"])
    if (
        type(raw["database_schema_version"]) is not int
        or raw["database_schema_version"] != database_schema.SCHEMA_VERSION
    ):
        raise BackupError(
            "备份数据库版本与当前程序不兼容，不能直接恢复",
        )

    entries = raw["entries"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise BackupError("备份清单条目数量无效或超出安全上限")
    seen: set[str] = set()
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError("备份清单条目必须是对象")
        path = _safe_relative_path(entry.get("path"))
        if path in seen:
            raise BackupError(f"备份清单包含重复路径：{path}")
        seen.add(path)
        kind = entry.get("kind")
        if kind == "file":
            if set(entry) != {"path", "kind", "mode", "size", "sha256"}:
                raise BackupError(f"文件条目字段无效：{path}")
            mode = entry["mode"]
            size = entry["size"]
            digest = entry["sha256"]
            if mode not in {0o600, 0o700} or type(size) is not int or size < 0:
                raise BackupError(f"文件条目权限或大小无效：{path}")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise BackupError(f"文件条目 checksum 无效：{path}")
            total_bytes += size
            if total_bytes > MAX_UNCOMPRESSED_BYTES:
                raise BackupError("备份解压后大小超出安全上限")
        elif kind == "directory":
            if set(entry) != {"path", "kind", "mode"} or entry["mode"] != 0o700:
                raise BackupError(f"目录条目字段无效：{path}")
        else:
            raise BackupError(f"备份清单包含未知条目类型：{path}")

    expected_roots = {DATABASE_PATH, *MANAGED_DIRECTORIES}
    if not expected_roots.issubset(seen):
        raise BackupError("备份缺少数据库或上传目录")
    database_entries = [
        entry for entry in entries if entry["path"] == DATABASE_PATH
    ]
    if len(database_entries) != 1 or database_entries[0]["kind"] != "file":
        raise BackupError("备份数据库条目无效")
    for root in MANAGED_DIRECTORIES:
        roots = [entry for entry in entries if entry["path"] == root]
        if len(roots) != 1 or roots[0]["kind"] != "directory":
            raise BackupError(f"备份受管目录条目无效：{root}")

    for path in seen:
        if (
            path != DATABASE_PATH
            and path not in MANAGED_DIRECTORIES
            and not path.startswith("uploads/")
        ):
            raise BackupError(f"备份包含边界外路径：{path}")
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            parent_text = parent.as_posix()
            if parent_text not in seen:
                raise BackupError(f"备份条目缺少父目录：{path}")
            parent = parent.parent

    raw["entries"] = sorted(entries, key=lambda item: item["path"])
    return raw


def _zip_info(path: str, *, mode: int, size: int | None = None) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=datetime.now().timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    if size is not None:
        info.file_size = size
    return info


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        os.path.samestat(before, after)
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


@contextmanager
def _open_regular_read(path: Path) -> Iterator[BinaryIO]:
    try:
        before = path.lstat()
    except OSError as error:
        raise BackupError(f"无法读取受管文件：{path}") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise BackupError(f"备份只接受单链接普通文件：{path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    handle = os.fdopen(descriptor, "rb")
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not _same_file_snapshot(before, opened) or not os.path.samestat(opened, current):
            raise BackupError(f"文件在安全打开期间发生替换：{path}")
        yield handle
        after = os.fstat(descriptor)
        current = path.lstat()
        if not _same_file_snapshot(opened, after) or not os.path.samestat(after, current):
            raise BackupError(f"文件在备份读取期间发生变化：{path}")
    finally:
        handle.close()


def _archive_regular_file(
    archive: zipfile.ZipFile,
    source: Path,
    archive_path: str,
    *,
    mode: int | None = None,
) -> dict[str, object]:
    digest = hashlib.sha256()
    total = 0
    with _open_regular_read(source) as input_file:
        source_info = os.fstat(input_file.fileno())
        effective_mode = mode if mode is not None else _private_mode(source_info)
        info = _zip_info(archive_path, mode=effective_mode, size=source_info.st_size)
        with archive.open(info, "w") as output_file:
            while chunk := input_file.read(_CHUNK_SIZE):
                output_file.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    if total != source_info.st_size:
        raise BackupError(f"文件长度在备份期间发生变化：{source}")
    return {
        "path": archive_path,
        "kind": "file",
        "mode": effective_mode,
        "size": total,
        "sha256": digest.hexdigest(),
    }


def _scan_tree(
    archive: zipfile.ZipFile,
    directory: Path,
    archive_path: str,
    entries: list[dict[str, object]],
) -> None:
    before = directory.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise BackupError(f"受管备份根不是安全目录：{directory}")
    entries.append({"path": archive_path, "kind": "directory", "mode": 0o700})
    try:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
    except OSError as error:
        raise BackupError(f"无法枚举受管目录：{directory}") from error
    for child in children:
        path = Path(child.path)
        child_archive_path = f"{archive_path}/{child.name}"
        _safe_relative_path(child_archive_path)
        info = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            _scan_tree(archive, path, child_archive_path, entries)
        elif stat.S_ISREG(info.st_mode):
            entries.append(_archive_regular_file(archive, path, child_archive_path))
        else:
            raise BackupError(f"受管目录包含不支持的链接或特殊文件：{path}")
    after = directory.lstat()
    if not os.path.samestat(before, after) or before.st_mtime_ns != after.st_mtime_ns:
        raise BackupError(f"目录在备份期间发生变化：{directory}")


def _validate_database(path: Path) -> None:
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchmany(2)
            if integrity_rows != [("ok",)]:
                raise BackupError("数据库 integrity_check 未通过")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("数据库存在外键一致性错误")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != database_schema.SCHEMA_VERSION:
                raise BackupError(
                    f"数据库版本 v{version} 与当前 v{database_schema.SCHEMA_VERSION} 不兼容",
                )
            database_schema.assert_current_schema_manifest(
                connection,
                allow_missing_derived=False,
            )
    except BackupError:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as error:
        raise BackupError("数据库结构或内容校验失败") from error


def _snapshot_database(source: Path, destination: Path) -> None:
    _validate_database(source)
    prepare_private_file(destination)
    try:
        with closing(
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=30)
        ) as source_connection, closing(sqlite3.connect(destination)) as target_connection:
            source_connection.backup(target_connection)
    except sqlite3.Error as error:
        raise BackupError("SQLite Online Backup 失败") from error
    if os.name == "posix":
        os.chmod(destination, 0o600)
    _validate_database(destination)


def _manifest_summary(path: Path, manifest: dict[str, object]) -> BackupSummary:
    entries = manifest["entries"]
    files = [entry for entry in entries if entry["kind"] == "file"]
    return BackupSummary(
        path=path,
        created_at=str(manifest["created_at"]),
        file_count=len(files),
        total_bytes=sum(int(entry["size"]) for entry in files),
    )


def _write_private_exclusive(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    if os.name == "posix":
        os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "wb")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_archive(staging: Path, destination: Path) -> None:
    try:
        os.link(staging, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise BackupError(f"备份目标已存在，拒绝覆盖：{destination}") from error
    except OSError as error:
        raise BackupError(
            "无法以原子且不覆盖的方式发布备份；请改用支持硬链接的本地磁盘目录，"
            "完成后再同步 .jpbak 文件",
        ) from error
    try:
        _fsync_directory(destination.parent)
        staging.unlink()
        _fsync_directory(destination.parent)
    except OSError as error:
        raise BackupError("备份已生成，但无法完成临时名称清理/目录持久化") from error


def _canonical_output(path: str | Path) -> Path:
    requested = Path(path).expanduser()
    if requested.suffix.casefold() != BACKUP_EXTENSION:
        raise BackupError(f"备份文件必须使用 {BACKUP_EXTENSION} 扩展名")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as error:
        raise BackupError("备份目标的父目录必须已经存在") from error
    if not parent.is_dir():
        raise BackupError("备份目标的父路径不是目录")
    destination = parent / requested.name
    if os.path.lexists(destination):
        raise BackupError(f"备份目标已存在，拒绝覆盖：{destination}")
    return destination


def create_backup(source_data_dir: str | Path, output_path: str | Path) -> BackupSummary:
    """Create, verify, and atomically publish one complete backup package."""
    source = canonical_data_dir(source_data_dir)
    destination = _canonical_output(output_path)
    if destination == source or destination.is_relative_to(source):
        raise BackupError("备份文件不能写入活动数据目录")
    database = source / DATABASE_PATH
    if not database.is_file():
        raise BackupError(f"数据目录缺少 CareerDesk 数据库：{database}")

    staging = destination.parent / f".{destination.name}.partial-{uuid4().hex}"
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with acquire_instance_lock(source, entrypoint="backup"):
            root = harden_managed_data_tree(source)
            with tempfile.TemporaryDirectory(prefix="careerdesk-backup-") as work:
                database_snapshot = Path(work) / DATABASE_PATH
                _snapshot_database(root / DATABASE_PATH, database_snapshot)
                with _write_private_exclusive(staging) as handle:
                    with zipfile.ZipFile(
                        handle,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                        compresslevel=6,
                        allowZip64=True,
                    ) as archive:
                        entries = [
                            _archive_regular_file(
                                archive,
                                database_snapshot,
                                DATABASE_PATH,
                                mode=0o600,
                            ),
                        ]
                        for name in MANAGED_DIRECTORIES:
                            _scan_tree(archive, root / name, name, entries)
                        manifest = {
                            "schema_version": BACKUP_SCHEMA_VERSION,
                            "product": "CareerDesk",
                            "app_version": _application_version(),
                            "created_at": created_at,
                            "database_schema_version": database_schema.SCHEMA_VERSION,
                            "entries": sorted(entries, key=lambda item: item["path"]),
                        }
                        manifest_bytes = (
                            json.dumps(
                                manifest,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ) + "\n"
                        ).encode("utf-8")
                        archive.writestr(
                            _zip_info(MANIFEST_PATH, mode=0o600, size=len(manifest_bytes)),
                            manifest_bytes,
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
        verified = verify_backup(staging)
        _publish_archive(staging, destination)
        return BackupSummary(
            path=destination,
            created_at=verified.created_at,
            file_count=verified.file_count,
            total_bytes=verified.total_bytes,
        )
    except Exception:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _archive_manifest(archive: zipfile.ZipFile) -> dict[str, object]:
    members = archive.infolist()
    names = [member.filename for member in members]
    if len(names) != len(set(names)):
        raise BackupError("备份 ZIP 包含重复路径")
    manifest_info = next((item for item in members if item.filename == MANIFEST_PATH), None)
    if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
        raise BackupError("备份缺少清单或清单过大")
    if manifest_info.is_dir() or manifest_info.flag_bits & 0x1:
        raise BackupError("备份清单不能是目录或加密条目")
    try:
        payload = archive.read(manifest_info)
    except (OSError, zipfile.BadZipFile) as error:
        raise BackupError("无法读取备份清单") from error
    return _validate_manifest(_strict_json(payload))


def _prepare_restore_directories(root: Path, entries: list[dict[str, object]]) -> None:
    directories = [entry for entry in entries if entry["kind"] == "directory"]
    for entry in sorted(directories, key=lambda item: len(PurePosixPath(item["path"]).parts)):
        target = root.joinpath(*PurePosixPath(entry["path"]).parts)
        target.mkdir(mode=0o700)
        if os.name == "posix":
            os.chmod(target, 0o700)


def _extract_file(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    entry: dict[str, object],
    destination: Path | None,
) -> None:
    digest = hashlib.sha256()
    total = 0
    output: BinaryIO | None = None
    if destination is not None:
        output = _write_private_exclusive(destination)
    try:
        with archive.open(member, "r") as source:
            while chunk := source.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > int(entry["size"]):
                    raise BackupError(f"备份条目长度超过清单：{entry['path']}")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
        if total != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise BackupError(f"备份条目 checksum 不匹配：{entry['path']}")
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
            if os.name == "posix":
                os.fchmod(output.fileno(), int(entry["mode"]))
    except (OSError, zipfile.BadZipFile) as error:
        raise BackupError(f"无法安全读取备份条目：{entry['path']}") from error
    finally:
        if output is not None:
            output.close()


def _verify_archive(
    backup_path: Path,
    *,
    extract_root: Path | None = None,
) -> dict[str, object]:
    temporary_database: tempfile.TemporaryDirectory[str] | None = None
    try:
        with _open_regular_read(backup_path) as handle, zipfile.ZipFile(handle, "r") as archive:
            manifest = _archive_manifest(archive)
            entries = manifest["entries"]
            file_entries = {
                entry["path"]: entry for entry in entries if entry["kind"] == "file"
            }
            members = {member.filename: member for member in archive.infolist()}
            if set(members) != {MANIFEST_PATH, *file_entries}:
                raise BackupError("备份 ZIP 内容与 checksum 清单不一致")
            for path, entry in file_entries.items():
                member = members[path]
                mode = member.external_attr >> 16
                if (
                    member.is_dir()
                    or member.flag_bits & 0x1
                    or stat.S_ISLNK(mode)
                    or member.file_size != entry["size"]
                ):
                    raise BackupError(f"备份 ZIP 条目形状无效：{path}")

            database_target: Path
            if extract_root is not None:
                _prepare_restore_directories(extract_root, entries)
                database_target = extract_root / DATABASE_PATH
            else:
                temporary_database = tempfile.TemporaryDirectory(
                    prefix="careerdesk-backup-verify-",
                )
                database_target = Path(temporary_database.name) / DATABASE_PATH

            for path, entry in sorted(file_entries.items()):
                destination = None
                if extract_root is not None:
                    destination = extract_root.joinpath(*PurePosixPath(path).parts)
                elif path == DATABASE_PATH:
                    destination = database_target
                _extract_file(archive, members[path], entry, destination)

            _validate_database(database_target)
            return manifest
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise BackupError("备份文件不是有效、完整的 ZIP64 包") from error
    finally:
        if temporary_database is not None:
            temporary_database.cleanup()


def verify_backup(backup_path: str | Path) -> BackupSummary:
    """Verify package structure, all checksums, SQLite integrity, FKs, and schema."""
    path = Path(backup_path).expanduser().resolve(strict=True)
    manifest = _verify_archive(path)
    return _manifest_summary(path, manifest)


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a directory without POSIX rename's overwrite behavior."""
    if os.name == "nt":
        os.rename(source, destination)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename_exclusive = getattr(libc, "renamex_np", None)
        if rename_exclusive is None:
            raise BackupError("当前 macOS 不支持原子排他目录恢复")
        rename_exclusive.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(source_bytes, destination_bytes, 0x00000004)
    else:
        rename_exclusive = getattr(libc, "renameat2", None)
        if rename_exclusive is None:
            raise BackupError("当前 Linux libc 不支持原子排他目录恢复")
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(-100, source_bytes, -100, destination_bytes, 0x1)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise BackupError(f"恢复目标已存在，拒绝覆盖：{destination}")
    raise BackupError("无法原子发布已验证的恢复目录") from OSError(
        error_number,
        os.strerror(error_number),
    )


def restore_backup(
    backup_path: str | Path,
    destination_data_dir: str | Path,
) -> BackupSummary:
    """Verify and atomically install a backup at a brand-new data root."""
    backup = Path(backup_path).expanduser().resolve(strict=True)
    destination = canonical_data_dir(destination_data_dir)
    parent = destination.parent
    if not parent.exists() or not parent.is_dir():
        raise BackupError("恢复目标的父目录必须已经存在")
    if os.path.lexists(destination):
        raise BackupError(f"恢复目标已存在，拒绝覆盖或合并：{destination}")

    staging = parent / f".{destination.name}.restore-partial-{uuid4().hex}"
    os.mkdir(staging, mode=0o700)
    if os.name == "posix":
        os.chmod(staging, 0o700)
    published = False
    try:
        manifest = _verify_archive(backup, extract_root=staging)
        harden_managed_data_tree(staging)
        _validate_database(staging / DATABASE_PATH)
        _fsync_directory(staging)
        _rename_directory_noreplace(staging, destination)
        published = True
        _fsync_directory(parent)
        return _manifest_summary(destination, manifest)
    finally:
        if not published and os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "BACKUP_EXTENSION",
    "BACKUP_SCHEMA_VERSION",
    "BackupError",
    "BackupSummary",
    "create_backup",
    "restore_backup",
    "verify_backup",
]
