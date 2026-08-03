"""Questions vertical slice ownership and cross-feature boundary gates."""

import ast
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
FEATURE_ROOT = PACKAGE_ROOT / "features" / "questions"


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


def test_questions_feature_replaces_every_legacy_owner():
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
        PACKAGE_ROOT / "routers" / "questions.py",
        PACKAGE_ROOT / "services" / "question_service.py",
        PACKAGE_ROOT / "services" / "answer_service.py",
        PACKAGE_ROOT / "database" / "questions.py",
    ):
        assert not path.exists()


def test_questions_private_modules_do_not_leak_outside_feature():
    private_modules = {
        "careerdesk.features.questions.ai_models",
        "careerdesk.features.questions.ai_tasks",
        "careerdesk.features.questions.generation_models",
        "careerdesk.features.questions.repository",
        "careerdesk.features.questions.service",
        "careerdesk.features.questions.sets",
    }
    violations: list[str] = []

    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(FEATURE_ROOT):
            continue
        for name in _imports(path):
            if name in private_modules:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")

    assert violations == []


def test_questions_layers_keep_their_dependency_direction():
    ai_task_imports = _imports(FEATURE_ROOT / "ai_tasks.py")
    repository_imports = _imports(FEATURE_ROOT / "repository.py")
    service_imports = _imports(FEATURE_ROOT / "service.py")
    public_imports = _imports(FEATURE_ROOT / "public.py")

    assert not any(
        name.startswith(("fastapi", "agentmaker", "careerdesk.agentic"))
        for name in repository_imports
    )
    assert not any(
        name.startswith(
            (
                "agentmaker",
                "fastapi",
                "careerdesk.agentic",
                "careerdesk.bootstrap",
                "careerdesk.routers",
            )
        )
        for name in service_imports
    )
    assert not any(name.startswith("agentmaker") for name in ai_task_imports)
    assert "careerdesk.platform.ai.structured_tasks" not in ai_task_imports
    assert "careerdesk.features.questions.api" not in public_imports


def test_questions_public_surface_is_intentionally_narrow():
    from careerdesk.features.questions import public

    assert set(public.__all__) == {
        "GeneratedQuestionSet",
        "MaterialSummary",
        "archive_or_delete_question_set",
        "claim_generation",
        "competency_overview",
        "fail_generation",
        "find_knowledge_points",
        "get_question_set",
        "knowledge_overview",
        "list_question_sets",
        "list_questions",
        "list_weak_points",
        "publish_generation",
        "question_overview",
        "question_set_start_snapshot_in_transaction",
        "recover_running_generations",
        "update_generation_stage",
        "verify_answer_guide",
    }


def test_questions_generation_schemas_are_closed_and_bounded():
    from careerdesk.features.questions.public import GeneratedQuestionSet, MaterialSummary

    assert GeneratedQuestionSet.model_json_schema()["additionalProperties"] is False
    assert GeneratedQuestionSet.model_json_schema()["properties"]["questions"]["maxItems"] == 30
    assert MaterialSummary.model_json_schema()["additionalProperties"] is False


def test_question_routes_are_registered_once_under_one_owner():
    from careerdesk.bootstrap.app import create_app

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes)

    expected = {
        ("/api/questions", "GET"),
        ("/api/questions/competency-progress", "GET"),
        ("/api/questions/{question_id}/quality", "PUT"),
        ("/api/questions/{question_id}/answer-guide-verification", "PUT"),
    }
    counts: Counter[tuple[str, str]] = Counter()
    for route in walk(create_app().routes):
        if not route.path.startswith("/api/questions"):
            continue
        for method in route.methods:
            if method in {"GET", "POST", "PUT", "DELETE"}:
                counts[(route.path, method)] += 1

    assert set(counts) == expected
    assert all(count == 1 for count in counts.values())


def test_questions_consumers_use_public_api():
    for relative in ("agentic/tools/query_study.py", "orchestration/interview_generation/api.py"):
        imports = _imports(PACKAGE_ROOT / relative)
        assert "careerdesk.features.questions.public" in imports
        assert "careerdesk.database.questions" not in imports
        assert "careerdesk.services.answer_service" not in imports
