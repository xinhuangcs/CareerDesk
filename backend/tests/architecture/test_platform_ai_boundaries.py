
import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
AI_ROOT = PACKAGE_ROOT / "platform" / "ai"


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def test_platform_ai_contains_concrete_adapters_without_legacy_shims():
    for name in ("client.py", "retrieval.py", "structured_tasks.py", "tracing.py"):
        assert (AI_ROOT / name).is_file()

    assert not (PACKAGE_ROOT / "llm.py").exists()
    assert not (PACKAGE_ROOT / "rag.py").exists()
    assert not (PACKAGE_ROOT / "agentic" / "runtime" / "tracing.py").exists()


def test_platform_ai_never_depends_on_application_layers():
    forbidden = {"agentic", "bootstrap", "features", "jobs", "routers", "services"}
    violations: list[str] = []

    for path in AI_ROOT.rglob("*.py"):
        for name in _imported_names(path):
            if any(segment in forbidden for segment in name.split(".")):
                violations.append(f"{path.name}: {name}")

    assert violations == []


def test_agentmaker_private_provider_registry_is_isolated():
    users: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        ):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "agentmaker.core.llm_clients"
                or any(alias.name == "_PROFILES" for alias in node.names)
            ):
                users.append(str(path.relative_to(PACKAGE_ROOT)))

    assert users == ["platform/ai/providers.py"]


def test_structured_task_runner_has_one_explicit_validation_retry_boundary():
    path = AI_ROOT / "structured_tasks.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    runner = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_structured_task"
    )
    keyword_defaults = dict(
        zip(
            (argument.arg for argument in runner.args.kwonlyargs),
            runner.args.kw_defaults,
            strict=True,
        ),
    )
    handlers = [
        handler
        for node in ast.walk(runner)
        if isinstance(node, ast.Try)
        for handler in node.handlers
    ]
    agent_calls = [
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "arun"
    ]

    assert "validation_retries" in keyword_defaults
    assert keyword_defaults["validation_retries"] is None
    assert len(handlers) == 1
    assert isinstance(handlers[0].type, ast.Tuple)
    assert {
        item.id for item in handlers[0].type.elts if isinstance(item, ast.Name)
    } == {"LLMResponseError", "StructuredTaskValidationError"}
    assert len(agent_calls) == 1
    agent_keywords = {keyword.arg: keyword.value for keyword in agent_calls[0].keywords}
    assert isinstance(agent_keywords["retries"], ast.Constant)
    assert agent_keywords["retries"].value == 0
    assert "max_tokens" in agent_keywords


def test_every_production_llm_builder_call_forwards_model_bound_capabilities():
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == AI_ROOT / "client.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "build_llm"
            ):
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            if not {"context_window", "max_output_tokens"} <= keywords:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}")

    assert violations == []
