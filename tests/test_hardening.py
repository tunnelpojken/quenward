"""Fixes from the independent security review before v0.1.0, one test per finding.

1. Request bodies are capped before anything reads them.
2. Login attempts are reserved before the slow password check, and only a few
   password checks run at once.
3. A lockout is written to the audit log once, not for every refused request.
4. No other stack can act on the app's own compose project.
5. Values from a stack's .env never reach the browser in compose's messages.
6. X-Forwarded-For is only trusted from a configured proxy.
7. A Docker hiccup does not switch off the app's recognition of its own stack.
"""

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.auth import passwords
from app.auth.ratelimit import LoginLimiter
from app.config import Settings
from app.stacks import compose, editor, gateway
from app.stacks.actions import JobManager
from app.stacks.discovery import discover
from app.stacks.gateway import DockerGateway, DockerUnavailable
from tests.conftest import USERNAME
from tests.fake_docker import FakeGateway, container_attrs
from tests.test_actions import FakeRunner, _write

# ------------------------------------------------------------- 1. body size


def test_oversized_body_is_refused_before_it_is_read(client: TestClient) -> None:
    # No Origin, no login: the body must be refused before any route code runs.
    response = client.post(
        "/api/login",
        content=b"x" * (2 * 1024 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["content-security-policy"]  # security headers still apply


def test_oversized_chunked_body_is_refused(client: TestClient) -> None:
    def chunks():  # no Content-Length: sent chunked
        for _ in range(3):
            yield b" " * (512 * 1024)

    response = client.post(
        "/api/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_a_full_size_compose_file_still_fits(client: TestClient, csrf: str) -> None:
    # Nearly 256 KiB of compose text that JSON escaping doubles (every quote and
    # newline becomes two characters) must still get past the body cap: the
    # route answers (404, no such stack) instead of the cap's 413.
    client.app.state.docker = FakeGateway()
    text = '"\n' * (127 * 1024)
    response = client.put(
        "/api/stacks/none/compose",
        headers={"X-CSRF-Token": csrf},
        json={"content": text, "etag": "0" * 64},
    )
    assert len(response.request.content) > 500 * 1024
    assert response.status_code == 404


# ------------------------------------------------------------- 2. login race


def test_attempts_being_checked_count_towards_the_limit() -> None:
    limiter = LoginLimiter()
    assert [limiter.begin("fested", "10.0.0.1") for _ in range(5)] == ["ok"] * 5
    # Five checks in flight: a sixth parallel attempt must not start.
    assert limiter.begin("fested", "10.0.0.1") == "busy"
    for _ in range(5):
        limiter.record_failure("fested", "10.0.0.1")
        limiter.end("fested", "10.0.0.1")
    assert limiter.begin("fested", "10.0.0.1") == "locked_first"


def test_parallel_logins_get_no_extra_guesses(
    client: TestClient, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    real = passwords.verify_password

    def slow_verify(*args, **kwargs):
        gate.wait(5)  # every request is inside the check at the same time
        return real(*args, **kwargs)

    monkeypatch.setattr(passwords, "verify_password", slow_verify)
    codes: list[int] = []

    def attempt() -> None:
        r = client.post("/api/login", json={"username": USERNAME, "password": "wrong-guess-123"})
        codes.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    while sum(1 for c in codes if c == 429) < 7:  # the ones over the limit return at once
        threading.Event().wait(0.01)
    gate.set()
    for t in threads:
        t.join()
    assert codes.count(401) == 5  # exactly the limit got a password check
    assert codes.count(429) == 7


def test_only_a_few_password_checks_run_at_once() -> None:
    started = threading.Barrier(passwords.MAX_PARALLEL + 1)
    release = threading.Event()

    def hold() -> None:
        with passwords._slot(wait=1):
            started.wait()
            release.wait(5)

    holders = [threading.Thread(target=hold) for _ in range(passwords.MAX_PARALLEL)]
    for t in holders:
        t.start()
    started.wait()
    try:
        with pytest.raises(passwords.Busy):
            passwords.verify_password(None, "anything-at-all", wait=0.05)
    finally:
        release.set()
        for t in holders:
            t.join()


def test_a_busy_password_check_answers_429(
    client: TestClient, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*args, **kwargs):
        raise passwords.Busy

    monkeypatch.setattr(passwords, "verify_password", busy)
    r = client.post("/api/login", json={"username": USERNAME, "password": "whatever-123"})
    assert r.status_code == 429
    # A refused check is not a failed guess: the account is not closer to a lock.
    assert client.app.state.limiter.begin(USERNAME, "testclient") == "ok"


# ------------------------------------------------------------- 3. audit growth


def test_a_lockout_is_audited_once_not_per_request(
    client: TestClient, settings: Settings, admin
) -> None:
    for _ in range(5):
        client.post("/api/login", json={"username": USERNAME, "password": "wrong-guess-123"})
    for _ in range(50):
        r = client.post("/api/login", json={"username": USERNAME, "password": "x"})
        assert r.status_code == 429
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'login_blocked'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 1


# ------------------------------------------------------------- 4. own project


@pytest.fixture
def own_stack(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch):
    d = settings.stacks_dir
    _write(d / "quenward" / "compose.yaml", "services:\n  app: {}\n")
    gw = FakeGateway(
        containers=[container_attrs("aaaaaaaaaaaa", "app", "quenward", str(d / "quenward"))],
        self_id="aaaaaaaaaaaa",
    )
    runner = FakeRunner(gw)
    client.app.state.docker = gw
    client.app.state.jobs = JobManager(settings, lambda: client.app.state.docker, runner)
    monkeypatch.setattr(compose, "docker_binary", lambda: "/usr/bin/docker")
    return runner


def test_no_stack_can_be_saved_under_the_apps_project(
    client: TestClient, settings: Settings, csrf: str, own_stack
) -> None:
    _write(settings.stacks_dir / "other" / "compose.yaml", "services:\n  x: {}\n")
    current = client.get("/api/stacks/other/compose").json()
    r = client.put(
        "/api/stacks/other/compose",
        headers={"X-CSRF-Token": csrf},
        json={"content": "name: quenward\nservices:\n  app: {}\n", "etag": current["etag"]},
    )
    assert r.status_code == 422
    assert "own stack" in r.text
    assert "name: quenward" not in (settings.stacks_dir / "other" / "compose.yaml").read_text()


def test_a_stack_already_using_the_apps_project_is_read_only(
    client: TestClient, settings: Settings, csrf: str, own_stack
) -> None:
    # Edited over SSH, or a copy of the app's own compose file.
    _write(settings.stacks_dir / "copy" / "compose.yaml", "name: quenward\nservices:\n  app: {}\n")
    r = client.post("/api/stacks/copy/stop", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403
    assert own_stack.calls == []
    assert client.get("/api/stacks/copy/compose").json()["editable"] is False


# ------------------------------------------------------------- 5. .env values


def test_env_values_are_hidden_in_compose_messages(tmp_path: Path) -> None:
    folder = tmp_path / "stacks" / "db"
    _write(folder / "compose.yaml", "services:\n  db: {}\n")
    (folder / ".env").write_text('# comment\nDB_PASSWORD="hunter2-topsecret"\nDEBUG=1\n')
    stack = discover(tmp_path / "stacks")["db"]

    def runner(args, cwd, on_line, timeout):
        on_line("service db: invalid containerPort: hunter2-topsecret")
        on_line("debug is 1")
        return compose.Result(exit_code=1)

    with pytest.raises(editor.EditRefused) as refused:
        editor.check_compose(stack, folder / "compose.yaml", b"x", runner)
    output = "\n".join(refused.value.output)
    assert "hunter2-topsecret" not in output
    assert "[value from .env]" in output
    assert "debug is 1" in output  # values this short are left alone


# ------------------------------------------------------------- 6. proxy headers


@pytest.mark.parametrize(
    ("env", "expected"),
    [(None, (False, None)), ("10.46.33.2", (True, "10.46.33.2"))],
)
def test_forwarded_for_is_only_trusted_from_a_configured_proxy(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, env, expected
) -> None:
    seen = {}
    monkeypatch.setattr(main.uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(main.Settings, "from_env", classmethod(lambda cls: settings))
    if env is None:
        monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    else:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", env)
    main.run()
    assert (seen["proxy_headers"], seen["forwarded_allow_ips"]) == expected


def test_outside_a_container_it_only_listens_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.setattr(main, "Path", lambda p: type("P", (), {"exists": lambda self: False})())
    assert main._listen_address() == "127.0.0.1"


# ------------------------------------------------------------- 7. own id caching


def test_a_docker_hiccup_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "_in_container", lambda: True)
    monkeypatch.delenv("SELF_CONTAINER_ID", raising=False)
    gw = DockerGateway()
    answers = [DockerUnavailable("timeout"), {"id": "aaaaaaaaaaaa"}]

    def inspect(_):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gw, "inspect", inspect)
    assert gw.self_container_id() is None  # Docker did not answer this time
    assert gw.self_container_id() == "aaaaaaaaaaaa"  # asked again, and remembered
    assert gw.self_container_id() == "aaaaaaaaaaaa"
