"""Stage legal notices for self-contained desktop distributions."""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Iterable


_PROJECT_FILES = (
    "LICENSE",
    "DISCLAIMER.md",
    "zh/DISCLAIMER.md",
)
_TEXT_SUFFIXES = {"", ".md", ".rst", ".text", ".txt"}
_LICENSE_NAME = re.compile(
    r"^(?:licen[cs]e|copying|notice|copyright|authors)(?:$|[-_.])",
    re.IGNORECASE,
)
_SAFE_SEGMENT = re.compile(r"[^a-z0-9._-]+")
_MAX_NOTICE_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_MAX_PACKAGE_NAME_CHARS = 200
_MAX_VERSION_CHARS = 200
_MAX_URL_CHARS = 2_048
_MAX_PROJECT_URLS = 32


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_segment(value: str) -> str:
    return _SAFE_SEGMENT.sub("-", value.lower()).strip("-")


def _is_license_path(path: metadata.PackagePath) -> bool:
    suffix = Path(path.name).suffix.lower()
    return suffix in _TEXT_SUFFIXES and (
        _LICENSE_NAME.match(path.name) is not None
        or any(part.lower() in {"license", "licenses"} for part in path.parts[:-1])
    )


def _copy_distribution_licenses(
    distribution: metadata.Distribution,
    destination: Path,
) -> list[str]:
    root = distribution.locate_file("").resolve()
    copied: list[str] = []
    seen: set[bytes] = set()
    for package_path in distribution.files or ():
        if not _is_license_path(package_path):
            continue
        source = distribution.locate_file(package_path)
        if source.is_symlink() or not source.is_file():
            continue
        resolved = source.resolve()
        if not resolved.is_relative_to(root):
            raise RuntimeError(f"依赖许可证路径逃逸安装根：{package_path}")
        content = resolved.read_bytes()
        if len(content) > _MAX_NOTICE_BYTES:
            raise RuntimeError(f"依赖许可证文件异常过大：{package_path}")
        if content in seen:
            continue
        seen.add(content)
        base_output_name = _safe_segment(package_path.name) or "license.txt"
        output_name = base_output_name
        suffix = 1
        while output_name in copied:
            output_name = f"{base_output_name}-{suffix}"
            suffix += 1
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        (destination / output_name).write_bytes(content)
        copied.append(output_name)
    return copied


def _load_overrides(root: Path) -> tuple[dict[str, dict[str, str]], Path]:
    override_root = root / "third_party" / "python_license_overrides"
    payload = json.loads((override_root / "index.json").read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "packages"}
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("packages"), dict)
    ):
        raise RuntimeError("Python 依赖许可证 override 索引无效")
    packages = payload["packages"]
    required = {"version", "license", "template", "copyright", "source"}
    for name, override in packages.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(override, dict)
            or set(override) != required
            or any(not isinstance(override[field], str) for field in required)
            or any(not override[field] for field in required - {"copyright"})
        ):
            raise RuntimeError("Python 依赖许可证 override 索引无效")
    return packages, override_root


def _override_for(
    *,
    name: str,
    version: str,
    overrides: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    key = "pyobjc" if name == "pyobjc-core" or name.startswith("pyobjc-framework-") else name
    override = overrides.get(key)
    if override is None:
        return None
    if override.get("version") != version:
        raise RuntimeError(
            f"{name} {version} 未提供独立许可证文件，且现有 override 只适用于 "
            f"{override.get('version')}；必须重新核对上游许可证"
        )
    return override


def _write_override(
    *,
    override: dict[str, str],
    override_root: Path,
    destination: Path,
) -> list[str]:
    template_name = override.get("template", "")
    template = override_root / template_name
    if (
        not template.is_file()
        or template.is_symlink()
        or template.resolve().parent != override_root.resolve()
    ):
        raise RuntimeError(f"依赖许可证 override 模板无效：{template_name}")
    content = template.read_text(encoding="utf-8")
    copyright_notice = override.get("copyright", "")
    if "{{COPYRIGHT}}" in content and not copyright_notice:
        raise RuntimeError(f"依赖许可证 override 缺少版权声明：{template_name}")
    content = content.replace("{{COPYRIGHT}}", copyright_notice)
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    output_name = f"LICENSE-{_safe_segment(override['license'])}.txt"
    (destination / output_name).write_text(content, encoding="utf-8")
    return [output_name]


def _python_license(base_prefix: Path) -> Path:
    major_minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = (
        base_prefix / "LICENSE.txt",
        base_prefix / "LICENSE",
        base_prefix / "Lib" / "LICENSE.txt",
        base_prefix / "lib" / major_minor / "LICENSE.txt",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise RuntimeError(f"当前 Python 运行时没有可分发的许可证文件：{base_prefix}")


def stage_python_notices(
    *,
    repository_root: Path,
    destination: Path,
    distributions: Iterable[metadata.Distribution] | None = None,
    base_prefix: Path | None = None,
) -> list[dict[str, object]]:
    """Copy Python/runtime notices and return a deterministic package index."""
    if destination.exists():
        raise RuntimeError(f"Python 许可证输出目录必须尚不存在：{destination}")
    destination.mkdir(parents=True, mode=0o700)
    runtime_license = _python_license((base_prefix or Path(sys.base_prefix)).resolve())
    shutil.copyfile(runtime_license, destination / "PYTHON-LICENSE.txt")
    total_bytes = (destination / "PYTHON-LICENSE.txt").stat().st_size
    if total_bytes > _MAX_TOTAL_BYTES:
        raise RuntimeError("Python 第三方许可证材料异常过大")
    overrides, override_root = _load_overrides(repository_root)

    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    output_owners: dict[tuple[str, str], tuple[str, str]] = {}
    for distribution in sorted(
        distributions or metadata.distributions(),
        key=lambda item: (
            _canonical_name(str(item.metadata.get("Name", ""))),
            item.version,
        ),
    ):
        raw_name = str(distribution.metadata.get("Name", "")).strip()
        if not raw_name:
            raise RuntimeError("安装环境包含无 Name 的 Python distribution")
        name = _canonical_name(raw_name)
        version = str(distribution.version or "").strip()
        if not version:
            raise RuntimeError(f"Python distribution 缺少版本：{raw_name}")
        if len(raw_name) > _MAX_PACKAGE_NAME_CHARS or len(version) > _MAX_VERSION_CHARS:
            raise RuntimeError(f"Python distribution 名称或版本异常过长：{raw_name}")
        if name == "careerdesk" or (name, version) in seen:
            continue
        seen.add((name, version))
        output_key = (_safe_segment(name), _safe_segment(version))
        if not all(output_key):
            raise RuntimeError(f"Python distribution 名称或版本无法安全写入：{raw_name}")
        existing_owner = output_owners.get(output_key)
        if existing_owner is not None and existing_owner != (name, version):
            raise RuntimeError(
                f"Python distribution 许可证输出路径冲突：{existing_owner} / {(name, version)}"
            )
        output_owners[output_key] = (name, version)
        package_root = destination / "packages" / output_key[0] / output_key[1]
        license_files = _copy_distribution_licenses(distribution, package_root)
        override = None
        if not license_files:
            override = _override_for(name=name, version=version, overrides=overrides)
            if override is None:
                raise RuntimeError(
                    f"{raw_name} {version} 没有随安装包提供许可证文件；"
                    "发布前必须添加经版本锁定的上游 override"
                )
            license_files = _write_override(
                override=override,
                override_root=override_root,
                destination=package_root,
            )
        total_bytes += sum(
            path.stat().st_size for path in package_root.iterdir() if path.is_file()
        )
        if total_bytes > _MAX_TOTAL_BYTES:
            raise RuntimeError("Python 第三方许可证材料异常过大")

        declared_license = str(
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or ""
        ).strip()
        if len(declared_license) > 240 or "\n" in declared_license:
            declared_license = "See bundled license files and package metadata"
        project_urls = distribution.metadata.get_all("Project-URL") or []
        homepage = str(distribution.metadata.get("Home-page") or "").strip()
        if (
            len(homepage) > _MAX_URL_CHARS
            or len(project_urls) > _MAX_PROJECT_URLS
            or any(not isinstance(url, str) or len(url) > _MAX_URL_CHARS for url in project_urls)
        ):
            raise RuntimeError(f"Python distribution URL metadata 超出安全上限：{raw_name}")
        project_urls = sorted(project_urls)
        rows.append({
            "name": raw_name,
            "version": version,
            "declared_license": declared_license,
            "reviewed_license": (override or {}).get("license", declared_license),
            "homepage": homepage,
            "project_urls": project_urls,
            "license_files": license_files,
            "override_source": (override or {}).get("source", ""),
        })

    inventory = (
        json.dumps(
            {
                "schema_version": 1,
                "python": sys.version.split()[0],
                "packages": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    if total_bytes + len(inventory.encode("utf-8")) > _MAX_TOTAL_BYTES:
        raise RuntimeError("Python 第三方许可证材料异常过大")
    (destination / "index.json").write_text(inventory, encoding="utf-8")
    return rows


def stage_legal_bundle(*, repository_root: Path, destination: Path) -> None:
    """Create one immutable legal resource tree for a desktop build."""
    if destination.exists():
        raise RuntimeError(f"法律文件输出目录必须尚不存在：{destination}")
    project_root = destination / "CareerDesk"
    project_root.mkdir(parents=True, mode=0o700)
    for name in _PROJECT_FILES:
        source = repository_root / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"缺少项目法律/边界文件：{name}")
        output = project_root / name
        output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copyfile(source, output)
    stage_python_notices(
        repository_root=repository_root,
        destination=destination / "ThirdParty" / "Python",
    )
