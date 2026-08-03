
import ast
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
JOURNAL_ROOT = PACKAGE_ROOT / "features" / "journal"
REVIEWS_ROOT = PACKAGE_ROOT / "features" / "reviews"
REVIEW_OPERATIONS_ROOT = REVIEWS_ROOT / "operations"
MAINTENANCE_ROOT = PACKAGE_ROOT / "orchestration" / "maintenance"


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


def test_new_owners_replace_all_legacy_paths():
    assert {path.name for path in JOURNAL_ROOT.glob("*.py")} == {
        "__init__.py", "public.py", "repository.py",
    }
    assert {path.name for path in REVIEWS_ROOT.glob("*.py")} == {
        "__init__.py", "ai_models.py", "api.py", "public.py", "repository.py", "service.py",
    }
    assert {path.name for path in REVIEW_OPERATIONS_ROOT.glob("*.py")} == {
        "__init__.py", "edit_models.py", "record_models.py", "undo.py", "undo_models.py",
    }
    assert {path.name for path in (REVIEW_OPERATIONS_ROOT / "record").glob("*.py")} == {
        "__init__.py", "batch.py", "begin.py", "decisions.py", "dto.py", "errors.py",
        "execute.py", "finalize.py", "read.py", "rows.py",
    }
    assert {path.name for path in (REVIEW_OPERATIONS_ROOT / "edit").glob("*.py")} == {
        "__init__.py", "bundle.py", "errors.py", "execute.py", "projection.py",
        "read.py", "timeline_entry_edit.py", "undo.py", "validate.py",
    }
    assert {path.name for path in MAINTENANCE_ROOT.glob("*.py")} == {
        "__init__.py", "api.py", "contracts.py", "service.py",
    }
    for relative in (
        "database/reviews.py",
        "services/review_service.py",
        "services/maintenance.py",
        "routers/maintenance.py",
        "models/review.py",
        "models/__init__.py",
        "features/reviews/edit_operation_models.py",
        "features/reviews/edit_operations.py",
        "features/reviews/operation_models.py",
        "features/reviews/operations.py",
        "features/reviews/record_operation_models.py",
        "features/reviews/record_operations.py",
    ):
        assert not (PACKAGE_ROOT / relative).exists()


def test_cross_domain_code_cannot_import_private_modules():
    private_owners = {
        "careerdesk.features.journal.repository": JOURNAL_ROOT,
        "careerdesk.features.reviews.ai_models": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.bundle": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.errors": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.execute": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.projection": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.read": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.timeline_entry_edit": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.undo": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit.validate": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.edit_models": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.begin": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.decisions": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.dto": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.errors": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.execute": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.finalize": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.read": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record.rows": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.record_models": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.undo": REVIEWS_ROOT,
        "careerdesk.features.reviews.operations.undo_models": REVIEWS_ROOT,
        "careerdesk.features.reviews.repository": REVIEWS_ROOT,
        "careerdesk.features.reviews.service": REVIEWS_ROOT,
        "careerdesk.orchestration.maintenance.service": MAINTENANCE_ROOT,
    }
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for name in _imports(path):
            owner = private_owners.get(name)
            if owner is not None and not path.is_relative_to(owner):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")
    assert violations == []


def test_dependency_directions_use_public_feature_seams():
    journal_imports = _imports(JOURNAL_ROOT / "repository.py")
    review_imports = [
        name for path in REVIEWS_ROOT.rglob("*.py") for name in _imports(path)
    ]
    maintenance_imports = _imports(MAINTENANCE_ROOT / "service.py")
    assert not any(name.startswith(("fastapi", "careerdesk.features", "careerdesk.orchestration"))
                   for name in journal_imports)
    assert "careerdesk.features.journal.public" in review_imports
    assert "careerdesk.features.applications.public" in review_imports
    assert "careerdesk.features.companies.public" in review_imports
    assert not any(name.startswith("careerdesk.features.journal.repository")
                   for name in review_imports)
    for feature in ("journal", "reviews"):
        assert f"careerdesk.features.{feature}.public" in maintenance_imports
        assert not any(name.startswith(f"careerdesk.features.{feature}.repository")
                       for name in maintenance_imports)
    assert "careerdesk.features.applications.public" not in maintenance_imports


def test_immediate_operation_candidate_reader_has_one_neutral_owner():
    operation_paths = (
        PACKAGE_ROOT / "features" / "applications" / "operations" / "update.py",
        PACKAGE_ROOT / "features" / "preferences" / "operations.py",
        REVIEW_OPERATIONS_ROOT / "edit" / "validate.py",
        REVIEW_OPERATIONS_ROOT / "record" / "dto.py",
    )
    for path in operation_paths:
        imports = _imports(path)
        source = path.read_text(encoding="utf-8")
        assert "careerdesk.features.journal.public" in imports
        assert "careerdesk.features.journal.repository" not in imports
        assert "WITH candidate_ids(id) AS (" not in source
        assert "$.operation.client_turn_id" not in source
        assert source.count(
            "journal.read_operation_candidates_for_turn_in_transaction("
        ) == 1

    repository_source = (JOURNAL_ROOT / "repository.py").read_text(encoding="utf-8")
    assert repository_source.count("WITH candidate_ids(id) AS (") == 1
    assert repository_source.count("$.operation.client_turn_id") == 1
    assert not any(f'"{family}"' in repository_source for family in (
        "application_update",
        "preference_update",
        "review_timeline_entry_edit",
        "review_record",
    ))


def test_public_surfaces_are_explicit():
    from careerdesk.features.journal import public as journal
    from careerdesk.features.reviews import operations
    from careerdesk.features.reviews import public as reviews
    from careerdesk.features.reviews.operations import edit, record, undo

    assert set(journal.__all__) == {
        "OperationCandidate",
        "applied_reviews", "applied_reviews_in_transaction", "append_review",
        "append_review_correction", "cache_review_extraction",
        "claim_review_in_transaction", "fail_review_extraction",
        "finish_review_in_transaction", "get_entry", "read_merged_corrections",
        "read_operation_candidates_for_turn_in_transaction",
        "snapshot", "snapshot_in_transaction", "void_review_in_transaction",
    }
    assert set(reviews.__all__) == {
        "ReviewConflict", "ReviewTimelineEntryEditOperationConflict",
        "ReviewTimelineEntryEditOperationNotFound", "ReviewOperationConflict",
        "ReviewOperationNotFound", "ReviewExtractionUnavailable", "ReviewService",
        "approve_review_operation",
        "approve_review_record_operation",
        "decide_review_record_operations_for_turn",
        "MAX_REVIEW_RECORD_SOURCE_CHARS", "ReviewRecordOperationConflict",
        "ReviewRecordOperationNotFound", "execute_review_record_operation",
        "edit_review_timeline_entry_from_timeline",
        "execute_review_timeline_entry_edit_operation",
        "get_review_timeline_entry_edit_operation",
        "get_review_timeline_entry_edit_undo_command_status", "get_review_operation",
        "get_review_record_operation", "list_pending_review_record_confirmations",
        "list_pending_review_record_clarifications",
        "list_pending_review_operations",
        "list_review_timeline_entry_edit_operations_for_turn",
        "list_review_record_operations_for_turn",
        "prepare_review_timeline_entry_undo_operation",
        "prepare_review_record_undo_operation",
        "prepare_review_undo_operation", "reconcile_metadata_in_transaction",
        "recover_interrupted_review_record_operations_in_transaction",
        "reject_review_operation", "reject_review_record_operation",
        "reject_review_record_operations_for_turn",
        "undo_review_timeline_entry_edit_operation",
    }
    assert set(operations.__all__) == {
        "ReviewTimelineEntryEditOperationConflict",
        "ReviewTimelineEntryEditOperationNotFound",
        "ReviewOperationConflict", "ReviewOperationNotFound",
        "ReviewRecordOperationConflict", "ReviewRecordOperationNotFound",
        "approve_review_operation", "approve_review_record_operation",
        "decide_review_record_operations_for_turn",
        "edit_review_timeline_entry_from_timeline",
        "execute_review_timeline_entry_edit_operation",
        "execute_review_record_batch_operations", "execute_review_record_operation",
        "get_review_timeline_entry_edit_operation",
        "get_review_timeline_entry_edit_undo_command_status", "get_review_operation",
        "get_review_record_operation", "list_pending_review_operations",
        "list_pending_review_record_confirmations",
        "list_pending_review_record_clarifications",
        "list_review_timeline_entry_edit_operations_for_turn",
        "list_review_record_operations_for_turn",
        "prepare_review_timeline_entry_undo_operation",
        "prepare_review_record_undo_operation",
        "prepare_review_undo_operation",
        "recover_interrupted_review_record_operations_in_transaction",
        "reject_review_operation", "reject_review_record_operation",
        "reject_review_record_operations_for_turn",
        "undo_review_timeline_entry_edit_operation",
    }
    assert operations.ReviewOperationConflict is undo.ReviewOperationConflict
    assert (
        operations.ReviewTimelineEntryEditOperationConflict
        is edit.ReviewTimelineEntryEditOperationConflict
    )
    assert operations.ReviewRecordOperationConflict is record.ReviewRecordOperationConflict
    assert reviews.ReviewOperationConflict is undo.ReviewOperationConflict
    assert (
        reviews.ReviewTimelineEntryEditOperationConflict
        is edit.ReviewTimelineEntryEditOperationConflict
    )
    assert reviews.ReviewRecordOperationConflict is record.ReviewRecordOperationConflict


def test_agent_and_bootstrap_consumers_use_new_seams():
    expected = {
        "agentic/agents/career_assistant/toolset.py": "careerdesk.features.reviews.public",
        "agentic/tools/manage_review.py": "careerdesk.features.reviews.public",
        "agentic/tools/record_review.py": "careerdesk.features.reviews.public",
        "agentic/tools/manage_timeline.py": "careerdesk.features.applications.public",
        "bootstrap/app.py": "careerdesk.orchestration.maintenance.api",
    }
    for relative, public_module in expected.items():
        assert public_module in _imports(PACKAGE_ROOT / relative)
    assert "careerdesk.features.reviews.api" in _imports(PACKAGE_ROOT / "bootstrap/app.py")


def test_maintenance_routes_keep_exact_contract_and_single_owner():
    from careerdesk.bootstrap.app import create_app

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes)

    counts: Counter[tuple[str, str]] = Counter()
    for route in walk(create_app().routes):
        if not route.path.startswith("/api/maintenance"):
            continue
        for method in route.methods:
            if method in {"GET", "POST", "PUT", "DELETE"}:
                counts[(route.path, method)] += 1
    assert counts == Counter({
        ("/api/maintenance/status", "GET"): 1,
        ("/api/maintenance/reconcile", "POST"): 1,
    })


def test_journal_writes_have_a_small_explicit_allowlist():
    writers = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if any(token in path.read_text(encoding="utf-8")
               for token in ("INSERT INTO journal", "UPDATE journal", "DELETE FROM journal"))
    }
    assert writers == {
        "features/applications/operations/delete.py",
        "features/applications/operations/merge.py",
        "features/applications/operations/update.py",
        "features/applications/repository/intake.py",
        "features/journal/repository.py",
        "features/preferences/item_commands.py",
        "features/preferences/operations.py",
        "features/reviews/operations/edit/execute.py",
        "features/reviews/operations/edit/projection.py",
        "features/reviews/operations/edit/timeline_entry_edit.py",
        "features/reviews/operations/edit/undo.py",
        "features/reviews/operations/record/begin.py",
        "features/reviews/operations/record/batch.py",
        "features/reviews/operations/record/decisions.py",
        "features/reviews/operations/record/finalize.py",
        "features/reviews/operations/record/read.py",
        "features/reviews/operations/undo.py",
    }
