
import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
CORE_ROOT = PACKAGE_ROOT / "core"
PLATFORM_ROOT = PACKAGE_ROOT / "platform"
FEATURES_ROOT = PACKAGE_ROOT / "features"


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]


def _top_level_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            symbols.update(target.id for target in targets if isinstance(target, ast.Name))
    return symbols


def test_core_never_depends_on_outer_application_layers():
    forbidden_roots = {
        "fastapi", "sqlite3", "agentmaker",
        "careerdesk.bootstrap", "careerdesk.routers", "careerdesk.services",
        "careerdesk.database", "careerdesk.platform", "careerdesk.agentic",
        "careerdesk.features", "careerdesk.jobs", "careerdesk.orchestration",
    }
    violations: list[str] = []

    for path in CORE_ROOT.rglob("*.py"):
        for node in _imports(path):
            if isinstance(node, ast.ImportFrom):
                if node.level >= 2:
                    violations.append(f"{path.name}: relative import escapes core")
                imported = node.module or ""
                if any(imported == root or imported.startswith(f"{root}.") for root in forbidden_roots):
                    violations.append(f"{path.name}: {imported}")
            else:
                for alias in node.names:
                    if any(alias.name == root or alias.name.startswith(f"{root}.")
                           for root in forbidden_roots):
                        violations.append(f"{path.name}: {alias.name}")

    assert violations == []


def test_bootstrap_is_only_a_composition_root():
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(PACKAGE_ROOT / "bootstrap"):
            continue
        for node in _imports(path):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = [module] if module else [alias.name for alias in node.names]
            if any(name and (name == "careerdesk.bootstrap" or name.startswith("careerdesk.bootstrap."))
                   for name in names):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} imports bootstrap")
    assert violations == []


def test_platform_never_depends_on_business_or_composition_layers():
    forbidden_segments = {
        "bootstrap", "routers", "services", "agentic", "features", "jobs", "orchestration",
    }
    violations: list[str] = []
    for path in PLATFORM_ROOT.rglob("*.py"):
        for node in _imports(path):
            names = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                     else [alias.name for alias in node.names])
            for name in names:
                parts = name.split(".")
                if any(part in forbidden_segments for part in parts):
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")
    assert violations == []


def test_legacy_root_entrypoints_are_gone():
    assert not (PACKAGE_ROOT / "config.py").exists()
    assert not (PACKAGE_ROOT / "main.py").exists()
    assert not (PACKAGE_ROOT / "request_limits.py").exists()
    assert not (PACKAGE_ROOT / "upload_storage.py").exists()
    assert not (PACKAGE_ROOT / "documents.py").exists()
    assert (PACKAGE_ROOT / "core" / "config.py").is_file()
    assert (PACKAGE_ROOT / "core" / "paths.py").is_file()
    assert (PACKAGE_ROOT / "bootstrap" / "app.py").is_file()
    assert (PACKAGE_ROOT / "bootstrap" / "lifespan.py").is_file()
    assert (PACKAGE_ROOT / "bootstrap" / "desktop.py").is_file()
    assert (PACKAGE_ROOT / "platform" / "http" / "static.py").is_file()
    assert (PACKAGE_ROOT / "platform" / "storage" / "documents.py").is_file()
    assert (PACKAGE_ROOT / "platform" / "storage" / "uploads.py").is_file()


def test_agentic_namespace_replaces_legacy_agent_and_tool_roots():
    assistant_root = PACKAGE_ROOT / "agentic" / "agents" / "career_assistant"
    assert (assistant_root / "factory.py").is_file()
    assert (assistant_root / "prompt.py").is_file()
    assert (assistant_root / "policy.py").is_file()
    assert (assistant_root / "toolset.py").is_file()
    assert (PACKAGE_ROOT / "agentic" / "tools" / "__init__.py").is_file()
    assert (PACKAGE_ROOT / "agentic" / "memory" / "conversation.py").is_file()
    assert not (PACKAGE_ROOT / "agentic" / "agents" / "main_assistant.py").exists()
    assert not (PACKAGE_ROOT / "agents").exists()
    assert not (PACKAGE_ROOT / "tools").exists()


def test_database_has_no_retired_backfill_migration_runtime():
    retired = {
        "MIGRATIONS",
        "_run_migration",
        "_migrate_legacy_preferences",
        "_backfill_immediate_operation_turn_receipts",
        "_backfill_application_intake_operation_owners",
        "_backfill_preference_owners",
    }
    violations = {
        f"{path.relative_to(PACKAGE_ROOT)}:{symbol}"
        for path in PACKAGE_ROOT.rglob("*.py")
        for symbol in _top_level_symbols(path) & retired
    }
    assert violations == set()


def test_personal_state_feature_and_thin_tool_boundaries():
    feature_root = FEATURES_ROOT / "personal_state"
    tool_path = PACKAGE_ROOT / "agentic" / "tools" / "query_status.py"
    violations: list[str] = []

    for path in feature_root.rglob("*.py"):
        for node in _imports(path):
            names = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                     else [alias.name for alias in node.names])
            for name in names:
                if any(segment in name.split(".")
                       for segment in {"agentic", "bootstrap", "routers", "fastapi"}):
                    violations.append(f"{path.name}: {name}")

    tool_imports = {
        node.module or ""
        for node in _imports(tool_path)
        if isinstance(node, ast.ImportFrom)
    }
    assert "features.personal_state.public" in tool_imports
    assert not any(
        any(segment in name.split(".") for segment in {"database", "services", "sqlite3"})
        for name in tool_imports
    )

    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(feature_root):
            continue
        for node in _imports(path):
            name = node.module or "" if isinstance(node, ast.ImportFrom) else ""
            if name.endswith("features.personal_state.repository"):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} imports private repository")

    assert violations == []
    assert not (PACKAGE_ROOT / "database" / "status.py").exists()


def test_preferences_tool_only_depends_on_feature_public_boundary():
    tool_path = PACKAGE_ROOT / "agentic" / "tools" / "preferences.py"
    import_nodes = [
        node for node in _imports(tool_path)
        if isinstance(node, ast.ImportFrom)
    ]
    imports = {node.module or "" for node in import_nodes}
    feature_imports = [
        node for node in import_nodes
        if node.module == "features.preferences"
    ]

    assert len(feature_imports) == 1
    assert [(alias.name, alias.asname) for alias in feature_imports[0].names] == [
        ("public", "preferences"),
    ]
    assert not any(
        name.startswith((
            "agentmaker.memory",
            "database",
            "features.preferences.repository",
            "features.preferences.operations",
            "services",
            "sqlite3",
        ))
        for name in imports
    )


def test_preference_item_routes_have_one_feature_owner():
    expected = {
        ("/item-commands/{command_id}", "put"),
        ("/item-commands/{command_id}", "get"),
        ("/item-commands/{command_id}/cancel-if-absent", "post"),
        ("/item-operations/{operation_id}", "get"),
    }
    owners: dict[tuple[str, str], list[Path]] = {item: [] for item in expected}

    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (not isinstance(decorator, ast.Call)
                        or not isinstance(decorator.func, ast.Attribute)
                        or not isinstance(decorator.func.value, ast.Name)
                        or decorator.func.value.id != "router"
                        or not decorator.args
                        or not isinstance(decorator.args[0], ast.Constant)
                        or not isinstance(decorator.args[0].value, str)):
                    continue
                route = (decorator.args[0].value, decorator.func.attr.lower())
                if route in owners:
                    owners[route].append(path)

    api_path = FEATURES_ROOT / "preferences" / "api.py"
    assert owners == {route: [api_path] for route in expected}


def test_settings_feature_replaces_legacy_paths_and_keeps_service_http_free():
    feature_root = FEATURES_ROOT / "settings"
    service_path = feature_root / "service.py"
    assert (feature_root / "__init__.py").is_file()
    assert (feature_root / "api.py").is_file()
    assert service_path.is_file()
    assert not (PACKAGE_ROOT / "routers" / "settings.py").exists()
    assert not (PACKAGE_ROOT / "services" / "settings_service.py").exists()

    forbidden_segments = {"fastapi", "routers", "agentic", "bootstrap"}
    violations: list[str] = []
    for node in _imports(service_path):
        names = ([node.module or "", *(alias.name for alias in node.names)]
                 if isinstance(node, ast.ImportFrom)
                 else [alias.name for alias in node.names])
        for name in names:
            if any(segment in name.split(".") for segment in forbidden_segments):
                violations.append(name)

    assert violations == []
