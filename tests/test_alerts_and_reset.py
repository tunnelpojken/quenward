"""Phase 2 part B: security alerts, common-password check and password reset."""

import json
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli, db
from app.alerts import Alert, NtfyAlerter
from app.auth import passwords, sessions
from app.config import ConfigError, NtfyConfig, Settings
from tests.conftest import ORIGIN, PASSWORD, USERNAME, FakeClock

NEW_PASSWORD = "another-long-passphrase-77"


class RecordingAlerter:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture
def alerts(client: TestClient) -> RecordingAlerter:
    recorder = RecordingAlerter()
    client.app.state.alerter = recorder
    return recorder


def _login(client: TestClient, username: str = USERNAME, password: str = PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


# ---------------------------------------------------------------- common passwords


def test_common_passwords_are_rejected(client: TestClient) -> None:
    common = sorted(passwords._common_passwords())
    assert len(common) > 1000
    sample = next(p for p in common if USERNAME not in p and len(p) <= 256)
    response = client.post(
        "/api/setup",
        json={"token": client.app.state.setup_token, "username": USERNAME, "password": sample},
    )
    assert response.status_code == 400
    assert "commonly used" in response.json()["detail"]


def test_common_password_check_ignores_case() -> None:
    sample = next(iter(passwords._common_passwords()))
    with pytest.raises(passwords.PolicyError, match="commonly used"):
        passwords.validate_password(sample.upper(), "someone")


# ---------------------------------------------------------------- alerts from the app


def test_failed_login_sends_an_alert(client: TestClient, admin, alerts) -> None:
    _login(client, password="wrong-password-123")
    assert len(alerts.alerts) == 1
    alert = alerts.alerts[0]
    assert alert.title == "Failed login"
    assert f"'{USERNAME}'" in alert.message
    assert "testclient" in alert.message  # the client IP


def test_alert_never_repeats_an_unknown_username(client: TestClient, admin, alerts) -> None:
    _login(client, username="typed-my-password-here")
    assert "an unknown username" in alerts.alerts[0].message
    assert "typed-my-password-here" not in alerts.alerts[0].message


def test_lockout_sends_a_high_priority_alert(client: TestClient, admin, alerts) -> None:
    for _ in range(5):
        _login(client, password="wrong-password-123")
    last = alerts.alerts[-1]
    assert last.title == "Login locked"
    assert last.priority == "high"


def test_alerts_never_contain_passwords(client: TestClient, admin, alerts) -> None:
    _login(client, password=PASSWORD + "-typo")
    _login(client, username=PASSWORD)
    for alert in alerts.alerts:
        assert PASSWORD not in alert.title + alert.message


def test_failed_setup_sends_an_alert(client: TestClient, alerts) -> None:
    client.post("/api/setup", json={"token": "x", "username": USERNAME, "password": PASSWORD})
    assert alerts.alerts[0].title == "Failed setup attempt"


def test_successful_login_sends_no_alert(client: TestClient, admin, alerts) -> None:
    assert _login(client).status_code == 200
    assert alerts.alerts == []


# ---------------------------------------------------------------- the ntfy sender


class _NtfyStub(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - required method name
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def ntfy_server():
    _NtfyStub.received = []
    server = HTTPServer(("127.0.0.1", 0), _NtfyStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _NtfyStub.received
    server.shutdown()


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "ntfy_token"
    path.write_text("tk_secret_test_token\n")
    return path


def test_ntfy_alerter_posts_with_the_access_token(tmp_path: Path, ntfy_server) -> None:
    url, received = ntfy_server
    alerter = NtfyAlerter(NtfyConfig(url, "stack-security", _token_file(tmp_path)))
    alerter.send(Alert("Login locked", "Too many failed logins", "high", ("rotating_light",)))
    assert alerter.flush()
    assert len(received) == 1
    request = received[0]
    assert request["path"] == "/stack-security"
    assert request["headers"]["Authorization"] == "Bearer tk_secret_test_token"
    assert request["headers"]["Title"] == "Login locked"
    assert request["headers"]["Priority"] == "high"
    assert request["headers"]["Tags"] == "rotating_light"
    assert request["body"] == "Too many failed logins"


def test_ntfy_alerts_are_throttled(tmp_path: Path, ntfy_server) -> None:
    url, received = ntfy_server
    alerter = NtfyAlerter(
        NtfyConfig(url, "t", _token_file(tmp_path)), max_per_window=3, window=timedelta(minutes=5)
    )
    for i in range(10):
        alerter.send(Alert("Failed login", f"attempt {i}"))
    assert alerter.flush()
    titles = [r["headers"]["Title"] for r in received]
    assert titles == ["Failed login"] * 3 + ["Security alerts suppressed"]


def test_unreachable_ntfy_does_not_break_login(client: TestClient, admin, tmp_path: Path) -> None:
    client.app.state.alerter = NtfyAlerter(
        NtfyConfig("http://127.0.0.1:9", "t", _token_file(tmp_path))
    )
    assert _login(client, password="wrong-password-123").status_code == 401
    assert _login(client).status_code == 200


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"NTFY_URL": "http://x:8085"}, "missing"),
        ({"NTFY_URL": "ftp://x", "NTFY_TOPIC": "t", "NTFY_TOKEN_FILE": "{file}"}, "NTFY_URL"),
        ({"NTFY_URL": "http://x", "NTFY_TOPIC": "a/b", "NTFY_TOKEN_FILE": "{file}"}, "TOPIC"),
        ({"NTFY_URL": "http://x", "NTFY_TOPIC": "t", "NTFY_TOKEN_FILE": "/nope"}, "not a file"),
    ],
)
def test_ntfy_config_is_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict, message: str
) -> None:
    token = _token_file(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value.replace("{file}", str(token)))
    with pytest.raises(ConfigError, match=message):
        Settings.from_env()


def test_ntfy_is_off_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().ntfy is None


# ---------------------------------------------------------------- password reset


@pytest.fixture
def run_reset(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys):
    """Run the reset-password command as it runs inside the container."""
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    for name in ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)

    def run(*args: str) -> tuple[int, str, str | None]:
        code = cli.reset_password(list(args))
        out = capsys.readouterr()
        match = re.search(re.escape(ORIGIN) + r"/#/reset/(\S+)", out.out)
        return code, out.out + out.err, match.group(1) if match else None

    return run


def _reset(client: TestClient, token: str, password: str = NEW_PASSWORD):
    return client.post("/api/password-reset", json={"token": token, "password": password})


def test_reset_command_needs_a_user(run_reset) -> None:
    code, output, token = run_reset()
    assert code == 1
    assert token is None
    assert "No users" in output


def test_reset_command_logs_everyone_out(client: TestClient, csrf: str, run_reset) -> None:
    assert client.get("/api/session").status_code == 200
    code, output, token = run_reset()
    assert code == 0
    assert token
    assert client.get("/api/session").status_code == 401


def test_full_reset_flow(client: TestClient, admin, run_reset, alerts) -> None:
    _, _, token = run_reset()
    response = _reset(client, token)
    assert response.status_code == 200
    assert response.json() == {"username": USERNAME}
    assert alerts.alerts[-1].title == "Password changed"
    assert _login(client).status_code == 401  # old password gone
    assert _login(client, password=NEW_PASSWORD).status_code == 200


def test_reset_link_works_once(client: TestClient, admin, run_reset) -> None:
    _, _, token = run_reset()
    assert _reset(client, token).status_code == 200
    assert _reset(client, token, "yet-another-passphrase-99").status_code == 400


def test_weak_password_keeps_the_link_valid(client: TestClient, admin, run_reset) -> None:
    _, _, token = run_reset()
    response = _reset(client, token, "short")
    assert response.status_code == 400
    assert "at least 12" in response.json()["detail"]
    assert _reset(client, token).status_code == 200


def test_reset_link_expires_after_15_minutes(
    client: TestClient, admin, run_reset, fake_clock: FakeClock
) -> None:
    _, _, token = run_reset()
    fake_clock.advance(minutes=16)
    assert _reset(client, token).status_code == 400


def test_new_link_replaces_the_old_one(client: TestClient, admin, run_reset) -> None:
    _, _, first = run_reset()
    _, _, second = run_reset()
    assert _reset(client, first).status_code == 400
    assert _reset(client, second).status_code == 200


def test_reset_token_is_only_stored_hashed(settings: Settings, admin, run_reset) -> None:
    _, _, token = run_reset()
    for file in settings.data_dir.iterdir():
        assert token.encode() not in file.read_bytes()
    conn = db.connect(settings.db_path)
    stored = conn.execute("SELECT token_hash FROM password_resets").fetchone()["token_hash"]
    assert stored == sessions.hash_token(token)


def test_reset_token_guessing_is_limited(client: TestClient, admin, run_reset) -> None:
    _, _, token = run_reset()
    for _ in range(20):
        assert _reset(client, "guess").status_code == 400
    assert _reset(client, token).status_code == 429


def test_reset_needs_an_allowed_origin(client: TestClient, admin, run_reset) -> None:
    _, _, token = run_reset()
    response = client.post(
        "/api/password-reset",
        json={"token": token, "password": NEW_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_reset_with_several_users_needs_a_username(settings: Settings, admin, run_reset) -> None:
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO users (username, password_hash) VALUES ('second', 'x')")
    code, output, _ = run_reset()
    assert code == 1
    assert "--username" in output
    code, _, token = run_reset("--username", USERNAME)
    assert code == 0
    assert token


def test_reset_is_audited(settings: Settings, client: TestClient, admin, run_reset) -> None:
    _, _, token = run_reset()
    _reset(client, "bad-token")
    _reset(client, token)
    conn = db.connect(settings.db_path)
    rows = [dict(r) for r in conn.execute("SELECT action, result, detail_json FROM audit_log")]
    actions = [(r["action"], r["result"]) for r in rows]
    assert ("password_reset_requested", "ok") in actions
    assert ("password_reset_failed", "denied") in actions
    assert ("password_reset", "ok") in actions
    assert token not in json.dumps(rows)
    assert NEW_PASSWORD not in json.dumps(rows)
