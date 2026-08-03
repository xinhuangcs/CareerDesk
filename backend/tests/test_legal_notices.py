"""Legal/support boundaries and distributable third-party notices."""

from __future__ import annotations

from importlib.metadata import PathDistribution
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from desktop import legal_notices  # noqa: E402


def _distribution(
    root: Path,
    *,
    name: str = "example-package",
    version: str = "1.2.3",
    with_license: bool = True,
    project_url: str = "Source, https://example.test/source",
) -> PathDistribution:
    site = root / "site"
    dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    licenses = dist_info / "licenses"
    licenses.mkdir(parents=True)
    metadata = (
        "Metadata-Version: 2.4\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        "License-Expression: MIT\n"
        f"Project-URL: {project_url}\n"
    )
    (dist_info / "METADATA").write_text(metadata, encoding="utf-8")
    record = [f"{dist_info.name}/METADATA,,"]
    if with_license:
        (licenses / "LICENSE.txt").write_text("Example license\n", encoding="utf-8")
        record.append(f"{dist_info.name}/licenses/LICENSE.txt,,")
    (dist_info / "RECORD").write_text("\n".join(record) + "\n", encoding="utf-8")
    return PathDistribution(dist_info)


def _python_prefix(root: Path) -> Path:
    prefix = root / "python"
    license_file = (
        prefix
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "LICENSE.txt"
    )
    license_file.parent.mkdir(parents=True)
    license_file.write_text("Python license\n", encoding="utf-8")
    return prefix


def test_repository_and_python_package_publish_the_exact_same_mit_license():
    assert (REPO_ROOT / "LICENSE").read_bytes() == (REPO_ROOT / "backend/LICENSE").read_bytes()
    pyproject = (REPO_ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject


def test_python_notice_staging_copies_runtime_and_distribution_license(tmp_path):
    rows = legal_notices.stage_python_notices(
        repository_root=REPO_ROOT,
        destination=tmp_path / "notices",
        distributions=[_distribution(tmp_path)],
        base_prefix=_python_prefix(tmp_path),
    )

    inventory = json.loads((tmp_path / "notices/index.json").read_text(encoding="utf-8"))
    assert (tmp_path / "notices/PYTHON-LICENSE.txt").read_text() == "Python license\n"
    assert rows[0]["name"] == "example-package"
    assert rows[0]["license_files"] == ["license.txt"]
    assert rows[0]["declared_license"] == "MIT"
    assert rows[0]["reviewed_license"] == "MIT"
    assert inventory["packages"] == rows
    assert (tmp_path / "notices/packages/example-package/1.2.3/license.txt").is_file()


def test_python_notice_staging_fails_closed_for_unreviewed_missing_license(tmp_path):
    distribution = _distribution(tmp_path, name="unknown-package", with_license=False)

    with pytest.raises(RuntimeError, match="必须添加经版本锁定"):
        legal_notices.stage_python_notices(
            repository_root=REPO_ROOT,
            destination=tmp_path / "notices",
            distributions=[distribution],
            base_prefix=_python_prefix(tmp_path),
        )


def test_python_notice_staging_records_version_locked_review_separately(tmp_path):
    distribution = _distribution(
        tmp_path,
        name="proxy-tools",
        version="0.1.0",
        with_license=False,
    )

    rows = legal_notices.stage_python_notices(
        repository_root=REPO_ROOT,
        destination=tmp_path / "notices",
        distributions=[distribution],
        base_prefix=_python_prefix(tmp_path),
    )

    assert rows[0]["declared_license"] == "MIT"
    assert rows[0]["reviewed_license"] == "BSD-3-Clause"
    assert rows[0]["override_source"] == "https://github.com/jtushman/proxy_tools"
    assert rows[0]["license_files"] == ["LICENSE-bsd-3-clause.txt"]


def test_license_overrides_with_copyright_placeholder_carry_attribution():
    override_root = REPO_ROOT / "third_party" / "python_license_overrides"
    packages, _ = legal_notices._load_overrides(REPO_ROOT)
    for name, override in packages.items():
        template = (override_root / override["template"]).read_text(encoding="utf-8")
        if "{{COPYRIGHT}}" in template:
            assert override["copyright"], (
                f"{name} 使用带 {{{{COPYRIGHT}}}} 的模板但缺少版权署名"
            )
    assert "Google" in packages["flatbuffers"]["copyright"]
    assert "Google" in packages["magika"]["copyright"]


def test_apache_override_fails_closed_without_attribution(tmp_path):
    override_root = REPO_ROOT / "third_party" / "python_license_overrides"
    with pytest.raises(RuntimeError, match="缺少版权声明"):
        legal_notices._write_override(
            override={
                "template": "Apache-2.0.txt",
                "license": "Apache-2.0",
                "copyright": "",
            },
            override_root=override_root,
            destination=tmp_path / "out",
        )


def test_python_notice_staging_rejects_sanitized_output_path_collisions(tmp_path):
    distributions = [
        _distribution(tmp_path, name="a+b"),
        _distribution(tmp_path, name="a=b"),
    ]

    with pytest.raises(RuntimeError, match="输出路径冲突"):
        legal_notices.stage_python_notices(
            repository_root=REPO_ROOT,
            destination=tmp_path / "notices",
            distributions=distributions,
            base_prefix=_python_prefix(tmp_path),
        )


def test_python_notice_staging_rejects_oversized_url_metadata(tmp_path):
    distribution = _distribution(tmp_path, project_url=f"Source, https://example.test/{'a' * 2048}")

    with pytest.raises(RuntimeError, match="URL metadata"):
        legal_notices.stage_python_notices(
            repository_root=REPO_ROOT,
            destination=tmp_path / "notices",
            distributions=[distribution],
            base_prefix=_python_prefix(tmp_path),
        )


def test_legal_boundary_documents_do_not_offer_support_or_warranty():
    disclaimer = (REPO_ROOT / "DISCLAIMER.md").read_text(encoding="utf-8")
    disclaimer_zh = (REPO_ROOT / "zh/DISCLAIMER.md").read_text(encoding="utf-8")

    assert "provided **as is**" in disclaimer
    assert "without\nexpress or implied warranties" in disclaimer
    assert "do not\npromise a response" in disclaimer
    assert "customer-support channel" in disclaimer
    assert "Do not post API keys" in disclaimer
    assert "no maintainer-operated user account" in disclaimer
    assert "loopback interface" in disclaimer
    assert "Report a vulnerability" in disclaimer
    assert "UNSIGNED" in disclaimer
    assert "submitting a contribution represents" in disclaimer
    assert "按现状" in disclaimer_zh and "不附带任何" in disclaimer_zh
    assert "不承诺回复" in disclaimer_zh
    assert "不是客服渠道" in disclaimer_zh
    assert "请勿公开提交 API Key" in disclaimer_zh
    assert "没有由项目维护者运营的用户账号" in disclaimer_zh
    assert "桌面进程只绑定本机回环地址" in disclaimer_zh
    assert "提交贡献即表示" in disclaimer_zh


def test_desktop_project_notices_use_one_consolidated_boundary_file():
    assert legal_notices._PROJECT_FILES == (
        "LICENSE",
        "DISCLAIMER.md",
        "zh/DISCLAIMER.md",
    )
    for removed_name in (
        "PRIVACY.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "UNSIGNED_RELEASE_NOTICE.md",
        "SUPPORT.md",
    ):
        assert not (REPO_ROOT / removed_name).exists()


def test_public_documentation_matches_the_current_distribution_and_contribution_boundary():
    readme_zh = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "en/README.md").read_text(encoding="utf-8")
    readmes = f"{readme}\n{readme_zh}"
    disclaimer = (REPO_ROOT / "DISCLAIMER.md").read_text(encoding="utf-8")
    disclaimer_zh = (REPO_ROOT / "zh/DISCLAIMER.md").read_text(encoding="utf-8")
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    contributing_zh = (REPO_ROOT / "zh/CONTRIBUTING.md").read_text(encoding="utf-8")

    # Chinese is the default landing page; English is one click away.
    assert '<a href="en/README.md">' in readme_zh
    assert '<a href="../README.md">' in readme
    assert '<strong>简体中文</strong> · <a href="en/README.md">English</a>' in readme_zh
    assert '<a href="../README.md">简体中文</a> · <strong>English</strong>' in readme
    assert all(
        f'href="#{anchor}"' in readme
        for anchor in (
            "-key-features",
            "-what-is-careerdesk",
            "-install-and-get-started",
            "-privacy-and-security",
            "-contributing",
        )
    )
    assert all(
        f'href="#{anchor}"' in readme_zh
        for anchor in (
            "-核心功能",
            "-什么是-careerdesk",
            "-一键安装使用",
            "-隐私与安全",
            "-参与贡献",
        )
    )
    assert "[简体中文](zh/CONTRIBUTING.md)" in contributing
    assert "[English](../CONTRIBUTING.md)" in contributing_zh
    assert "[简体中文](zh/DISCLAIMER.md)" in disclaimer
    assert "[English](../DISCLAIMER.md)" in disclaimer_zh
    assert "## 📚 Documentation" not in readme
    assert "## 📚 文档" not in readme_zh
    assert "零证书" not in readmes or "UNSIGNED" in readmes
    assert "未来的自包含发行包" not in readmes
    assert "逐步迁移" not in readmes
    assert "上线时配 CD" not in readmes
    assert "hosted web service" in readme
    assert "托管式网站服务" in readme_zh
    assert "Every Issue and PR is welcome" in readme
    assert "所有 Issue 和 PR 都非常欢迎" in readme_zh
    assert "customer-support channel" in disclaimer
    assert "does not guarantee review or acceptance" in disclaimer
    assert "社区协作入口，不是客服渠道" in disclaimer_zh
    assert "提交不保证得到评审或接受" in disclaimer_zh
    assert "uv run --project backend python run.py" in contributing
    assert "```mermaid" in contributing
    assert "## Submit a Pull Request" in contributing
    assert "## 提交 Pull Request" in contributing_zh
    assert "ARCHITECTURE_AND_EXECUTION_PLAN.md" not in contributing
    assert "ARCHITECTURE_AND_EXECUTION_PLAN.md" not in contributing_zh
    assert "Submission does not guarantee review" not in contributing
    assert "提交不保证得到评审" not in contributing_zh
    assert "Nothing here attempts to exclude liability" not in disclaimer
    assert "本文不试图排除" not in disclaimer_zh
    assert "## Changes and verification" not in disclaimer
    assert "## 变更与核对" not in disclaimer_zh
    assert "Dependency versions are locked" not in disclaimer
    assert "依赖版本记录在" not in disclaimer_zh
    assert "third-party notice inventory" in disclaimer
    assert "第三方许可清单" in disclaimer_zh
    assert "docs/ARCHITECTURE_AND_EXECUTION_PLAN.md" in (
        REPO_ROOT / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()
    assert readme.count("DISCLAIMER.md") == 1
    assert readme_zh.count("DISCLAIMER.md") == 1


PUBLIC_DOCUMENTS = (
    "README.md",
    "en/README.md",
    "DISCLAIMER.md",
    "zh/DISCLAIMER.md",
    "CONTRIBUTING.md",
    "zh/CONTRIBUTING.md",
)


def test_public_documents_are_tracked_by_git():
    """A public document present only on disk ships as a broken link.

    The ignore rules deny READMEs by default and allow specific paths back in, so a
    moved or added page is dropped silently until a fresh checkout exposes it.
    """
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is unavailable")

    for name in PUBLIC_DOCUMENTS:
        assert name in tracked, name


def test_public_markdown_relative_links_resolve_to_repository_files():
    documents = [
        REPO_ROOT / name
        for name in PUBLIC_DOCUMENTS
    ]
    for document in documents:
        content = document.read_text(encoding="utf-8")
        for raw_target in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", content):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            assert (document.parent / target).resolve().exists(), (
                f"{document.relative_to(REPO_ROOT)} contains a broken link: {raw_target}"
            )
