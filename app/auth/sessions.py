"""Server-side sessions.

The browser holds a random token in an HttpOnly cookie. The database stores
only the token's SHA-256, so a leaked database cannot be used to log in.

The CSRF token is HMAC(per-process secret, session token hash): the frontend
fetches it from /api/session and sends it in a header. A cross-site page can
neither read it nor compute it.
"""

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass

from app import clock
from app.config import Settings

COOKIE_NAME = "__Host-session"  # __Host- forces Secure, Path=/ and no Domain
_MAX_TOKEN_LENGTH = 128


@dataclass(frozen=True)
class Session:
    id: int
    user_id: int
    username: str
    token_hash: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(secret: bytes, token_hash: str) -> str:
    return hmac.new(secret, token_hash.encode(), hashlib.sha256).hexdigest()


def create(
    conn: sqlite3.Connection,
    user_id: int,
    settings: Settings,
    ip: str | None,
    user_agent: str | None,
) -> str:
    """Start a new session and return the raw token (it exists only in the cookie)."""
    token = secrets.token_urlsafe(32)
    now = clock.now()
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at, ip, "
        "user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            hash_token(token),
            clock.to_iso(now),
            clock.to_iso(now),
            clock.to_iso(now + settings.session_absolute),
            ip,
            (user_agent or "")[:256] or None,
        ),
    )
    return token


def validate(
    conn: sqlite3.Connection, token: str | None, settings: Settings, *, touch: bool = True
) -> Session | None:
    """Return the session for a cookie token, or None. Expired sessions are deleted.

    touch=False checks the session without counting it as user activity, so
    automatic refreshes and open log streams do not keep an idle session alive.
    """
    if not token or len(token) > _MAX_TOKEN_LENGTH:
        return None
    token_hash = hash_token(token)
    row = conn.execute(
        "SELECT s.id, s.user_id, s.last_seen_at, s.expires_at, u.username "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None

    now = clock.now()
    idle_expired = now - clock.from_iso(row["last_seen_at"]) >= settings.session_idle
    absolute_expired = now >= clock.from_iso(row["expires_at"])
    if idle_expired or absolute_expired:
        conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        return None

    if touch:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?", (clock.to_iso(now), row["id"])
        )
    return Session(
        id=row["id"], user_id=row["user_id"], username=row["username"], token_hash=token_hash
    )


def delete(conn: sqlite3.Connection, token: str | None) -> None:
    if token and len(token) <= _MAX_TOKEN_LENGTH:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def delete_all_for_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired(conn: sqlite3.Connection, settings: Settings) -> None:
    now = clock.now()
    conn.execute(
        "DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at <= ?",
        (clock.to_iso(now), clock.to_iso(now - settings.session_idle)),
    )
