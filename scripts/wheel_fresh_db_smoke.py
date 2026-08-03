"""Smoke the installed backend wheel against a brand-new SQLite database."""

import importlib
from importlib.metadata import distribution
import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import careerdesk
from careerdesk.core import paths


EXPECTED_SCHEMA_VERSION = 28
EXPECTED_TABLES = {
    "applications",
    "assistant_turns",
    "journal",
    "meta",
    "preferences",
    "resumes",
}


def main() -> None:
    source_tree = Path(__file__).resolve().parents[1] / "backend" / "src"
    assert not Path(careerdesk.__file__).resolve().is_relative_to(source_tree)
    assert paths.SOURCE_LAYOUT is False
    assert paths.RESOURCE_ROOT == paths.PACKAGE_ROOT
    frontend = paths.DEFAULT_FRONTEND_DIST_DIR
    assert frontend == paths.PACKAGE_ROOT / "frontend_dist"
    assert (frontend / "index.html").is_file()
    assert (frontend / "assets").is_dir()
    assert any(path.suffix == ".js" for path in (frontend / "assets").iterdir())
    assert not (frontend / "package.json").exists()
    assert not (frontend / "node_modules").exists()
    default_env = paths.PACKAGE_ROOT / "default.env"
    assert default_env.is_file()
    assert "APP_TIMEZONE=Asia/Shanghai" in default_env.read_text(encoding="utf-8")
    launcher = importlib.import_module("careerdesk.bootstrap.desktop")
    assert launcher.SOURCE_LAYOUT is False
    scripts = {
        entry.name: entry.value
        for entry in distribution("careerdesk").entry_points
        if entry.group == "console_scripts"
    }
    assert scripts["careerdesk"] == "careerdesk.bootstrap.desktop:main"
    # Verify the installed topology without falling back to the source tree.
    database_facade = importlib.import_module("careerdesk.platform.database")
    database_connection = importlib.import_module(
        "careerdesk.platform.database.connection"
    )
    database_schema = importlib.import_module("careerdesk.platform.database.schema")
    knowledge = importlib.import_module("careerdesk.features.knowledge.public")
    public_database_symbols = {
        "DatabaseBusy",
        "INTERACTIVE_BUSY_TIMEOUT_MS",
        "application_identity_key",
        "derived_db_path",
        "get_meta",
        "init_db",
        "loads_json",
        "normalize_application_identity_part",
        "now_iso",
        "read_connection",
        "squash_whitespace",
        "transaction",
        "truncate_wal_if_oversized",
    }
    assert set(database_facade.__all__) == public_database_symbols
    assert all(
        getattr(database_facade, name) is getattr(database_connection, name)
        for name in public_database_symbols
    )
    assert database_schema.SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION
    assert set(knowledge.__all__) == {
        "link_question_knowledge_in_transaction",
        "touch_knowledge_point_in_transaction",
    }
    assert importlib.util.find_spec("careerdesk.database") is None

    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "careerdesk.db"
        database_facade.init_db(str(db_path))
        database_facade.init_db(str(db_path))

        with closing(sqlite3.connect(db_path)) as conn:
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            (integrity,) = conn.execute("PRAGMA integrity_check").fetchone()
            foreign_key_violations = conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            tables = {
                name
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }

        assert version == EXPECTED_SCHEMA_VERSION, version
        assert integrity == "ok", integrity
        assert foreign_key_violations == [], foreign_key_violations
        assert EXPECTED_TABLES <= tables, sorted(EXPECTED_TABLES - tables)


if __name__ == "__main__":
    main()
