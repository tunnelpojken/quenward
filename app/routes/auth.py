"""First-admin setup, login, logout, the current session and password reset."""

import hmac
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import audit
from app.alerts import Alert, Alerter
from app.auth import passwords, resets, sessions
from app.auth.deps import CurrentUser, check_origin, client_ip, get_db, require_user

# Routes anyone may call. Every one is listed in tests/test_route_auth.py.
public_router = APIRouter(prefix="/api")
# Everything else. require_user runs for every route added here.
protected_router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

_INVALID_LOGIN = "Invalid username or password"
_TOO_MANY = "Too many attempts. Try again later."
_BUSY = "Too many logins at once. Try again in a moment."


class SetupRequest(BaseModel):
    token: str = Field(max_length=128)
    username: str = Field(max_length=64)
    password: str = Field(max_length=passwords.MAX_PASSWORD_LENGTH)


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=passwords.MAX_PASSWORD_LENGTH)


class ResetRequest(BaseModel):
    token: str = Field(max_length=128)
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
        state.alerter.send(
            Alert(
                "Failed setup attempt",
                f"Someone at {ip} tried to create the first admin with a wrong setup token.",
                tags=("warning",),
            )
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid setup token")

    try:
        passwords.validate_username(body.username)
        passwords.validate_password(body.password, body.username)
    except passwords.PolicyError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None

    try:
        password_hash = passwords.hash_password(body.password)
    except passwords.Busy:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _BUSY) from None
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


def _alert_failed_login(
    alerter: Alerter, username: str | None, ip: str | None, locked: list[str]
) -> None:
    who = f"'{username}'" if username else "an unknown username"
    if locked:
        what = " and ".join({"user": "the account", "ip": "the address"}[k] for k in locked)
        alerter.send(
            Alert(
                "Login locked",
                f"Too many failed logins for {who} from {ip}: {what} is locked for 15 minutes.",
                priority="high",
                tags=("rotating_light",),
            )
        )
    else:
        alerter.send(Alert("Failed login", f"Failed login for {who} from {ip}.", tags=("warning",)))


def _check_login(
    state: Any, conn: sqlite3.Connection, body: LoginRequest, ip: str | None
) -> tuple[sqlite3.Row, str | None]:
    """Check the password of a reserved attempt. Raises 401 on a wrong login."""
    user = conn.execute(
        "SELECT id, username, password_hash FROM users WHERE username = ?", (body.username,)
    ).fetchone()
    try:
        ok, upgraded_hash = passwords.verify_password(
            user["password_hash"] if user else None, body.password
        )
    except passwords.Busy:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _BUSY) from None
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
        _alert_failed_login(state.alerter, user["username"] if user else None, ip, locked)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID_LOGIN)
    return user, upgraded_hash


@public_router.post("/login", dependencies=[Depends(check_origin)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> SessionInfo:
    state = request.app.state
    ip = client_ip(request)
    # Reserved before the (slow) password check: attempts still being checked
    # count towards the limit, so parallel requests cannot all slip past it.
    attempt = state.limiter.begin(body.username, ip)
    if attempt != "ok":
        # Recorded once per lock, not for every refused request: refusals cost an
        # attacker nothing, and each row would stay in the audit log for good.
        if attempt == "locked_first":
            audit.record(conn, "login_blocked", "denied", ip=ip)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _TOO_MANY)
    try:
        user, upgraded_hash = _check_login(state, conn, body, ip)
    finally:
        state.limiter.end(body.username, ip)

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
    # Logging out ends every session of this user, on every device: one admin with
    # root-equivalent access, so a forgotten browser elsewhere must not stay in.
    # Open log streams and terminals close at their next session check.
    sessions.delete_all_for_user(conn, user.id)
    audit.record(conn, "logout", "ok", user_id=user.id, ip=client_ip(request))
    response.delete_cookie(
        sessions.COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict"
    )


@public_router.post("/password-reset", dependencies=[Depends(check_origin)])
def password_reset(
    body: ResetRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, str]:
    """Set a new password with a link from the `reset-password` command."""
    state = request.app.state
    ip = client_ip(request)
    if state.limiter.is_blocked(None, ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _TOO_MANY)

    reset = resets.find_valid(conn, body.token)
    if reset is None:
        state.limiter.record_failure(None, ip)
        audit.record(conn, "password_reset_failed", "denied", ip=ip)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This reset link is invalid or expired")
    try:
        passwords.validate_password(body.password, reset["username"])
    except passwords.PolicyError as e:
        # The link stays valid so the user can try a better password.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None

    try:
        password_hash = passwords.hash_password(body.password)
    except passwords.Busy:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _BUSY) from None
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check inside the write lock: the same link cannot be used twice.
        if resets.find_valid(conn, body.token) is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "This reset link is invalid or expired"
            )
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, reset["user_id"])
        )
        resets.mark_used(conn, reset["id"])
        sessions.delete_all_for_user(conn, reset["user_id"])
        audit.record(conn, "password_reset", "ok", user_id=reset["user_id"], ip=ip)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    state.alerter.send(
        Alert(
            "Password changed",
            f"The password for '{reset['username']}' was changed with a reset link from {ip}.",
            priority="high",
            tags=("key",),
        )
    )
    return {"username": reset["username"]}
