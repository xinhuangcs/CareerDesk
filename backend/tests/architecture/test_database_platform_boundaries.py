
import ast
from pathlib import Path

import careerdesk.platform.database as database_facade
from careerdesk.platform.database import connection as database_connection
from careerdesk.platform.database import schema as database_schema


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
DATABASE_ROOT = PACKAGE_ROOT / "platform" / "database"
SCHEMA_PATH = DATABASE_ROOT / "schema.py"
CONNECTION_PATH = DATABASE_ROOT / "connection.py"
FORBIDDEN_DATABASE_ROOT = PACKAGE_ROOT / "database"

PUBLIC_DATABASE_SYMBOLS = {
    "DatabaseBusy",
    "INTERACTIVE_BUSY_TIMEOUT_MS",
    "derived_db_path",
    "get_meta",
    "init_db",
    "loads_json",
    "now_iso",
    "application_identity_key",
    "normalize_application_identity_part",
    "read_connection",
    "squash_whitespace",
    "transaction",
    "truncate_wal_if_oversized",
}

PUBLIC_SCHEMA_SYMBOLS = {
    "FRESH_SCHEMA_REVISION",
    "INDEXES",
    "SCHEMA",
    "SCHEMA_VERSION",
    "TRIGGERS",
    "assert_current_schema_manifest",
    "assert_database_shape_before_init",
    "assert_supported_schema_version",
}

PRIVATE_SCHEMA_SYMBOLS = {
    "_FRESH_CURRENT_INDEX_MANIFEST_DIGEST",
    "_FRESH_CURRENT_TABLE_PROFILE_DIGEST",
    "_FRESH_CURRENT_TRIGGER_MANIFEST_DIGEST",
    "_ReferenceManifest",
    "_assert_required_schema_objects",
    "_assert_schema_manifest",
    "_build_reference_manifest",
    "_foreign_key_groups",
    "_fresh_current_reference_manifest",
    "_index_fingerprint",
    "_key_columns",
    "_manifest_digest",
    "_raise_schema_manifest_mismatch",
    "_required_schema_objects",
    "_schema_object_row",
    "_sql_digest",
    "_table_manifest_entry",
    "_table_profile_digest",
    "_trigger_fingerprint",
}

PRIVATE_CONNECTION_SYMBOLS = {
    "DERIVE_VERSION",
    "EXTRACT_VERSION",
    "_connect",
    "_open_database_read_only",
    "_open_database_read_write",
    "_preflight_database_read_only",
    "_repair_current_derived_schema",
    "set_meta",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_symbols(path: Path) -> set[str]:
    symbols: set[str] = set()
    for node in _tree(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Import):
            symbols.update(
                alias.asname or alias.name.split(".", 1)[0]
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            symbols.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name != "*"
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            symbols.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
    return symbols


def _imports(path: Path) -> list[str]:
    tree = _tree(path)
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
        names.extend(
            f"{module}.{alias.name}"
            for alias in node.names
            if module
        )
    return names


def test_schema_and_connection_have_single_platform_owners():
    expected_schema = PUBLIC_SCHEMA_SYMBOLS | PRIVATE_SCHEMA_SYMBOLS
    expected_connection = PUBLIC_DATABASE_SYMBOLS | PRIVATE_CONNECTION_SYMBOLS

    assert (DATABASE_ROOT / "__init__.py").is_file()
    assert SCHEMA_PATH.is_file()
    assert CONNECTION_PATH.is_file()
    assert expected_schema <= _top_level_symbols(SCHEMA_PATH)
    assert expected_connection <= _top_level_symbols(CONNECTION_PATH)
    assert expected_schema.isdisjoint(_top_level_symbols(CONNECTION_PATH))
    assert expected_connection.isdisjoint(_top_level_symbols(SCHEMA_PATH))
    assert set(database_schema.__all__) == PUBLIC_SCHEMA_SYMBOLS


def test_platform_database_facade_is_narrow_and_identity_preserving():
    assert set(database_facade.__all__) == PUBLIC_DATABASE_SYMBOLS
    assert all(
        getattr(database_facade, name) is getattr(database_connection, name)
        for name in PUBLIC_DATABASE_SYMBOLS
    )

    facade_imports = [
        node
        for node in _tree(DATABASE_ROOT / "__init__.py").body
        if isinstance(node, ast.ImportFrom)
    ]
    assert len(facade_imports) == 1
    assert facade_imports[0].level == 1
    assert facade_imports[0].module == "connection"
    assert {alias.name for alias in facade_imports[0].names} == PUBLIC_DATABASE_SYMBOLS


def test_schema_only_depends_on_stdlib_and_pure_identity_helper():
    allowed_stdlib = {
        "contextlib",
        "dataclasses",
        "functools",
        "hashlib",
        "json",
        "sqlite3",
    }
    violations: list[str] = []

    for node in ast.walk(_tree(SCHEMA_PATH)):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
            violations.extend(sorted(roots - allowed_stdlib))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if node.level == 1 and node.module == "identity":
                continue
            if node.level or root not in allowed_stdlib:
                violations.append(f"level={node.level}:{node.module}")

    assert violations == []


def test_connection_has_only_platform_storage_and_qualified_schema_dependencies():
    allowed_stdlib = {
        "__future__",
        "contextlib",
        "datetime",
        "json",
        "logging",
        "pathlib",
        "sqlite3",
    }
    violations: list[str] = []
    schema_imports: list[ast.ImportFrom] = []

    for node in ast.walk(_tree(CONNECTION_PATH)):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
            violations.extend(sorted(roots - allowed_stdlib))
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if (
            node.level == 1
            and node.module is None
            and [(alias.name, alias.asname) for alias in node.names]
            == [("schema", "database_schema")]
        ):
            schema_imports.append(node)
            continue
        if node.level == 1 and node.module == "identity":
            continue
        if node.level == 2 and node.module == "storage.private":
            continue
        if node.level == 0 and (node.module or "").split(".", 1)[0] in allowed_stdlib:
            continue
        violations.append(f"level={node.level}:{node.module}")

    assert len(schema_imports) == 1
    assert violations == []


def test_removed_database_source_package_and_runtime_imports_stay_absent():
    forbidden_sources = (
        sorted(FORBIDDEN_DATABASE_ROOT.rglob("*.py"))
        if FORBIDDEN_DATABASE_ROOT.exists()
        else []
    )
    assert forbidden_sources == []

    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for name in _imports(path):
            if name == "careerdesk.database" or name.startswith("careerdesk.database."):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {name}")
    assert violations == []
