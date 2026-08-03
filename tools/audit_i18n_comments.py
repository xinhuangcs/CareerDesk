"""Reject CJK text in repository-owned code comments and Python docstrings."""

from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import tokenize


def has_cjk(value: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in value)


IGNORED_PARTS = {".git", ".venv", "build", "dist", "node_modules", "release-artifacts"}
PYTHON_ROOTS = (Path("backend"), Path("desktop"), Path("scripts"), Path("tools"))
SQL_COMMENT_PATHS = {Path("backend/src/careerdesk/platform/database/schema.py")}
TEXT_COMMENT_FILES = (
    Path("Dockerfile"),
    Path(".dockerignore"),
    Path(".env.example"),
    Path("compose.yaml"),
    Path("compose.override.yaml"),
    Path("compose.prod.yaml"),
    Path("backend/pyproject.toml"),
    Path("desktop/launch-headless.sh"),
    Path("desktop/make-shortcut.ps1"),
    Path("start.command"),
    Path("start.bat"),
    Path("start-hidden.vbs"),
    Path("build-local-macos-package.command"),
)


def python_paths() -> list[Path]:
    paths = [path for root in PYTHON_ROOTS for path in root.rglob("*.py")]
    if Path("run.py").is_file():
        paths.append(Path("run.py"))
    return sorted(path for path in paths if not IGNORED_PARTS.intersection(path.parts))


def main() -> None:
    violations: list[str] = []
    for path in python_paths():
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT and has_cjk(token.string):
                violations.append(f"{path}:{token.start[0]}: CJK comment")
            if token.type == tokenize.STRING and path in SQL_COMMENT_PATHS:
                try:
                    value = ast.literal_eval(token.string)
                except (SyntaxError, ValueError):
                    continue
                if not isinstance(value, str):
                    continue
                for offset, line in enumerate(value.splitlines()):
                    marker = line.find("--")
                    if marker >= 0 and has_cjk(line[marker + 2:]):
                        violations.append(
                            f"{path}:{token.start[0] + offset}: CJK SQL comment"
                        )
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            value = ast.get_docstring(node, clean=False)
            if value and has_cjk(value):
                violations.append(f"{path}:{node.body[0].lineno}: CJK docstring")

    comment_pattern = re.compile(r"^\s*(?:#(?!\!)|rem\b|').*", re.IGNORECASE)
    heredoc_pattern = re.compile(
        r"<<-?\s*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?P=quote)"
    )
    text_comment_files = (*TEXT_COMMENT_FILES, *Path(".github/workflows").glob("*.yml"))
    for path in text_comment_files:
        if not path.is_file():
            continue
        heredoc_delimiter: str | None = None
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if heredoc_delimiter is not None:
                if line.strip() == heredoc_delimiter:
                    heredoc_delimiter = None
                continue
            comment = comment_pattern.match(line)
            inline_hash_match = re.search(r"(?<!\S)#", line[1:])
            inline_hash = inline_hash_match.start() + 1 if inline_hash_match else -1
            inline_quote = line.find("'", 1) if path.suffix == ".vbs" else -1
            candidate = comment.group(0) if comment else ""
            if inline_hash >= 0:
                candidate += line[inline_hash:]
            if inline_quote >= 0:
                candidate += line[inline_quote:]
            if has_cjk(candidate):
                violations.append(f"{path}:{line_number}: CJK comment")
            heredoc = heredoc_pattern.search(line)
            if heredoc:
                heredoc_delimiter = heredoc.group("delimiter")

    if violations:
        print("\n".join(violations))
        raise SystemExit(1)
    print(f"Comment language audit passed ({len(python_paths())} Python files).")


if __name__ == "__main__":
    main()
