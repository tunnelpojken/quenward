import sqlite3
import stat
from pathlib import Path

import pytest

from app import db
from app.config import Settings

LATEST = len(list(db.MIGRATIONS_DIR.glob("*.sql")))
EXPECTED_TABLES = {"users", "sessions", "audit_log", "settings", "schema_version"}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_migrations_create_the_schema(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "app.db")
    assert db.migrate(conn) == LATEST
    assert _tables(conn) >= EXPECTED_TABLES


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "app.db")
    db.migrate(conn)
    db.migrate(conn)
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert [r["version"] for r in rows] == list(range(1, LATEST + 1))


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (migrations / "0002_broken.sql").write_text("CREATE TABLE b (x INTEGER);\nTHIS IS NOT SQL;")
    conn = db.connect(tmp_path / "app.db")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, migrations)
    # 0001 applied, 0002 left no half-created table behind.
    assert db.current_version(conn) == 1
    assert "a" in _tables(conn)
    assert "b" not in _tables(conn)


def test_migration_numbering_gaps_are_rejected(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER);")
    (migrations / "0003_c.sql").write_text("CREATE TABLE c (x INTEGER);")
    conn = db.connect(tmp_path / "app.db")
    with pytest.raises(RuntimeError, match="without gaps"):
        db.migrate(conn, migrations)


def test_database_file_and_dir_are_private(tmp_path: Path) -> None:
    path = tmp_path / "data" / "app.db"
    db.migrate(db.connect(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_audit_log_is_append_only(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "app.db")
    db.migrate(conn)
    conn.execute("INSERT INTO audit_log (action, result) VALUES ('test', 'ok')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_log SET result = 'changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


def test_app_startup_runs_migrations(client, settings: Settings) -> None:
    conn = db.connect(settings.db_path)
    assert db.current_version(conn) == LATEST
