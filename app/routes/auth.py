"""First-admin setup, login, logout and the current session."""

import hmac
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import audit
from app.auth import passwords, sessions
from app.auth.deps import CurrentUser, check_origin, client_ip, get_db, require_user

# Routes anyone may call. Every one is listed in tests/test_route_auth.py.
public_router = APIRouter(prefix="/api")
# Everything else. require_user runs for every route added here.
protected_router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

_INVALID_LOGIN = "Invalid username or password"
_TOO_MANY = "Too many attempts. Try again later."


class SetupRequest(BaseModel):
    token: str = Field(max_length=128)
    username: str = Field(max_length=64)
    password: str = Field(max_length=passwords.MAX_PASSWORD_LENGTH)


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=passwords.MAX_PASSWORD_LENGTH)


class SessionInfo(BaseModel):
    username: str
    csrf_token: str


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        sessions.COOKIE_NAME,
        token,
        max_age=int(request.app.state.settings.session_absolute.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _users_exist(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


@public_router.get("/setup/status")
def setup_status(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, bool]:
    return {"needs_setup": not _users_exist(conn)}


@public_router.post(
    "/setup", status_code=status.HTTP_201_CREATED, dependencies=[Depends(check_origin)]
)
def setup(
    body: SetupRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, str]:
    """Create the first admin with the one-time token printed to the container log."""
    state = request.app.state
    ip = client_ip(request)
    if state.limiter.is_blocked(None, ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _TOO_MANY)
    if state.setup_token is None or _users_exist(conn):
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed")
    if not hmac.compare_digest(body.token.encode(), state.setup_token.encode()):
        state.limiter.record_failure(None, ip)
        audit.record(conn, "setup_failed", "denied", ip=ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid setup token")

    try:
        passwords.validate_username(body.username)
        passwords.validate_password(body.password, body.username)
    except passwords.PolicyError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None

    password_hash = passwords.hash_password(body.password)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check inside the write lock: two simultaneous setups cannot both win.
        if _users_exist(conn):
            raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed")
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
            (body.username, password_hash),
        )
        audit.record(conn, "setup_complete", "ok", user_id=cursor.lastrowid, ip=ip)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    state.setup_token = None  # setup is now permanently closed
    return {"username": body.username}


@public_router.post("/login", dependencies=[Depends(check_origin)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> SessionInfo:
    state = request.app.state
    ip = client_ip(request)
    if state.limiter.is_blocked(body.username, ip):
        audit.record(conn, "login_blocked", "denied", ip=ip)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _TOO_MANY)

    user = conn.execute(
        "SELECT id, username, password_hash FROM users WHERE username = ?", (body.username,)
    ).fetchone()
    ok, upgraded_hash = passwords.verify_password(
        user["password_hash"] if user else None, body.password
    )
    if not ok:
        locked = state.limiter.record_failure(body.username, ip)
        # The typed username is only logged when it matches a real account:
        # people sometimes type their password into the username field.
        audit.record(
            conn,
            "login_failed",
            "denied",
            user_id=user["id"] if user else None,
            ip=ip,
            unknown_user=user is None,
            locked=locked,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID_LOGIN)

    state.limiter.record_success(body.username)
    if upgraded_hash:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (upgraded_hash, user["id"]))

    # Always a brand-new session; any session the browser already had is dropped
    # (prevents session fixation).
    sessions.delete(conn, request.cookies.get(sessions.COOKIE_NAME))
    token = sessions.create(conn, user["id"], state.settings, ip, request.headers.get("user-agent"))
    audit.record(conn, "login", "ok", user_id=user["id"], ip=ip)
    _set_session_cookie(response, token, request)
    return SessionInfo(
        username=user["username"],
        csrf_token=sessions.csrf_token(state.csrf_secret, sessions.hash_token(token)),
    )


@protected_router.get("/session")
def current_session(request: Request, user: CurrentUser = Depends(require_user)) -> SessionInfo:
    return SessionInfo(
        username=user.username,
        csrf_token=sessions.csrf_token(request.app.state.csrf_secret, user.session.token_hash),
    )


@protected_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    user: CurrentUser = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (user.session.id,))
    audit.record(conn, "logout", "ok", user_id=user.id, ip=client_ip(request))
    response.delete_cookie(
        sessions.COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict"
    )
