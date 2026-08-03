"""Grill vertical slice ownership and dependency gates."""

import ast
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
FEATURE_ROOT = PACKAGE_ROOT / "features" / "grill"


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


def test_grill_feature_replaces_every_legacy_owner():
    for name in (
        "ai_models.py",
        "ai_tasks.py",
        "api.py",
        "public.py",
        "repository.py",
        "service.py",
    ):
        assert (FEATURE_ROOT / name).is_file()

    for path in (
        PACKAGE_ROOT / "routers" / "grill.py",
        PACKAGE_ROOT / "services" / "grill_service.py",
        PACKAGE_ROOT / "database" / "grill.py",
        PACKAGE_ROOT / "models" / "grill.py",
    ):
        assert not path.exists()


def test_grill_private_modules_do_not_leak_outside_feature():
    private_modules = {
        "careerdesk.features.grill.ai_models",
        "careerdesk.features.grill.ai_tasks",
        "careerdesk.features.grill.repository",
        "careerdesk.features.grill.service",
    }
    violations: list[str] = []

    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(FEATURE_ROOT):
            continue
        for name in _imports(path):
            if name in private_modules:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")

    assert violations == []


def test_grill_layers_keep_their_dependency_direction():
    repository_imports = _imports(FEATURE_ROOT / "repository.py")
    service_imports = _imports(FEATURE_ROOT / "service.py")
    public_imports = _imports(FEATURE_ROOT / "public.py")

    assert not any(name.startswith(("fastapi", "agentmaker", "careerdesk.agentic"))
                   for name in repository_imports)
    assert not any(name.startswith((
        "fastapi", "careerdesk.agentic", "careerdesk.bootstrap", "careerdesk.routers",
    )) for name in service_imports)
    assert "careerdesk.features.questions.repository" not in repository_imports
    assert "careerdesk.features.questions.service" not in repository_imports
    assert "careerdesk.features.grill.api" not in public_imports
    assert "careerdesk.features.questions.repository" not in service_imports
    assert "careerdesk.features.questions.service" not in service_imports


def test_grill_public_surface_is_read_only_and_narrow():
    from careerdesk.features.grill import public

    assert set(public.__all__) == {
        "create_session_in_transaction", "grill_overview", "list_sessions", "replay",
    }


def test_grill_routes_are_registered_once_under_one_owner():
    from careerdesk.bootstrap.app import create_app

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes)

    expected = {
        ("/api/grill/experiment-intro/claim", "POST"),
        ("/api/grill/start", "POST"),
        ("/api/grill/answer", "POST"),
        ("/api/grill/skip", "POST"),
        ("/api/grill/suspend", "POST"),
        ("/api/grill/resume", "POST"),
        ("/api/grill/sessions", "GET"),
        ("/api/grill/sessions/{session_id}/summary", "GET"),
        ("/api/grill/sessions/{session_id}/finalize", "POST"),
        ("/api/grill/sessions/{session_id}", "DELETE"),
    }
    counts: Counter[tuple[str, str]] = Counter()
    for route in walk(create_app().routes):
        if not route.path.startswith("/api/grill"):
            continue
        for method in route.methods:
            if method in {"GET", "POST", "PUT", "DELETE"}:
                counts[(route.path, method)] += 1

    assert set(counts) == expected
    assert all(count == 1 for count in counts.values())


def test_query_grill_tool_uses_public_api():
    imports = _imports(PACKAGE_ROOT / "agentic" / "tools" / "query_grill.py")

    assert "careerdesk.features.grill.public" in imports
    assert "careerdesk.database.grill" not in imports
