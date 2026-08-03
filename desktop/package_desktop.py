"""Build a self-contained macOS/Windows desktop artifact from a CareerDesk wheel.

The caller supplies a freshly built wheel and a new output directory.  This
script never removes or overwrites an existing path.  PyInstaller runs in
one-folder/windowed mode, so the artifact contains Python and dependencies but
does not unpack executable libraries into a temporary directory on each start.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import zipfile

if __package__:
    from .legal_notices import stage_legal_bundle
else:  # direct ``python desktop/package_desktop.py`` invocation
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from desktop.legal_notices import stage_legal_bundle


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "desktop" / "careerdesk.spec"
SUPPORTED_PLATFORMS = {"darwin": "macos", "win32": "windows"}
_REQUIRED_SKILL_NAMES = (
    "prepare-for-interview",
    "emotional-support",
)
_REQUIRED_PACKAGE_FILES = (
    "careerdesk/__init__.py",
    "careerdesk/bootstrap/console.py",
    "careerdesk/bootstrap/desktop.py",
    "careerdesk/bootstrap/cli.py",
    "careerdesk/platform/database/backup.py",
    "careerdesk/default.env",
    "careerdesk/frontend_dist/index.html",
    "careerdesk/frontend_dist/legal/node/index.json",
)
_SENSITIVE_ENV = re.compile(
    r"(?:^|_)(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)",
    re.IGNORECASE,
)
_BUILD_ENV_ALLOWLIST = frozenset({
    "PATH",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "CI",
    "DEVELOPER_DIR",
    "SDKROOT",
    "MACOSX_DEPLOYMENT_TARGET",
    "ARCHFLAGS",
})


@dataclass(frozen=True, slots=True)
class WheelIdentity:
    name: str
    version: str


def _new_output_root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if candidate.exists() or os.path.lexists(candidate):
        raise ValueError(f"输出目录必须尚不存在，避免覆盖或删除已有文件：{candidate}")
    candidate.mkdir(parents=True, mode=0o700)
    return candidate


def _safe_wheel_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    names = [member.filename for member in members]
    if len(names) != len(set(names)):
        raise ValueError("wheel 包含重复路径，已拒绝解包")
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        raw_parts = name.rstrip("/").split("/")
        mode = member.external_attr >> 16
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or any(not part or part == "." or ":" in part for part in raw_parts)
            or stat.S_ISLNK(mode)
        ):
            raise ValueError("wheel 包含不安全路径或符号链接，已拒绝解包")
    return members


def _wheel_identity(archive: zipfile.ZipFile) -> WheelIdentity:
    metadata_names = [
        member.filename
        for member in archive.infolist()
        if member.filename.endswith(".dist-info/METADATA")
    ]
    if len(metadata_names) != 1:
        raise ValueError("wheel 必须包含且只包含一份 dist-info/METADATA")
    message = BytesParser(policy=email_policy).parsebytes(
        archive.read(metadata_names[0])
    )
    name = str(message.get("Name", "")).strip()
    version = str(message.get("Version", "")).strip()
    if name.lower() != "careerdesk" or not version:
        raise ValueError("只接受带有效版本的 CareerDesk wheel")
    return WheelIdentity(name=name, version=version)


def stage_wheel(wheel: Path, site: Path) -> WheelIdentity:
    """Safely extract one CareerDesk wheel into a new PyInstaller source root."""
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError(f"wheel 不存在或扩展名无效：{wheel}")
    site.mkdir(parents=True, mode=0o700)
    with zipfile.ZipFile(wheel) as archive:
        members = _safe_wheel_members(archive)
        identity = _wheel_identity(archive)
        names = {member.filename for member in members}
        required = {
            *_REQUIRED_PACKAGE_FILES,
            *(
                f"careerdesk/agentic/skills/{name}/{filename}"
                for name in _REQUIRED_SKILL_NAMES
                for filename in ("SKILL.md", "SKILL.en.md")
            ),
        }
        if not required.issubset(names):
            missing = ", ".join(sorted(required - names))
            raise ValueError(f"wheel 缺少安装式只读资源：{missing}")
        if not any(
            name.startswith("careerdesk/frontend_dist/assets/")
            and not name.endswith("/")
            for name in names
        ):
            raise ValueError("wheel 缺少 frontend hashed assets")

        for member in members:
            relative = PurePosixPath(member.filename)
            destination = site.joinpath(*relative.parts)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
    return identity


def _windows_version_file(version: str, destination: Path) -> None:
    numbers = version.split("+")[0].split("-")[0].split(".")
    if not 1 <= len(numbers) <= 4 or any(not item.isdigit() for item in numbers):
        raise ValueError(f"Windows 包版本必须是 1 到 4 段数字：{version}")
    parts = tuple(int(item) for item in numbers) + (0,) * (4 - len(numbers))
    dotted = ".".join(str(item) for item in parts)
    destination.write_text(
        "# UTF-8\n"
        "VSVersionInfo(ffi=FixedFileInfo("
        f"filevers={parts}, prodvers={parts}, mask=0x3f, flags=0x0, "
        "OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)), "
        "kids=[StringFileInfo([StringTable('040904B0', ["
        f"StringStruct('CompanyName', 'CareerDesk'), "
        f"StringStruct('FileDescription', 'CareerDesk career assistant'), "
        f"StringStruct('FileVersion', '{dotted}'), "
        f"StringStruct('InternalName', 'CareerDesk'), "
        f"StringStruct('OriginalFilename', 'CareerDesk.exe'), "
        f"StringStruct('ProductName', 'CareerDesk'), "
        f"StringStruct('ProductVersion', '{dotted}')"
        "])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n",
        encoding="utf-8",
    )


def _build_environment(
    *,
    site: Path,
    version: str,
    windows_version_file: Path | None,
    legal_dir: Path,
) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _BUILD_ENV_ALLOWLIST and not _SENSITIVE_ENV.search(name)
    }
    environment.update({
        "CAREERDESK_FROZEN_SITE": str(site),
        "CAREERDESK_BUILD_VERSION": version,
        "CAREERDESK_LEGAL_DIR": str(legal_dir),
        "PYTHONHASHSEED": "0",
        # Ensure spec-time package discovery sees the wheel, not the editable checkout.
        "PYTHONPATH": str(site),
    })
    if windows_version_file is not None:
        environment["CAREERDESK_WINDOWS_VERSION_FILE"] = str(windows_version_file)
    else:
        environment.pop("CAREERDESK_WINDOWS_VERSION_FILE", None)
    return environment


def _configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                continue


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_paths(dist: Path, platform_name: str) -> tuple[Path, Path, Path]:
    if platform_name == "darwin":
        artifact = dist / "CareerDesk.app"
        executable = artifact / "Contents" / "MacOS" / "CareerDesk"
        data_executable = artifact / "Contents" / "MacOS" / "CareerDeskData"
    else:
        artifact = dist / "CareerDesk"
        executable = artifact / "CareerDesk.exe"
        data_executable = artifact / "CareerDeskData.exe"
    if (
        not artifact.is_dir()
        or not executable.is_file()
        or not data_executable.is_file()
    ):
        raise RuntimeError("PyInstaller 未生成预期的桌面/数据维护可执行文件")
    return artifact, executable, data_executable


def _verify_bundled_resources(artifact: Path, platform_name: str) -> None:
    # macOS app bundles expose Resources through Frameworks symlinks.  Count
    # resolved physical targets so that the standard bundle view is not
    # mistaken for duplicated mutable resources.
    default_env = {path.resolve() for path in artifact.rglob("careerdesk/default.env")}
    indexes = {path.resolve() for path in artifact.rglob("careerdesk/frontend_dist/index.html")}
    skill_targets = {
        (name, filename): {
            path.resolve()
            for path in artifact.rglob(
                f"careerdesk/agentic/skills/{name}/{filename}"
            )
        }
        for name in _REQUIRED_SKILL_NAMES
        for filename in ("SKILL.md", "SKILL.en.md")
    }
    project_licenses = {
        path.resolve() for path in artifact.rglob("Legal/CareerDesk/LICENSE")
    }
    python_notice_indexes = {
        path.resolve()
        for path in artifact.rglob("Legal/ThirdParty/Python/index.json")
    }
    node_notice_indexes = {
        path.resolve()
        for path in artifact.rglob("careerdesk/frontend_dist/legal/node/index.json")
    }
    runtime_resources = (
        "magika/config/content_types_kb.min.json",
        "magika/models/standard_v3_3/model.onnx",
        "magika/models/standard_v3_3/config.min.json",
        "magika/models/standard_v3_3/metadata.json",
        f"sqlite_vec/{'vec0.dylib' if platform_name == 'darwin' else 'vec0.dll'}",
    )
    runtime_targets = {
        relative: {
            path.resolve()
            for path in artifact.rglob(relative)
        }
        for relative in runtime_resources
    }
    if (
        len(default_env) != 1
        or len(indexes) != 1
        or any(len(targets) != 1 for targets in skill_targets.values())
        or len(project_licenses) != 1
        or len(python_notice_indexes) != 1
        or len(node_notice_indexes) != 1
        or any(len(targets) != 1 for targets in runtime_targets.values())
    ):
        raise RuntimeError(
            "桌面包没有且仅有一份默认配置、前端、内置 Skill、文档识别模型、"
            "向量扩展或法律/第三方许可资源"
        )
    forbidden = [
        path
        for path in artifact.rglob("*")
        if path.name in {"uv", "uv.exe", "npm", "npm.cmd", "package.json"}
    ]
    if forbidden:
        raise RuntimeError("桌面包意外依赖 uv/npm 或包含前端源码清单")


def _run_bundled_self_test(executable: Path, artifact: Path) -> None:
    """Execute resource-backed dependencies from the final frozen layout."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _BUILD_ENV_ALLOWLIST and not _SENSITIVE_ENV.search(name)
    }
    environment.update({
        "CAREERDESK_PACKAGE_SELF_TEST": "1",
        "PYTHONHASHSEED": "0",
    })
    try:
        subprocess.run(
            [str(executable)],
            cwd=artifact,
            env=environment,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            "桌面包运行时自检失败：文档识别模型或向量扩展不可用"
        ) from error


def build(
    *,
    wheel: Path,
    output_dir: Path,
    platform_name: str | None = None,
    codesign_identity: str | None = None,
) -> Path:
    host = platform_name or sys.platform
    if host not in SUPPORTED_PLATFORMS:
        raise ValueError("正式桌面包目前只在 macOS 或 Windows 原生构建")
    if codesign_identity is not None:
        if host != "darwin":
            raise ValueError("--codesign-identity 只用于 macOS 桌面包")
        if not codesign_identity.strip():
            raise ValueError("--codesign-identity 不能是空白")
    output = _new_output_root(output_dir)
    site = output / "wheel-site"
    legal = output / "legal"
    work = output / "pyinstaller-work"
    dist = output / "dist"
    identity = stage_wheel(wheel.resolve(), site)
    stage_legal_bundle(repository_root=ROOT, destination=legal)

    windows_version = None
    if host == "win32":
        windows_version = output / "windows-version.txt"
        _windows_version_file(identity.version, windows_version)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--log-level=WARN",
        f"--distpath={dist}",
        f"--workpath={work}",
        str(SPEC),
    ]
    subprocess.run(
        command,
        cwd=ROOT,
        env=_build_environment(
            site=site,
            version=identity.version,
            windows_version_file=windows_version,
            legal_dir=legal,
        ),
        check=True,
    )
    artifact, executable, data_executable = _artifact_paths(dist, host)
    if codesign_identity is not None:
        # PyInstaller identity-signs only the launchers while collected dylibs
        # stay ad-hoc; dyld rejects that mix once the launcher signature is no
        # longer ad-hoc.  Re-sign the whole bundle uniformly, before the
        # self-test exercises the signed layout.
        subprocess.run(
            [
                "codesign",
                "--force",
                "--deep",
                "--timestamp=none",
                "--sign",
                codesign_identity,
                str(artifact),
            ],
            check=True,
        )
    _verify_bundled_resources(artifact, host)
    _run_bundled_self_test(data_executable, artifact)

    manifest = {
        "schema_version": 1,
        "product": identity.name,
        "version": identity.version,
        "platform": SUPPORTED_PLATFORMS[host],
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "format": "pyinstaller-onedir-windowed",
        "artifact": str(artifact.relative_to(output)),
        "executable": str(executable.relative_to(output)),
        "data_executable": str(data_executable.relative_to(output)),
        "wheel_sha256": _sha256(wheel),
        "executable_sha256": _sha256(executable),
        "data_executable_sha256": _sha256(data_executable),
        "legal_notices": True,
        "python_notice_index_sha256": _sha256(
            legal / "ThirdParty" / "Python" / "index.json"
        ),
        "node_notice_index_sha256": _sha256(
            site / "careerdesk" / "frontend_dist" / "legal" / "node" / "index.json"
        ),
        "code_signing": (
            ("local-certificate" if codesign_identity else "ad-hoc")
            if host == "darwin"
            else "unsigned"
        ),
    }
    (output / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return artifact


def main() -> int:
    _configure_console_output()
    parser = argparse.ArgumentParser(
        description="从已构建 CareerDesk wheel 生成当前平台的自包含桌面包",
    )
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--codesign-identity",
        default=None,
        help="macOS 专用：用本机钥匙串中的固定签名证书替代 ad-hoc 签名",
    )
    arguments = parser.parse_args()
    artifact = build(
        wheel=arguments.wheel,
        output_dir=arguments.output_dir,
        codesign_identity=arguments.codesign_identity,
    )
    print(f"CareerDesk 桌面包已生成：{artifact}")
    if arguments.codesign_identity:
        print("已使用本机自建证书签名；产物仍未经 Apple 公证，对外分发必须明确标记 UNSIGNED。")
    else:
        print("当前产物未使用平台发布证书；对外分发时必须明确标记 UNSIGNED。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
