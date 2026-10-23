"""Terminal sessions and their encrypted transcripts.

- A transcript is everything typed and printed in a terminal session. It can
  hold secrets (a password typed into a prompt, `env` output), so it is only
  recorded when a key file is configured, and stored encrypted with
  AES-256-GCM. The key lives in that file, never in the database: a stolen
  database alone reveals nothing of what was typed.
- Each stored piece is bound to its session and position (associated data), so
  pieces cannot be swapped between sessions or reordered without detection.
- Sessions older than the retention time are deleted with their transcripts.
"""

import base64
import binascii
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import clock, db

log = logging.getLogger("app.terminal")

KEY_BYTES = 32
MAX_TRANSCRIPT = 8 * 1024 * 1024  # bytes per session; the rest is not recorded
FLUSH_BYTES = 64 * 1024
_NONCE = 12


class TranscriptKeyError(ValueError):
    """The key file does not hold a usable key. The message says what to fix."""


class TranscriptUnreadable(Exception):
    """A transcript could not be decrypted (no key, another key, or altered data)."""


def load_key(path: Path) -> bytes:
    """Read a base64 key of exactly 32 bytes, e.g. made with `openssl rand -base64 32`."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise TranscriptKeyError(f"Cannot read the transcript key file: {type(e).__name__}") from e
    try:
        key = base64.b64decode(text.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise TranscriptKeyError("The transcript key file must contain base64 text") from None
    if len(key) != KEY_BYTES:
        raise TranscriptKeyError(
            f"The transcript key must be {KEY_BYTES} bytes, got {len(key)}: "
            "create one with `openssl rand -base64 32`"
        )
    if path.stat().st_mode & 0o077:
        log.warning("The transcript key file can be read by other users: chmod 400 it")
    return key


def _aad(public_id: str, seq: int) -> bytes:
    return f"transcript:{public_id}:{seq}".encode()


def encrypt_chunk(key: bytes, public_id: str, seq: int, plaintext: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(public_id, seq))


def decrypt_chunk(key: bytes, public_id: str, seq: int, data: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(data[:_NONCE], data[_NONCE:], _aad(public_id, seq))
    except (InvalidTag, ValueError):
        raise TranscriptUnreadable(
            "This transcript cannot be decrypted with the current key"
        ) from None


# ---------------------------------------------------------------- session rows


@dataclass(frozen=True)
class SessionInfo:
    row_id: int
    public_id: str


def create_session(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    stack: str,
    service: str | None,
    container_id: str,
    container_name: str,
    exec_id: str,
    shell_name: str,
    shell_pid: int,
    recorded: bool,
) -> SessionInfo:
    public_id = secrets.token_hex(8)
    cursor = conn.execute(
        "INSERT INTO terminal_sessions (public_id, user_id, stack, service, container_id, "
        "container_name, exec_id, shell, shell_pid, recorded, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            public_id,
            user_id,
            stack,
            service,
            container_id,
            container_name,
            exec_id,
            shell_name,
            shell_pid,
            int(recorded),
            clock.to_iso(clock.now()),
        ),
    )
    return SessionInfo(row_id=int(cursor.lastrowid or 0), public_id=public_id)


def finish_session(
    conn: sqlite3.Connection, row_id: int, reason: str, exit_code: int | None
) -> None:
    conn.execute(
        "UPDATE terminal_sessions SET ended_at = ?, end_reason = ?, exit_code = ? "
        "WHERE id = ? AND ended_at IS NULL",
        (clock.to_iso(clock.now()), reason, exit_code, row_id),
    )


def unfinished_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Sessions still marked open: the app stopped while they ran."""
    return conn.execute(
        "SELECT id, container_id, exec_id, shell_pid FROM terminal_sessions WHERE ended_at IS NULL"
    ).fetchall()


_PUBLIC_COLUMNS = (
    "public_id, stack, service, container_name, shell, recorded, transcript_bytes, "
    "truncated, started_at, ended_at, end_reason, exit_code"
)


def _public(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["id"] = item.pop("public_id")
    item["recorded"] = bool(item["recorded"])
    item["truncated"] = bool(item["truncated"])
    return item


def list_sessions(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_PUBLIC_COLUMNS} FROM terminal_sessions ORDER BY started_at DESC LIMIT ?",  # noqa: S608  # nosec B608 - fixed column list
        (limit,),
    ).fetchall()
    return [_public(r) for r in rows]


def get_session(conn: sqlite3.Connection, public_id: str) -> tuple[int, dict[str, Any]] | None:
    row = conn.execute(
        f"SELECT id, {_PUBLIC_COLUMNS} FROM terminal_sessions WHERE public_id = ?",  # noqa: S608  # nosec B608 - fixed column list
        (public_id,),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    row_id = item.pop("id")
    return row_id, _public(item)


def read_transcript(
    conn: sqlite3.Connection, key: bytes | None, row_id: int, public_id: str
) -> list[list[Any]]:
    """All events of a session: [milliseconds, "i" (input) or "o" (output), base64 data]."""
    rows = conn.execute(
        "SELECT seq, data FROM transcript_chunks WHERE session_id = ? ORDER BY seq", (row_id,)
    ).fetchall()
    if rows and key is None:
        raise TranscriptUnreadable("No transcript key is configured, so it cannot be decrypted")
    events: list[list[Any]] = []
    for expected, row in enumerate(rows):
        if row["seq"] != expected:
            raise TranscriptUnreadable("Parts of this transcript are missing")
        if key is not None:
            events.extend(json.loads(decrypt_chunk(key, public_id, row["seq"], row["data"])))
    return events


def purge(conn: sqlite3.Connection, retention: Any) -> int:
    """Delete sessions (and their transcripts) older than the retention time."""
    cutoff = clock.to_iso(clock.now() - retention)
    # Overwrite deleted pages with zeros instead of leaving them in the file.
    conn.execute("PRAGMA secure_delete = ON")
    cursor = conn.execute("DELETE FROM terminal_sessions WHERE started_at < ?", (cutoff,))
    return cursor.rowcount


# ---------------------------------------------------------------- recording


class Recorder:
    """Buffers a session's input and output and stores it in encrypted pieces.

    add() runs on the event loop and is cheap; flush() encrypts and writes, so
    it is called in a worker thread. It opens its own database connection.
    """

    def __init__(self, db_path: Path, key: bytes, session: SessionInfo) -> None:
        self._db_path = db_path
        self._key = key
        self._session = session
        self._lock = threading.Lock()
        self._writing = threading.Lock()  # one flush at a time, so pieces land in order
        self._events: list[list[Any]] = []
        self._buffered = 0
        self._seq = 0
        self._start = time.monotonic()
        self.total = 0
        self.truncated = False

    def add(self, kind: str, data: bytes) -> bool:
        """Record input ("i") or output ("o"). True when a flush is due."""
        with self._lock:
            if self.truncated or not data:
                return False
            room = MAX_TRANSCRIPT - self.total
            if len(data) > room:
                data = data[:room]
                self.truncated = True
            ms = int((time.monotonic() - self._start) * 1000)
            self._events.append([ms, kind, base64.b64encode(data).decode()])
            self.total += len(data)
            self._buffered += len(data)
            return self._buffered >= FLUSH_BYTES

    def flush(self) -> None:
        with self._writing:
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            events, self._events = self._events, []
            self._buffered = 0
            if not events:
                return
            seq = self._seq
            self._seq += 1
            total, truncated = self.total, self.truncated
        data = encrypt_chunk(self._key, self._session.public_id, seq, json.dumps(events).encode())
        conn = db.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO transcript_chunks (session_id, seq, data) VALUES (?, ?, ?)",
                (self._session.row_id, seq, data),
            )
            conn.execute(
                "UPDATE terminal_sessions SET transcript_bytes = ?, truncated = ? WHERE id = ?",
                (total, int(truncated), self._session.row_id),
            )
        finally:
            conn.close()


def end_leftovers(conn: sqlite3.Connection, gateway: Any) -> int:
    """End shells of sessions that were open when the app last stopped.

    Only when Docker still reports that same exec as running: a restarted
    container has new processes, and a pid alone could belong to one of them.
    """
    ended = 0
    for row in unfinished_sessions(conn):
        try:
            running = gateway.shell_exit_code(row["exec_id"]) is None
            if running and row["shell_pid"]:
                gateway.end_shell_session(row["container_id"], row["shell_pid"])
                ended += 1
        except Exception as e:  # container or exec gone: nothing left to end
            log.info("Left-over terminal %s: %s", row["id"], type(e).__name__)
        finish_session(conn, row["id"], "app restarted", None)
    return ended
