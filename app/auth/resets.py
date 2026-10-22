"""One-time password reset links.

A link can only be created from inside the container (`docker exec ...
reset-password`), which already requires root-level access to the host. There is
deliberately no "forgot password" in the web UI.
"""

import secrets
import sqlite3

from app import clock
from app.auth.sessions import hash_token
from app.config import Settings


def create(conn: sqlite3.Connection, user_id: int, settings: Settings) -> str:
    """Create a reset token for the user, replacing any unused earlier one."""
    token = secrets.token_urlsafe(32)
    now = clock.now()
    conn.execute("DELETE FROM password_resets WHERE user_id = ? AND used_at IS NULL", (user_id,))
    conn.execute(
        "INSERT INTO password_resets (user_id, token_hash, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (
            user_id,
            hash_token(token),
            clock.to_iso(now),
            clock.to_iso(now + settings.reset_link_lifetime),
        ),
    )
    return token


def find_valid(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """The unused, unexpired reset for this token (with the username), or None."""
    if not token or len(token) > 128:
        return None
    return conn.execute(
        "SELECT r.id, r.user_id, u.username FROM password_resets r "
        "JOIN users u ON u.id = r.user_id "
        "WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > ?",
        (hash_token(token), clock.to_iso(clock.now())),
    ).fetchone()


def mark_used(conn: sqlite3.Connection, reset_id: int) -> None:
    conn.execute(
        "UPDATE password_resets SET used_at = ? WHERE id = ?",
        (clock.to_iso(clock.now()), reset_id),
    )
