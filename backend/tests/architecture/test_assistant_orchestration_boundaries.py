
import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "careerdesk"
ASSISTANT_ROOT = PACKAGE_ROOT / "orchestration" / "assistant"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            level = node.level
            parts = list(path.relative_to(PACKAGE_ROOT).with_suffix("").parts[:-1])
            base = parts[:len(parts) - level + 1] if level else []
            module = [part for part in (node.module or "").split(".") if part]
            resolved = ".".join(["careerdesk", *base, *module]) if level else (node.module or "")
            names.add(resolved)
            if level and not node.module:
                names.update(f"{resolved}.{alias.name}" for alias in node.names)
    return names


def _routes(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    routes: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.upper()
            if method not in {"GET", "POST", "DELETE", "PUT", "PATCH"} or not decorator.args:
                continue
            arg = decorator.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                routes.append((arg.value, method))
    return routes


def test_exact_files_and_legacy_owners_are_gone():
    assert {path.name for path in ASSISTANT_ROOT.glob("*.py")} == {
        "__init__.py", "api.py", "contracts.py", "ledger.py", "service.py",
        "turn_cancellation.py",
    }
    for legacy in (
        "routers/chat.py", "routers/chat_policy.py", "routers/uploads.py",
        "services/assistant.py",
    ):
        assert not (PACKAGE_ROOT / legacy).exists()
    assert list((PACKAGE_ROOT / "routers").glob("*.py")) == []


def test_service_and_contracts_are_transport_neutral():
    service_imports = _imports(ASSISTANT_ROOT / "service.py")
    contracts_imports = _imports(ASSISTANT_ROOT / "contracts.py")
    ledger_imports = _imports(ASSISTANT_ROOT / "ledger.py")
    assert not any(name.startswith(("fastapi", "careerdesk.bootstrap", "careerdesk.routers"))
                   for name in service_imports)
    assert not any(name.startswith(("fastapi", "agentmaker", "careerdesk"))
                   for name in contracts_imports)
    assert not any(name.startswith(("fastapi", "agentmaker", "careerdesk.agentic"))
                   for name in ledger_imports)
    assert "careerdesk.orchestration.assistant.ledger" in service_imports


def test_api_is_thin_and_does_not_own_storage_or_agent_runtime():
    imports = _imports(ASSISTANT_ROOT / "api.py")
    assert "careerdesk.orchestration.assistant.service" in imports
    assert "careerdesk.orchestration.assistant.contracts" in imports
    assert "careerdesk.orchestration.assistant.ledger" not in imports
    assert not any(name.startswith((
        "agentmaker", "careerdesk.platform.storage", "careerdesk.agentic",
    )) for name in imports)


def test_assistant_urls_have_one_owner_and_bootstrap_uses_it():
    owners: dict[tuple[str, str], list[Path]] = {}
    for path in PACKAGE_ROOT.rglob("*.py"):
        prefix = ""
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "router" for target in node.targets
            ) and isinstance(node.value, ast.Call):
                for keyword in node.value.keywords:
                    if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                        prefix = keyword.value.value
        for route, method in _routes(path):
            owners.setdefault((f"{prefix}{route}", method), []).append(path)
    expected = {
        ("/api/chat", "POST"),
        ("/api/chat/recovery-scope", "GET"),
        ("/api/chat/turns/{client_turn_id}/cancel", "POST"),
        ("/api/chat/turns/{client_turn_id}/cancel-if-absent", "POST"),
        ("/api/chat/turns/{client_turn_id}/status", "GET"),
        ("/api/uploads", "POST"),
        ("/api/uploads/{stored}", "DELETE"),
    }
    assert set(owners) >= expected
    assert all(owners[key] == [ASSISTANT_ROOT / "api.py"] for key in expected)

    imports = _imports(PACKAGE_ROOT / "bootstrap" / "app.py")
    assert "careerdesk.orchestration.assistant.api" in imports
    assert not any(name.startswith("careerdesk.routers") for name in imports)


def test_agent_tools_do_not_bypass_feature_public_seams():
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "agentic" / "tools").glob("*.py"):
        for name in _imports(path):
            if (
                name.startswith(("careerdesk.database", "careerdesk.platform.database"))
                or ".repository" in name
            ):
                violations.append(f"{path.name}: {name}")
    assert violations == []
