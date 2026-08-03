
import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "careerdesk"
FEATURE_ROOT = PACKAGE_ROOT / "features" / "knowledge"


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


def test_knowledge_feature_replaces_legacy_database_owner():
    for name in ("__init__.py", "repository.py", "public.py"):
        assert (FEATURE_ROOT / name).is_file()
    assert not (PACKAGE_ROOT / "database" / "knowledge.py").exists()


def test_knowledge_repository_is_private_and_dependency_light():
    private_module = "careerdesk.features.knowledge.repository"
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(FEATURE_ROOT):
            continue
        if private_module in _imports(path):
            violations.append(str(path.relative_to(PACKAGE_ROOT)))
    assert violations == []

    imports = _imports(FEATURE_ROOT / "repository.py")
    assert not any(name.startswith((
        "fastapi",
        "agentmaker",
        "careerdesk.agentic",
        "careerdesk.bootstrap",
        "careerdesk.features",
        "careerdesk.orchestration",
    )) for name in imports)


def test_knowledge_public_surface_is_intentionally_narrow():
    from careerdesk.features.knowledge import public, repository

    assert set(public.__all__) == {
        "link_question_knowledge_in_transaction",
        "touch_knowledge_point_in_transaction",
    }
    assert (
        public.link_question_knowledge_in_transaction
        is repository.link_question_knowledge_in_transaction
    )
    assert (
        public.touch_knowledge_point_in_transaction
        is repository.touch_knowledge_point_in_transaction
    )


def test_reviews_only_use_knowledge_public_seam():
    for relative in (
        "features/reviews/repository.py",
    ):
        imports = _imports(PACKAGE_ROOT / relative)
        assert "careerdesk.features.knowledge.public" in imports
        assert "careerdesk.features.knowledge.repository" not in imports
        assert "careerdesk.database.knowledge" not in imports
