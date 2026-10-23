"""Authentication tests: the checklist from the build draft, Phase 2 part A."""

import json
import secrets
import time
from http.cookies import SimpleCookie

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import audit, db
from app.auth import passwords, sessions
from app.auth.ratelimit import LoginLimiter
from app.config import Settings
from tests.conftest import ORIGIN, PASSWORD, USERNAME, FakeClock

# ---------------------------------------------------------------- helpers


def _login(client: TestClient, username: str = USERNAME, password: str = PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


def _audit_rows(settings: Settings) -> list[dict]:
    conn = db.connect(settings.db_path)
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]


def _use_cookie(client: TestClient, value: str) -> None:
    """Replace whatever session cookie the client holds with this exact value."""
    client.cookies.clear()
    client.cookies.set(sessions.COOKIE_NAME, value)


def _session_cookie(response) -> SimpleCookie:
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie


# ---------------------------------------------------------------- first-admin setup


def test_setup_needed_on_a_fresh_install(client: TestClient) -> None:
    assert client.get("/api/setup/status").json() == {"needs_setup": True}
    assert client.app.state.setup_token


def test_setup_with_wrong_token_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/setup", json={"token": "guess", "username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 403
    assert client.get("/api/setup/status").json() == {"needs_setup": True}


def test_setup_works_once_then_is_closed_for_good(client: TestClient, admin) -> None:
    assert client.get("/api/setup/status").json() == {"needs_setup": False}
    assert client.app.state.setup_token is None
    response = client.post(
        "/api/setup", json={"token": "anything", "username": "second", "password": PASSWORD}
    )
    assert response.status_code == 409


def test_no_setup_token_after_restart_once_admin_exists(settings: Settings, admin) -> None:
    from app.main import create_app

    with TestClient(create_app(settings), base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        assert c.app.state.setup_token is None
        assert c.get("/api/setup/status").json() == {"needs_setup": False}


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        (USERNAME, "short", "at least 12"),
        (USERNAME, "x" * 257, None),  # rejected by the request schema (422)
        (USERNAME, f"my-{USERNAME}-password", "must not contain the username"),
        ("bad name!", PASSWORD, "Username must be"),
        ("", PASSWORD, "Username must be"),
    ],
)
def test_setup_enforces_the_password_policy(
    client: TestClient, username: str, password: str, message: str | None
) -> None:
    response = client.post(
        "/api/setup",
        json={"token": client.app.state.setup_token, "username": username, "password": password},
    )
    assert response.status_code in (400, 422)
    if message:
        assert message in response.json()["detail"]
    assert client.get("/api/setup/status").json() == {"needs_setup": True}


# ---------------------------------------------------------------- password storage


def test_password_is_stored_as_argon2id_hash(settings: Settings, admin) -> None:
    conn = db.connect(settings.db_path)
    stored = conn.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_plaintext_password_appears_nowhere(
    client: TestClient, settings: Settings, admin, caplog: pytest.LogCaptureFixture
) -> None:
    """Scan every byte the app wrote (DB, WAL, logs, responses) for the password."""
    responses = [
        _login(client),
        _login(client, password=PASSWORD + "-wrong"),
        _login(client, username=PASSWORD),  # password typed into the username field
        _login(client, password=PASSWORD + "x" * 300),  # schema error path (422)
        client.post("/api/setup", json={"token": "t", "username": "u", "password": PASSWORD}),
    ]
    needle = PASSWORD.encode()
    for file in settings.data_dir.iterdir():
        assert needle not in file.read_bytes(), f"password found in {file.name}"
    for response in responses:
        assert needle not in response.content, f"password echoed in a {response.status_code}"
    assert PASSWORD not in caplog.text


def test_verify_upgrades_hashes_made_with_weaker_parameters() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    ok, upgraded = passwords.verify_password(weak, PASSWORD)
    assert ok
    assert upgraded is not None
    assert passwords.verify_password(upgraded, PASSWORD) == (True, None)


# ---------------------------------------------------------------- login


def test_login_sets_a_hardened_session_cookie(client: TestClient, admin) -> None:
    response = _login(client)
    assert response.status_code == 200
    assert response.json()["username"] == USERNAME
    raw = response.headers["set-cookie"].lower()
    morsel = _session_cookie(response)[sessions.COOKIE_NAME]
    assert morsel.value
    assert "httponly" in raw
    assert "secure" in raw
    assert "samesite=strict" in raw
    assert morsel["path"] == "/"
    assert not morsel["domain"]  # required by the __Host- prefix


def test_session_token_is_only_stored_hashed(client: TestClient, settings: Settings, admin) -> None:
    token = _session_cookie(_login(client))[sessions.COOKIE_NAME].value
    for file in settings.data_dir.iterdir():
        assert token.encode() not in file.read_bytes()
    conn = db.connect(settings.db_path)
    stored = conn.execute("SELECT token_hash FROM sessions").fetchone()["token_hash"]
    assert stored == sessions.hash_token(token)


def test_wrong_username_and_wrong_password_look_identical(client: TestClient, admin) -> None:
    wrong_password = _login(client, password=PASSWORD + "-nope")
    wrong_user = _login(client, username="nobody")
    assert wrong_password.status_code == wrong_user.status_code == 401
    assert wrong_password.json() == wrong_user.json()


def test_wrong_username_and_wrong_password_take_similar_time(client: TestClient, admin) -> None:
    def timed(**kwargs) -> float:
        start = time.perf_counter()
        _login(client, **kwargs)
        return time.perf_counter() - start

    # Lockouts answer instantly, which would hide the comparison; lift the limits here.
    client.app.state.limiter = LoginLimiter(max_per_user=1000, max_per_ip=1000)
    # Alternate the two cases and compare the fastest run of each: the minimum is
    # the true cost of the work, while slower runs only add random machine noise.
    real, fake = [], []
    for _ in range(6):
        real.append(timed(password="wrong-password-123"))
        fake.append(timed(username=f"ghost{secrets.token_hex(4)}"))
    ratio = min(real) / min(fake)
    assert 0.5 < ratio < 2.0, f"timing differs too much: ratio {ratio:.2f}"


def test_username_is_case_insensitive(client: TestClient, admin) -> None:
    assert _login(client, username=USERNAME.upper()).status_code == 200


# ---------------------------------------------------------------- sessions


def test_session_endpoint_requires_and_reports_the_login(client: TestClient, csrf: str) -> None:
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json() == {"username": USERNAME, "csrf_token": csrf}
    client.cookies.clear()
    assert client.get("/api/session").status_code == 401


def test_forged_and_oversized_tokens_are_rejected(client: TestClient, admin) -> None:
    for value in [secrets.token_urlsafe(32), "", "x" * 5000]:
        _use_cookie(client, value)
        assert client.get("/api/session").status_code == 401


def test_logout_kills_the_session(client: TestClient, csrf: str) -> None:
    old_cookie = client.cookies.get(sessions.COOKIE_NAME)
    response = client.post("/api/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 204
    # Replaying the old cookie must not work: the session is gone server-side.
    _use_cookie(client, old_cookie)
    assert client.get("/api/session").status_code == 401


def test_new_login_replaces_the_old_session(client: TestClient, admin) -> None:
    first = _session_cookie(_login(client))[sessions.COOKIE_NAME].value
    second = _session_cookie(_login(client))[sessions.COOKIE_NAME].value
    assert first != second
    _use_cookie(client, first)
    assert client.get("/api/session").status_code == 401


def test_idle_timeout(client: TestClient, admin, fake_clock: FakeClock) -> None:
    _login(client)
    fake_clock.advance(minutes=29)
    assert client.get("/api/session").status_code == 200  # activity resets the idle timer
    fake_clock.advance(minutes=29)
    assert client.get("/api/session").status_code == 200
    fake_clock.advance(minutes=31)
    assert client.get("/api/session").status_code == 401


def test_absolute_lifetime(client: TestClient, admin, fake_clock: FakeClock) -> None:
    _login(client)
    for _ in range(8):  # active every 20 minutes for 2h40m
        fake_clock.advance(minutes=20)
        assert client.get("/api/session").status_code == 200
    fake_clock.advance(minutes=21)  # past 3 hours since login
    assert client.get("/api/session").status_code == 401


def test_expired_sessions_are_deleted(
    client: TestClient, settings: Settings, admin, fake_clock: FakeClock
) -> None:
    _login(client)
    fake_clock.advance(hours=4)
    client.get("/api/session")
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0


# ---------------------------------------------------------------- CSRF and Origin


def test_state_changes_need_the_csrf_token(client: TestClient, csrf: str) -> None:
    assert client.post("/api/logout").status_code == 403
    assert client.post("/api/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/logout", headers={"X-CSRF-Token": "é".encode()}).status_code == 403
    assert client.get("/api/session").status_code == 200  # still logged in
    assert client.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code == 204


def test_csrf_token_is_bound_to_the_session(client: TestClient, csrf: str) -> None:
    _login(client)  # new session -> the old token must stop working
    assert client.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"},
    ],
)
def test_foreign_origins_are_refused(client: TestClient, csrf: str, headers: dict) -> None:
    assert client.post("/api/logout", headers={"X-CSRF-Token": csrf, **headers}).status_code == 403
    response = client.post(
        "/api/login", json={"username": USERNAME, "password": PASSWORD}, headers=headers
    )
    assert response.status_code == 403


def test_missing_origin_is_refused(client: TestClient, admin) -> None:
    del client.headers["Origin"]
    assert _login(client).status_code == 403


# ---------------------------------------------------------------- brute force


def test_account_locks_after_five_failures(client: TestClient, admin, fake_clock) -> None:
    for _ in range(5):
        assert _login(client, password="wrong-password-123").status_code == 401
    assert _login(client).status_code == 429  # even the right password is refused now
    fake_clock.advance(minutes=16)
    assert _login(client).status_code == 200


def test_unknown_usernames_lock_the_same_way(client: TestClient, admin) -> None:
    """Otherwise the lockout itself would reveal which usernames exist."""
    for _ in range(5):
        assert _login(client, username="ghost").status_code == 401
    assert _login(client, username="ghost").status_code == 429


def test_one_ip_is_limited_across_usernames(client: TestClient, admin) -> None:
    for i in range(20):
        assert _login(client, username=f"user{i}").status_code == 401
    assert _login(client).status_code == 429


def test_setup_token_guessing_is_limited(client: TestClient) -> None:
    for _ in range(20):
        client.post("/api/setup", json={"token": "x", "username": USERNAME, "password": PASSWORD})
    real = client.app.state.setup_token
    response = client.post(
        "/api/setup", json={"token": real, "username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 429


# ---------------------------------------------------------------- audit log


def test_logins_are_audited_without_secrets(client: TestClient, settings: Settings, admin) -> None:
    _login(client, password="wrong-password-123")
    _login(client, username="typed-password-here")
    _login(client)
    rows = _audit_rows(settings)
    actions = [(r["action"], r["result"]) for r in rows]
    assert actions == [
        ("setup_complete", "ok"),
        ("login_failed", "denied"),
        ("login_failed", "denied"),
        ("login", "ok"),
    ]
    assert rows[1]["user_id"] is not None
    unknown = rows[2]
    assert unknown["user_id"] is None
    assert json.loads(unknown["detail_json"])["unknown_user"] is True
    assert "typed-password-here" not in json.dumps(rows)


def test_audit_refuses_secret_fields(settings: Settings) -> None:
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    with pytest.raises(ValueError, match="password"):
        audit.record(conn, "x", "ok", password="oops")


def test_api_responses_are_not_cached(client: TestClient) -> None:
    assert client.get("/api/setup/status").headers["cache-control"] == "no-store"


def test_tab_switch_session_checks_do_not_keep_a_session_alive(
    client: TestClient, csrf: str, fake_clock: FakeClock
) -> None:
    """Coming back to the tab asks whether the session is still valid. That check
    must not count as activity, or glancing at the tab would keep it alive forever."""
    background = {"X-Background-Request": "1"}
    for _ in range(3):
        fake_clock.advance(minutes=9)
        assert client.get("/api/session", headers=background).status_code == 200
    fake_clock.advance(minutes=4)  # 31 minutes since the last real activity
    assert client.get("/api/session", headers=background).status_code == 401
