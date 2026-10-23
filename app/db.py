"""SQLite access and schema migrations.

Migrations are plain SQL files in app/migrations, named NNNN_description.sql.
Each one runs exactly once, inside a transaction, and is recorded in
schema_version. Applied migrations must never be edited: add a new file instead.
"""

import os
import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _ensure_private_file(path: Path) -> None:
    """Create the data dir (700) and the db file (600) before SQLite touches them.

    SQLite gives its -wal and -shm files the same permissions as the main
    file, so locking down app.db covers those too.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def connect(path: Path) -> sqlite3.Connection:
    _ensure_private_file(path)
    # autocommit=True: we manage transactions explicitly with BEGIN/COMMIT.
    # check_same_thread=False: FastAPI may open a connection in one worker thread
    # and use it in another (or in the event loop, for WebSockets). That is safe
    # because each connection belongs to a single request and is used by one
    # step at a time, never concurrently.
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _discover(migrations_dir: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for file in sorted(migrations_dir.iterdir()):
        if file.suffix != ".sql":
            continue
        match = _MIGRATION_NAME.match(file.name)
        if not match:
            raise RuntimeError(f"Badly named migration: {file.name}")
        found.append((int(match.group(1)), file))
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"Migrations must be numbered 0001, 0002, ... without gaps: {versions}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> int:
    """Apply pending migrations in order. Returns the resulting schema version."""
    version = current_version(conn)
    for number, file in _discover(migrations_dir):
        if number <= version:
            continue
        sql = file.read_text(encoding="utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would commit first, so run statements one by one.
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        version = number
    return version


def _split_statements(sql: str) -> list[str]:
    """Split a script into complete statements using SQLite's own parser."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip() and not buffer.strip().startswith("--"):
        raise RuntimeError("Migration ends with an incomplete statement")
    return statements
