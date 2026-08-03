
import ast
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
FEATURE_ROOT = PACKAGE_ROOT / "features" / "applications"
PREP_ROOT = PACKAGE_ROOT / "orchestration" / "application_prep"


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


def test_applications_and_prep_workflow_replace_legacy_owners():
    assert {path.name for path in FEATURE_ROOT.glob("*.py")} == {
        "__init__.py", "api.py", "contracts.py", "intake_models.py", "public.py",
        "service.py", "workbook_intake.py",
    }
    assert {path.name for path in (FEATURE_ROOT / "repository").glob("*.py")} == {
        "__init__.py", "board.py", "intake.py", "mutations.py", "prep.py",
        "resume_binding.py", "shared.py", "timeline.py",
    }
    assert {path.name for path in (FEATURE_ROOT / "operations").glob("*.py")} == {
        "__init__.py", "delete.py", "merge.py", "merge_models.py", "models.py",
        "update.py", "update_models.py",
    }
    assert {path.name for path in PREP_ROOT.glob("*.py")} == {
        "__init__.py", "adaptation.py", "adaptation_contracts.py", "ai_tasks.py", "api.py",
        "adaptation_workflow.py", "briefing.py", "commands.py", "contracts.py", "factory.py",
        "http_contracts.py", "public.py", "service.py",
    }
    for relative in (
        "database/timeline.py",
        "routers/timeline.py",
        "services/application_service.py",
        "services/briefing_service.py",
        "services/match_service.py",
        "services/prep_service.py",
        "jobs/prep.py",
        "models/application.py",
        "models/match.py",
    ):
        assert not (PACKAGE_ROOT / relative).exists()


def test_private_modules_do_not_leak_across_boundaries():
    private_owners = {
        "careerdesk.features.applications.intake_models": FEATURE_ROOT,
        "careerdesk.features.applications.operations": FEATURE_ROOT,
        "careerdesk.features.applications.operations.delete": FEATURE_ROOT,
        "careerdesk.features.applications.operations.merge": FEATURE_ROOT,
        "careerdesk.features.applications.operations.merge_models": FEATURE_ROOT,
        "careerdesk.features.applications.operations.models": FEATURE_ROOT,
        "careerdesk.features.applications.operations.update": FEATURE_ROOT,
        "careerdesk.features.applications.operations.update_models": FEATURE_ROOT,
        "careerdesk.features.applications.repository": FEATURE_ROOT,
        "careerdesk.features.applications.repository.board": FEATURE_ROOT,
        "careerdesk.features.applications.repository.intake": FEATURE_ROOT,
        "careerdesk.features.applications.repository.mutations": FEATURE_ROOT,
        "careerdesk.features.applications.repository.prep": FEATURE_ROOT,
        "careerdesk.features.applications.repository.resume_binding": FEATURE_ROOT,
        "careerdesk.features.applications.repository.shared": FEATURE_ROOT,
        "careerdesk.features.applications.repository.timeline": FEATURE_ROOT,
        "careerdesk.features.applications.service": FEATURE_ROOT,
        "careerdesk.orchestration.application_prep.briefing": PREP_ROOT,
        "careerdesk.orchestration.application_prep.adaptation": PREP_ROOT,
        "careerdesk.orchestration.application_prep.adaptation_contracts": PREP_ROOT,
        "careerdesk.orchestration.application_prep.adaptation_workflow": PREP_ROOT,
        "careerdesk.orchestration.application_prep.ai_tasks": PREP_ROOT,
        "careerdesk.orchestration.application_prep.contracts": PREP_ROOT,
        "careerdesk.orchestration.application_prep.factory": PREP_ROOT,
        "careerdesk.orchestration.application_prep.service": PREP_ROOT,
    }
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for name in _imports(path):
            owner = private_owners.get(name)
            if owner is not None and not path.is_relative_to(owner):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")
    assert violations == []


def test_applications_and_orchestration_dependency_direction():
    repository_imports = [
        name
        for path in (FEATURE_ROOT / "repository").glob("*.py")
        for name in _imports(path)
    ]
    service_imports = _imports(FEATURE_ROOT / "service.py")
    prep_imports = [
        name
        for path in PREP_ROOT.glob("*.py")
        for name in _imports(path)
    ]
    assert not any(name.startswith(("fastapi", "agentmaker", "careerdesk.agentic"))
                   for name in repository_imports)
    assert not any(name.startswith((
        "fastapi", "careerdesk.agentic", "careerdesk.bootstrap", "careerdesk.routers",
        "careerdesk.orchestration",
    )) for name in service_imports)
    assert not any(name.startswith((
        "careerdesk.features.applications.repository",
        "careerdesk.features.applications.service",
    )) for name in prep_imports)
    for path in FEATURE_ROOT.rglob("*.py"):
        assert not any(name.startswith("careerdesk.orchestration") for name in _imports(path))


def test_public_surfaces_are_intentionally_narrow():
    from careerdesk.features.applications import public as applications
    from careerdesk.orchestration.application_prep import public as prep

    assert set(applications.__all__) == {
        "ApplicationDeleteOperationConflict",
        "ApplicationMergeOperationConflict",
        "ApplicationNextAction",
        "ApplicationService",
        "ApplicationStage",
        "ApplicationUpdateOperationConflict",
        "ApplicationUpdateOperationNotFound",
        "ApplicationUpdateOperationDTO",
        "MAX_APPLICATION_UPDATE_BATCH_ITEMS",
        "STAGE_LABELS",
        "StepText",
            "TimelineMutationConflict",
            "WORKBOOK_SUFFIXES",
        "apply_application_progress_in_transaction",
        "append_application_note",
        "application_jd_confirmation",
        "application_detail",
        "bind_application_resume",
        "board",
        "claim_prep_generation",
        "confirm_application_jd",
        "fail_prep_generation",
        "find_applications_by_company",
        "freeze_resume_adaptation_input",
        "freeze_resume_adaptation_input_in_transaction",
        "get_application_delete_operation",
        "get_application_merge_operation",
        "has_completed_application_merge_lineage_in_transaction",
        "has_later_application_state_write_in_transaction",
            "merge_prep_artifacts",
            "merge_localized_prep_artifacts",
        "merge_resume_adaptation_key_if_current",
        "publish_research_snapshot",
        "execute_application_update_batch",
        "execute_application_update_operation",
        "recover_interrupted_intakes",
        "remove_review_created_application_in_transaction",
            "prepare_all_application_delete_operations",
            "prepare_application_delete_operation",
            "prepare_application_delete_operations",
            "prepare_application_merge_operation",
            "parse_standard_workbook",
        "reject_application_delete_operation",
        "reject_application_merge_operation",
        "resolve_application_by_name",
        "restore_application_stage_in_transaction",
        "set_application_note",
        "set_prep_status",
        "set_research_attempt",
        "touch_prep_generation",
        "timeline_entry_snapshot_fingerprint",
            "statistics",
            "standard_positions_from_structured_text",
        "upcoming",
    }
    assert set(prep.__all__) == {
        "PrepApplicationNotFound",
        "compose_briefing",
        "inspect_resume_adaptation",
        "request_prep_generation",
    }


def test_timeline_routes_keep_exact_contract_and_single_owner():
    from careerdesk.bootstrap.app import create_app

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes)

    expected = {
        ("/api/timeline/board", "GET"),
        ("/api/timeline/statistics", "GET"),
        ("/api/timeline/upcoming", "GET"),
            ("/api/timeline/intake-operations/pending", "GET"),
            ("/api/timeline/intake-operations/file", "POST"),
        ("/api/timeline/intake-operations/{operation_id}", "GET"),
        ("/api/timeline/intake-operations/{operation_id}/approve", "POST"),
        ("/api/timeline/intake-operations/{operation_id}/reject", "POST"),
        ("/api/timeline/application-delete-operations/pending", "GET"),
        ("/api/timeline/application-delete-operations/{operation_id}", "GET"),
        ("/api/timeline/application-delete-operations/{operation_id}/approve", "POST"),
        ("/api/timeline/application-delete-operations/{operation_id}/reject", "POST"),
        ("/api/timeline/application-merge-operations/pending", "GET"),
        ("/api/timeline/application-merge-operations/{operation_id}", "GET"),
        ("/api/timeline/application-merge-operations/{operation_id}/approve", "POST"),
        ("/api/timeline/application-merge-operations/{operation_id}/reject", "POST"),
        ("/api/timeline/application-update-operations/by-client-turn/{client_turn_id}", "GET"),
        ("/api/timeline/application-update-operations/{operation_id}", "GET"),
        ("/api/timeline/application-update-operations/{operation_id}/undo", "POST"),
        ("/api/timeline/application-update-undo-commands/{command_id}", "GET"),
        ("/api/timeline/applications", "POST"),
        ("/api/timeline/applications/{application_id}", "GET"),
        ("/api/timeline/applications/{application_id}/briefing", "GET"),
        ("/api/timeline/applications/{application_id}/priority", "PUT"),
        ("/api/timeline/applications/{application_id}/note", "PUT"),
        ("/api/timeline/applications/{application_id}/profile", "PUT"),
        ("/api/timeline/applications/{application_id}/prepare-delete", "POST"),
        ("/api/timeline/applications/{application_id}/stage", "PUT"),
        ("/api/timeline/applications/{application_id}/next-action", "PUT"),
        ("/api/timeline/applications/{application_id}/progress", "POST"),
        ("/api/timeline/applications/{application_id}/complete-next-action", "POST"),
        ("/api/timeline/applications/{application_id}/timeline-entries/{entry_id}", "PUT"),
        ("/api/timeline/applications/{application_id}/timeline-entries/{entry_id}", "DELETE"),
        ("/api/timeline/applications/{application_id}/resume-binding", "PUT"),
        ("/api/timeline/applications/{application_id}/resume-adaptation", "GET"),
        ("/api/timeline/applications/{application_id}/resume-adaptation", "POST"),
        (
            "/api/timeline/applications/{application_id}/resume-adaptation/input-preview",
            "GET",
        ),
        ("/api/timeline/applications/{application_id}/prep", "POST"),
    }
    counts: Counter[tuple[str, str]] = Counter()
    for route in walk(create_app().routes):
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/timeline"):
            continue
        for method in route.methods:
            if method in {"GET", "POST", "PUT", "DELETE"}:
                counts[(route.path, method)] += 1
    assert set(counts) == expected
    assert all(count == 1 for count in counts.values())


def test_cross_domain_consumers_use_public_seams():
    applications_consumers = (
        "agentic/agents/career_assistant/toolset.py",
        "agentic/tools/manage_jobs.py",
        "agentic/tools/manage_timeline.py",
        "agentic/tools/query_timeline.py",
        "features/research/service.py",
        "features/resumes/service.py",
        "features/reviews/repository.py",
        "orchestration/application_prep/commands.py",
        "orchestration/application_prep/briefing.py",
        "orchestration/application_prep/adaptation_workflow.py",
        "orchestration/application_prep/service.py",
    )
    for relative in applications_consumers:
        imports = _imports(PACKAGE_ROOT / relative)
        assert "careerdesk.features.applications.public" in imports
        assert "careerdesk.features.applications.repository" not in imports

    query_prep_imports = _imports(PACKAGE_ROOT / "agentic/tools/query_prep.py")
    assert "careerdesk.orchestration.application_prep.public" in query_prep_imports
    assert not any(name.startswith("careerdesk.orchestration.application_prep.")
                   and not name.startswith("careerdesk.orchestration.application_prep.public")
                   for name in query_prep_imports)

    request_prep_imports = _imports(
        PACKAGE_ROOT / "agentic/tools/request_application_prep.py",
    )
    assert "careerdesk.orchestration.application_prep.public" in request_prep_imports
    assert "careerdesk.orchestration.application_prep.commands" not in request_prep_imports


def test_resume_adaptation_workflow_uses_feature_public_cas_seams():
    path = PREP_ROOT / "adaptation_workflow.py"
    imports = _imports(path)
    source = path.read_text(encoding="utf-8")

    assert "careerdesk.features.applications.public" in imports
    assert "careerdesk.features.research.public" in imports
    assert "careerdesk.platform.database" not in imports
    assert "careerdesk.orchestration.application_prep.http_contracts" not in imports
    assert not any(".repository" in name for name in imports)
    assert "SELECT " not in source
    assert "UPDATE " not in source

    applications_public_imports = _imports(FEATURE_ROOT / "public.py")
    assert "careerdesk.features.companies.public" in applications_public_imports
    assert "careerdesk.features.resumes.public" in applications_public_imports
