"""Append-only audit log (the database refuses UPDATE and DELETE on it)."""

import json
import sqlite3
from typing import Any

# Keys that must never reach the audit log, whatever a caller passes.
_FORBIDDEN_KEYS = {"password", "new_password", "token", "setup_token", "csrf_token", "secret"}


def record(
    conn: sqlite3.Connection,
    action: str,
    result: str,
    *,
    user_id: int | None = None,
    target_stack: str | None = None,
    target_container: str | None = None,
    **detail: Any,
) -> None:
    leaked = _FORBIDDEN_KEYS & detail.keys()
    if leaked:
        raise ValueError(f"Refusing to write secret fields to the audit log: {sorted(leaked)}")
    conn.execute(
        "INSERT INTO audit_log (user_id, action, target_stack, target_container, detail_json, "
        "result) VALUES (?, ?, ?, ?, ?, ?)",
        (
            user_id,
            action,
            target_stack,
            target_container,
            json.dumps(detail, sort_keys=True) if detail else None,
            result,
        ),
    )
