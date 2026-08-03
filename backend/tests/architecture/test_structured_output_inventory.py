"""Inventory every production structured-output root."""

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"

KNOWN_STRUCTURED_RUNNERS = {
    "careerdesk.platform.ai.structured_tasks.run_structured_task",
}

# These functions deliberately forward their ``schema_model`` parameter to the
# next framework adapter.  Concrete roots must still be visible at their
# callers; no other dynamic schema expression is allowed to disappear from the
# reviewed inventory.
TRUSTED_SCHEMA_FORWARDERS = {
    "platform/ai/structured_tasks.py": {"run_structured_task"},
}

EXPECTED_ROOT_SCHEMA_CALLS = {
    "features/applications/service.py": {
        "careerdesk.features.applications.intake_models.BatchParse",
    },
    "features/grill/ai_tasks.py": {
        "careerdesk.features.grill.ai_models.JudgeVerdict",
    },
    "features/research/ai_tasks.py": {
        "careerdesk.features.research.ai_models.CompanyReport",
        "careerdesk.features.research.ai_models.PositionReport",
        "careerdesk.features.research.ai_models.ResearchPlan",
    },
    "features/resumes/ai_tasks.py": {
        "careerdesk.features.resumes.ai_models.ResumeParse",
    },
    "features/reviews/service.py": {
        "careerdesk.features.reviews.ai_models.ReviewBatchIdentityManifest",
        "careerdesk.features.reviews.ai_models.ReviewExtraction",
    },
    "orchestration/application_prep/ai_tasks.py": {
        "careerdesk.orchestration.application_prep.adaptation_contracts.ResumeAdaptationReport",
        "careerdesk.orchestration.application_prep.adaptation_contracts.ResumeSummaryResult",
    },
    "orchestration/interview_generation/ai_tasks.py": {
        "careerdesk.features.questions.public.GeneratedQuestionSet",
        "careerdesk.features.questions.public.MaterialSummary",
    },
}

SCHEMA_BOUNDED_ROOTS = {
    "careerdesk.features.applications.intake_models.BatchParse",
    "careerdesk.features.questions.public.GeneratedQuestionSet",
    "careerdesk.features.questions.public.MaterialSummary",
    "careerdesk.features.grill.ai_models.JudgeVerdict",
    "careerdesk.features.research.ai_models.CompanyReport",
    "careerdesk.features.research.ai_models.PositionReport",
    "careerdesk.features.research.ai_models.ResearchPlan",
    "careerdesk.features.resumes.ai_models.ResumeParse",
    "careerdesk.features.reviews.ai_models.ReviewBatchIdentityManifest",
    "careerdesk.features.reviews.ai_models.ReviewExtraction",
    "careerdesk.orchestration.application_prep.adaptation_contracts.ResumeAdaptationReport",
    "careerdesk.orchestration.application_prep.adaptation_contracts.ResumeSummaryResult",
}

PENDING_SCHEMA_BOUNDARY_ROOTS = set()


def _relative_module(relative_path: Path, node: ast.ImportFrom) -> str:
    package = ["careerdesk", *relative_path.parent.parts]
    if not node.level:
        return node.module or ""
    keep = len(package) - (node.level - 1)
    module_parts = package[:keep]
    if node.module:
        module_parts.extend(node.module.split("."))
    return ".".join(module_parts)


def _imported_symbols(relative_path: Path, tree: ast.Module) -> dict[str, str]:
    """Resolve only module-level imports; local shadowing stays unambiguous."""
    symbols: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                symbols[local_name] = alias.name if alias.asname else local_name
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = _relative_module(relative_path, node)
        for alias in node.names:
            if alias.name == "*":
                continue
            symbols[alias.asname or alias.name] = f"{module}.{alias.name}"
    return symbols


def _qualified_name(value: ast.expr, symbols: dict[str, str]) -> str | None:
    if isinstance(value, ast.Name):
        return symbols.get(value.id)
    if isinstance(value, ast.Attribute):
        base = _qualified_name(value.value, symbols)
        if base:
            return f"{base}.{value.attr}"
    return None


def _enclosing_function(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> str | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


def _is_trusted_schema_forward(
    relative_path: Path,
    call: ast.Call,
    value: ast.expr,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if not isinstance(value, ast.Name) or value.id != "schema_model":
        return False
    function_name = _enclosing_function(call, parents)
    return function_name in TRUSTED_SCHEMA_FORWARDERS.get(str(relative_path), set())


def _runner_schema_slot(
    relative_path: Path,
    node: ast.Call,
    symbols: dict[str, str],
) -> tuple[str, int | None] | None:
    """Return the explicit schema keyword and optional positional slot."""
    if isinstance(node.func, ast.Attribute) and node.func.attr == "arun":
        # ``Agent.arun`` only admits the input as a positional argument.  Calls
        # without output_schema are ordinary Agent executions, not this P3
        # inventory, unless **kwargs makes that fact impossible to prove.
        has_schema = any(keyword.arg == "output_schema" for keyword in node.keywords)
        has_kwargs = any(keyword.arg is None for keyword in node.keywords)
        return ("output_schema", None) if has_schema or has_kwargs else None

    resolved = _qualified_name(node.func, symbols)
    if resolved in KNOWN_STRUCTURED_RUNNERS:
        return "schema_model", None

    if (
        str(relative_path) == "features/questions/ai_tasks.py"
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_question_task"
    ):
        return "schema_model", None

    return None


def _scan_source(
    relative_path: Path,
    source: str,
) -> tuple[set[str], list[str]]:
    tree = ast.parse(source, filename=str(relative_path))
    symbols = _imported_symbols(relative_path, tree)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    roots: set[str] = set()
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        slot = _runner_schema_slot(relative_path, node, symbols)
        if slot is None:
            continue
        keyword_name, positional_index = slot
        if any(keyword.arg is None for keyword in node.keywords):
            unresolved.append(
                f"{relative_path}:{node.lineno}: structured runner uses **kwargs"
            )
            continue
        schema_values = [
            keyword.value for keyword in node.keywords if keyword.arg == keyword_name
        ]
        if (
            not schema_values
            and positional_index is not None
            and len(node.args) > positional_index
        ):
            schema_values.append(node.args[positional_index])
        if len(schema_values) != 1:
            unresolved.append(
                f"{relative_path}:{node.lineno}: structured runner needs one explicit schema"
            )
            continue
        value = schema_values[0]
        root = _qualified_name(value, symbols)
        if root:
            roots.add(root)
            continue
        if _is_trusted_schema_forward(relative_path, node, value, parents):
            continue
        unresolved.append(
            f"{relative_path}:{node.lineno}: structured schema is not a module import"
        )
    return roots, unresolved


def _discover_root_schema_calls() -> tuple[dict[str, set[str]], list[str]]:
    discovered: dict[str, set[str]] = {}
    unresolved: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative_path = path.relative_to(PACKAGE_ROOT)
        roots, failures = _scan_source(
            relative_path,
            path.read_text(encoding="utf-8"),
        )
        unresolved.extend(failures)
        if roots:
            discovered[str(relative_path)] = roots
    return discovered, unresolved


def test_every_structured_output_root_is_in_the_reviewed_inventory():
    discovered, unresolved = _discover_root_schema_calls()
    assert unresolved == []
    assert discovered == EXPECTED_ROOT_SCHEMA_CALLS


def test_inventory_resolves_lowercase_and_module_aliases():
    roots, unresolved = _scan_source(
        Path("features/questions/synthetic.py"),
        """
from careerdesk.platform.ai.structured_tasks import run_structured_task as execute
from careerdesk.features.questions.generation_models import GeneratedQuestionSet as schema
import careerdesk.features.questions.generation_models as models

async def run(llm):
    await execute(llm, schema_model=schema)
    await execute(llm, schema_model=models.MaterialSummary)
""",
    )

    assert unresolved == []
    assert roots == {
        "careerdesk.features.questions.generation_models.GeneratedQuestionSet",
        "careerdesk.features.questions.generation_models.MaterialSummary",
    }


def test_inventory_fails_closed_for_dynamic_schema_and_kwargs():
    roots, unresolved = _scan_source(
        Path("features/questions/synthetic.py"),
        """
from careerdesk.platform.ai.structured_tasks import run_structured_task

async def run(llm, selected_schema, options):
    await run_structured_task(llm, schema_model=selected_schema)
    await run_structured_task(llm, **options)
    await llm.arun("payload", output_schema=choose_schema())
    await llm.arun("payload", **options)
""",
    )

    assert roots == set()
    assert len(unresolved) == 4
    assert sum("uses **kwargs" in failure for failure in unresolved) == 2
    assert sum("not a module import" in failure for failure in unresolved) == 2


def test_inventory_allows_only_the_reviewed_dynamic_forwarder():
    roots, unresolved = _scan_source(
        Path("platform/ai/structured_tasks.py"),
        """
async def run_structured_task(agent, schema_model):
    return await agent.arun("payload", output_schema=schema_model)
""",
    )

    assert roots == set()
    assert unresolved == []


def test_inventory_never_confuses_completed_and_pending_schema_boundaries():
    all_roots = set().union(*EXPECTED_ROOT_SCHEMA_CALLS.values())
    assert SCHEMA_BOUNDED_ROOTS.isdisjoint(PENDING_SCHEMA_BOUNDARY_ROOTS)
    assert SCHEMA_BOUNDED_ROOTS | PENDING_SCHEMA_BOUNDARY_ROOTS == all_roots
