"""Upload storage boundary for user isolation, chunk limits, and failure cleanup."""

from hashlib import sha256
from pathlib import Path
from time import time
from typing import BinaryIO
from uuid import uuid4

from .private import ensure_private_directory, open_private_binary_exclusive


COPY_CHUNK_BYTES = 1024 * 1024
UPLOAD_NAME_ATTEMPTS = 3
MAX_CHAT_OR_RESUME_BYTES = 10 * 1024 * 1024
MAX_CHAT_STORAGE_BYTES = 100 * 1024 * 1024
MAX_RESUME_STORAGE_BYTES = 500 * 1024 * 1024
CHAT_UPLOAD_TTL_SECONDS = 24 * 60 * 60


class UploadTooLarge(ValueError):
    """Upload exceeds the endpoint byte limit."""


def user_storage_key(user_id: str) -> str:
    """Convert user identity into a stable directory name without source leakage."""
    return sha256(user_id.encode("utf-8")).hexdigest()[:20]


def user_upload_root(data_dir: str | Path, category: str, user_id: str) -> Path:
    """Create the managed upload path layer by layer, rejecting symlinks."""
    if (not category or category != category.strip() or category in (".", "..")
            or "/" in category or "\\" in category
            or any(ord(character) < 32 for character in category)):
        raise ValueError("非法上传目录")
    root = ensure_private_directory(data_dir, resolve=True)
    uploads = ensure_private_directory(root / "uploads")
    category_root = ensure_private_directory(uploads / category)
    return ensure_private_directory(category_root / user_storage_key(user_id))


def copy_limited(source: BinaryIO, destination: Path, max_bytes: int) -> int:
    """Copy in chunks, failing at the first overflow and deleting partial files."""
    written = 0
    created = False
    try:
        with open_private_binary_exclusive(destination) as output:
            created = True
            while chunk := source.read(COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    limit = (f"{max_bytes // (1024 * 1024)} MB" if max_bytes >= 1024 * 1024
                             else f"{max_bytes // 1000} KB")
                    raise UploadTooLarge(f"文件不能超过 {limit}")
                output.write(chunk)
    except Exception:
        # O_EXCL rejects a collision (including a symlink) before creation.
        # Never unlink that pre-existing directory entry on the failure path.
        if created:
            destination.unlink(missing_ok=True)
        raise
    return written


def cleanup_stale_files(root: Path, max_age_seconds: int) -> int:
    """Delete stale regular files without following or removing symlinks."""
    if not root.is_dir():
        return 0
    cutoff = time() - max_age_seconds
    removed = 0
    for path in root.iterdir():
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _storage_bytes(root: Path) -> int:
    """Sum regular-file sizes in a managed flat directory without following links."""
    total = 0
    if not root.is_dir():
        return total
    for path in root.iterdir():
        try:
            if not path.is_symlink() and path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def save_upload(source: BinaryIO, filename: str | None, root: Path, max_bytes: int, *,
                max_total_bytes: int | None = None) -> Path:
    """Save a bounded upload under a random name and bounded safe basename."""
    original = Path(filename or "file").name
    suffix = Path(original).suffix[:20]
    stem = Path(original).stem[:80] or "file"
    for _attempt in range(UPLOAD_NAME_ATTEMPTS):
        destination = root / f"{uuid4().hex[:16]}_{stem}{suffix}"
        try:
            copy_limited(source, destination, max_bytes)
        except FileExistsError:
            continue
        break
    else:
        raise OSError("无法分配安全的上传文件名，请重试")
    if max_total_bytes is not None and _storage_bytes(root) > max_total_bytes:
        destination.unlink(missing_ok=True)
        raise UploadTooLarge(
            f"该类文件的个人存储额度为 {max_total_bytes // (1024 * 1024)} MB，"
            "请删除不用的附件或简历后重试"
        )
    return destination
