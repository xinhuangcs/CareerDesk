"""Offline maintenance CLI for verified, non-destructive data backups."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .console import configure_console_streams
from .desktop import configured_data_dir
from ..platform.database.backup import (
    BackupError,
    BackupSummary,
    create_backup,
    restore_backup,
    verify_backup,
)
from ..platform.runtime.instance_lock import InstanceLockError


def _size_text(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _print_summary(label: str, summary: BackupSummary) -> None:
    print(f"{label}：{summary.path}")
    print(
        f"创建时间 {summary.created_at}；{summary.file_count} 个文件；"
        f"未压缩数据 {_size_text(summary.total_bytes)}",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="careerdesk-data",
        description="创建、校验或非破坏性恢复 CareerDesk 本地业务数据",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser(
        "backup",
        help="应用完全关闭后创建 checksum 完整备份",
    )
    backup.add_argument("output", type=Path, help="新的 .jpbak 文件；拒绝覆盖")
    backup.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="源数据目录；默认使用当前 CareerDesk 配置",
    )

    verify = commands.add_parser("verify", help="离线校验备份、数据库与全部文件")
    verify.add_argument("backup", type=Path)

    restore = commands.add_parser(
        "restore",
        help="校验后原子恢复到一个全新的数据目录",
    )
    restore.add_argument("backup", type=Path)
    restore.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="必须尚不存在；绝不覆盖或合并现有数据",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console_streams()
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "backup":
            data_dir = arguments.data_dir or configured_data_dir()
            summary = create_backup(data_dir, arguments.output)
            _print_summary("备份已完成并验证", summary)
            print("现在可以同步或复制这个完整的 .jpbak 文件。")
            return 0
        if arguments.command == "verify":
            summary = verify_backup(arguments.backup)
            _print_summary("备份校验通过", summary)
            return 0
        if arguments.command == "restore":
            summary = restore_backup(arguments.backup, arguments.destination)
            _print_summary("恢复已原子写入新数据目录", summary)
            print("原数据未被修改；请显式把 APP_DATA_DIR 切换到上面的新目录。")
            return 0
    except (BackupError, InstanceLockError, OSError, ValueError) as error:
        print(f"careerdesk-data：{error}", file=sys.stderr)
        return 2
    parser.error("未知命令")


if __name__ == "__main__":
    raise SystemExit(main())
