"""FastAPI dependencies that every route goes through.

require_user is attached to the protected router as a whole, so a new route is
protected by default. tests/test_route_auth.py fails if any route is neither
protected nor on the explicit public list.
"""

import hmac
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from app import db
from app.auth import sessions

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    session: sessions.Session


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def check_origin(request: Request) -> None:
    """Refuse state-changing requests that do not come from our own UI.

    Browsers always send Origin on cross-site POSTs, and Sec-Fetch-Site tells us
    whether the request came from the same origin. A missing Origin (curl, old
    clients) is refused too: this API is only meant for the bundled frontend.
    """
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") not in request.app.state.settings.allowed_origins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Request origin not allowed")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Request origin not allowed")


def require_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> CurrentUser:
    check_origin(request)
    session = sessions.validate(
        conn, request.cookies.get(sessions.COOKIE_NAME), request.app.state.settings
    )
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    if request.method not in SAFE_METHODS:
        expected = sessions.csrf_token(request.app.state.csrf_secret, session.token_hash)
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent.encode(), expected.encode()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")
    return CurrentUser(id=session.user_id, username=session.username, session=session)
