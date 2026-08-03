
import ast
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
FEATURE_ROOT = PACKAGE_ROOT / "features" / "resumes"


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


def test_resumes_feature_replaces_every_legacy_owner():
    for name in (
        "api.py",
        "service.py",
        "repository.py",
        "public.py",
        "ai_models.py",
        "ai_tasks.py",
        "policy.py",
    ):
        assert (FEATURE_ROOT / name).is_file()

    for path in (
        PACKAGE_ROOT / "routers" / "resumes.py",
        PACKAGE_ROOT / "services" / "resume_service.py",
        PACKAGE_ROOT / "database" / "resumes.py",
        PACKAGE_ROOT / "models" / "resume.py",
        PACKAGE_ROOT / "documents.py",
    ):
        assert not path.exists()

    assert (PACKAGE_ROOT / "platform" / "storage" / "documents.py").is_file()
    uploads_source = (
        PACKAGE_ROOT / "orchestration" / "assistant" / "api.py"
    ).read_text(encoding="utf-8")
    assert '"/resumes' not in uploads_source
    assert "features.resumes" not in uploads_source


def test_resumes_private_modules_do_not_leak_outside_feature():
    private_modules = {
        f"careerdesk.features.resumes.{path.stem}"
        for path in FEATURE_ROOT.glob("*.py")
        if path.stem not in {"__init__", "api", "public"}
    }
    legacy_modules = {
        "careerdesk.database.resumes",
        "careerdesk.models.resume",
        "careerdesk.routers.resumes",
        "careerdesk.services.resume_service",
    }
    violations: list[str] = []

    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(FEATURE_ROOT):
            continue
        for name in _imports(path):
            if name in private_modules | legacy_modules:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")

    assert violations == []


def test_resumes_layers_keep_their_dependency_direction():
    repository_imports = _imports(FEATURE_ROOT / "repository.py")
    service_imports = _imports(FEATURE_ROOT / "service.py")
    ai_task_imports = _imports(FEATURE_ROOT / "ai_tasks.py")
    public_imports = _imports(FEATURE_ROOT / "public.py")

    assert not any(name.startswith((
        "fastapi", "agentmaker", "careerdesk.agentic", "careerdesk.bootstrap",
        "careerdesk.routers", "careerdesk.services",
    )) for name in repository_imports)
    assert not any(name.startswith((
        "fastapi", "careerdesk.agentic", "careerdesk.bootstrap", "careerdesk.routers",
    )) for name in service_imports)
    assert not any(name.startswith((
        "fastapi", "careerdesk.features.applications",
        "careerdesk.features.resumes.api", "careerdesk.features.resumes.repository",
        "careerdesk.features.resumes.service",
    )) for name in ai_task_imports)
    assert "careerdesk.features.resumes.api" not in public_imports


def test_resumes_service_delegates_structured_provider_calls_to_ai_tasks():
    source = (FEATURE_ROOT / "service.py").read_text(encoding="utf-8")

    assert "output_schema=" not in source
    assert "run_structured_task" not in source
    assert "Agent(" not in source


def test_resumes_public_surface_is_intentionally_narrow():
    from careerdesk.features.resumes import public

    assert set(public.__all__) == {
        "ResumeService",
        "STEADY_BOX",
        "get_resume",
        "list_resumes",
        "list_resume_summaries",
        "normalize_resume_line",
        "pick_resume_for_application",
        "resume_adaptation_candidates_in_transaction",
        "resume_analysis_lines",
        "resume_generation_snapshot_in_transaction",
    }


def test_resume_routes_are_registered_once_under_one_owner():
    from careerdesk.bootstrap.app import create_app

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes)

    expected = {
        ("/api/resumes", "GET"),
        ("/api/resumes", "POST"),
        ("/api/resumes/{resume_id}/text", "GET"),
        ("/api/resumes/{resume_id}/text", "PUT"),
        ("/api/resumes/jobs", "GET"),
        ("/api/resumes/jobs/{job_id}", "DELETE"),
        ("/api/resumes/upload", "POST"),
        ("/api/resumes/{resume_id}", "DELETE"),
        ("/api/resumes/{resume_id}", "PUT"),
    }
    counts: Counter[tuple[str, str]] = Counter()
    for route in walk(create_app().routes):
        if not route.path.startswith("/api/resumes"):
            continue
        for method in route.methods:
            if method in {"GET", "POST", "PUT", "DELETE"}:
                counts[(route.path, method)] += 1

    assert set(counts) == expected
    assert all(count == 1 for count in counts.values())


def test_query_library_uses_resumes_public_api():
    imports = _imports(PACKAGE_ROOT / "agentic" / "tools" / "query_library.py")

    assert "careerdesk.features.resumes.public" in imports
    assert "careerdesk.database.resumes" not in imports


def test_platform_documents_contains_no_resume_or_chat_policy():
    from careerdesk.platform.storage import documents

    assert not hasattr(documents, "MAX_RESUME_TEXT_CHARS")
    assert not hasattr(documents, "validate_resume_text")
    assert not hasattr(documents, "CHAT_ATTACHMENT_CHAR_LIMIT")
    assert not hasattr(documents, "IMAGE_SUFFIXES")
