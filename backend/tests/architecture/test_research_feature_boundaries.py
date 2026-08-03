
import ast
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.features.companies.public import ensure_company_in_transaction


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
COMPANIES_ROOT = PACKAGE_ROOT / "features" / "companies"
RESEARCH_ROOT = PACKAGE_ROOT / "features" / "research"


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ["careerdesk", *path.relative_to(PACKAGE_ROOT).parent.parts]
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package) - (node.level - 1)
            base = package[:keep]
            if node.module:
                base.extend(node.module.split("."))
            module = ".".join(base)
        else:
            module = node.module or ""
        names.append(module)
        names.extend(f"{module}.{alias.name}" for alias in node.names if module)
    return names


def test_companies_and_research_replace_every_legacy_owner():
    assert {path.name for path in COMPANIES_ROOT.glob("*.py")} == {
        "__init__.py",
        "public.py",
        "repository.py",
    }
    assert {path.name for path in RESEARCH_ROOT.glob("*.py")} == {
        "__init__.py",
        "ai_models.py",
        "ai_tasks.py",
        "contracts.py",
        "fetcher.py",
        "materials.py",
        "providers.py",
        "public.py",
        "queries.py",
        "repository.py",
        "service.py",
    }
    assert not (PACKAGE_ROOT / "database" / "companies.py").exists()
    assert not (PACKAGE_ROOT / "models" / "research.py").exists()
    assert not (PACKAGE_ROOT / "services" / "research_service.py").exists()


def test_private_modules_do_not_leak_outside_their_features():
    private_modules = {
        "careerdesk.features.companies.repository": COMPANIES_ROOT,
        "careerdesk.features.research.ai_models": RESEARCH_ROOT,
        "careerdesk.features.research.ai_tasks": RESEARCH_ROOT,
        "careerdesk.features.research.contracts": RESEARCH_ROOT,
        "careerdesk.features.research.repository": RESEARCH_ROOT,
        "careerdesk.features.research.service": RESEARCH_ROOT,
    }
    violations: list[str] = []

    for path in PACKAGE_ROOT.rglob("*.py"):
        for name in _imports(path):
            owner = private_modules.get(name)
            if owner is not None and not path.is_relative_to(owner):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")

    assert violations == []


def test_feature_layers_keep_dependency_direction():
    companies_repository_imports = _imports(COMPANIES_ROOT / "repository.py")
    research_repository_imports = _imports(RESEARCH_ROOT / "repository.py")
    research_service_imports = _imports(RESEARCH_ROOT / "service.py")

    assert not any(
        name.startswith(("fastapi", "agentmaker", "careerdesk.agentic"))
        for name in companies_repository_imports + research_repository_imports
    )
    assert not any(
        name.startswith(
            (
                "fastapi",
                "careerdesk.agentic",
                "careerdesk.bootstrap",
                "careerdesk.routers",
                "careerdesk.services",
            )
        )
        for name in research_service_imports
    )


def test_public_surfaces_are_intentionally_narrow():
    from careerdesk.features.companies import public as companies
    from careerdesk.features.research import public as research

    assert set(companies.__all__) == {
        "company_profile_in_transaction",
        "ensure_company_in_transaction",
    }
    assert set(research.__all__) == {
        "ResearchAITaskError",
        "ResearchAttempt",
        "ResearchService",
        "ResearchSnapshot",
        "build_research_snapshot",
        "build_search",
        "company_cache_eligibility_hash",
            "derive_research_artifact_state",
            "derive_search_profile",
        "get_company_profile",
        "get_research_cache",
        "research_semantic_claim",
        "research_semantic_claim_hash",
        "research_is_fresh",
    }


def test_cross_domain_consumers_only_use_public_seams():
    companies_consumers = (
        "features/reviews/repository.py",
        "features/applications/operations/update.py",
    )
    research_consumers = (
        "agentic/tools/query_prep.py",
        "orchestration/application_prep/briefing.py",
        "orchestration/application_prep/factory.py",
        "orchestration/application_prep/service.py",
    )
    for relative in companies_consumers:
        imports = _imports(PACKAGE_ROOT / relative)
        assert "careerdesk.features.companies.public" in imports
        assert "careerdesk.features.companies.repository" not in imports
    for relative in research_consumers:
        imports = _imports(PACKAGE_ROOT / relative)
        assert "careerdesk.features.research.public" in imports
        assert not any(
            name.startswith("careerdesk.features.research.")
            and not name.startswith("careerdesk.features.research.public")
            for name in imports
        )


def test_company_ensure_participates_in_callers_transaction(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    init_db(db_path)

    with pytest.raises(RuntimeError, match="roll back"):
        with transaction(db_path) as conn:
            ensure_company_in_transaction(conn, "u1", "示例公司")
            raise RuntimeError("roll back")

    with read_connection(db_path) as conn:
        assert conn.execute("SELECT id FROM companies").fetchone() is None


def test_company_ensure_is_idempotent_inside_one_transaction(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    init_db(db_path)

    with transaction(db_path) as conn:
        first = ensure_company_in_transaction(conn, "u1", "示例公司")
        second = ensure_company_in_transaction(conn, "u1", "示例公司")

    assert first == second
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_company_ensure_is_safe_for_concurrent_first_writers(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    init_db(db_path)
    workers = 12
    barrier = Barrier(workers)

    def ensure() -> int:
        barrier.wait()
        with transaction(db_path) as conn:
            return ensure_company_in_transaction(conn, "u1", "示例公司")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        company_ids = list(pool.map(lambda _index: ensure(), range(workers)))

    assert len(set(company_ids)) == 1
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
