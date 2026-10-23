"""Phase 4: stack actions, the per-stack lock, updates with a kept previous image.

Uses FakeGateway plus a fake compose runner, so it runs without Docker.
tests/test_docker_integration.py runs the same flows with real compose.
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import audit, db
from app.config import Settings
from app.stacks import compose
from app.stacks.actions import JobManager, repository
from tests.fake_docker import FakeGateway, container_attrs


class FakeRunner:
    """Stands in for compose.run. Can simulate pulls, failures and slow commands."""

    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.calls: list[tuple[list[str], Path]] = []
        self.new_images: dict[str, str] = {}  # ref -> id a pull will bring
        self.fail: set[str] = set()  # subcommands that exit 1
        self.block = threading.Event()
        self.block.set()

    def __call__(self, args, cwd, on_line, timeout) -> compose.Result:
        self.calls.append((args, cwd))
        command = args[args.index("--project-name") + 2]
        on_line(f"fake compose {command}")
        self.block.wait(10)
        if command in self.fail:
            on_line("something went wrong")
            return compose.Result(exit_code=1)
        if command == "pull":
            for ref, image in self.new_images.items():
                self.gateway.images[ref] = image
        if command == "up":
            for c in self.gateway.containers:
                c["ImageID"] = self.gateway.images.get(c["Image"], c["ImageID"])
        return compose.Result(exit_code=0)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


PIHOLE_ID = "aaaaaaaaaaaa"
CURRENT = "example/pihole:current-pihole-pihole"
ROLLBACK = "example/pihole:rollback-pihole-pihole"


@pytest.fixture
def setup(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch):
    d = settings.stacks_dir
    _write(d / "pihole" / "compose.yaml", "services:\n  pihole: {}\n")
    _write(d / "web" / "compose.yaml", "services:\n  site: {}\n  builder:\n    build: .\n")
    _write(d / "third" / "compose.yaml", "services:\n  x: {}\n")
    gateway = FakeGateway(
        containers=[
            container_attrs(PIHOLE_ID, "pihole", "pihole", str(d / "pihole")),
            container_attrs("bbbbbbbbbbbb", "site", "web", str(d / "web")),
        ],
        images={"example/pihole:1.0": "sha256:pihole-v1", "example/site:1.0": "sha256:site-v1"},
    )
    runner = FakeRunner(gateway)
    client.app.state.docker = gateway
    client.app.state.jobs = JobManager(settings, lambda: client.app.state.docker, runner)
    monkeypatch.setattr(compose, "docker_binary", lambda: "/usr/bin/docker")
    return gateway, runner


def _act(client: TestClient, csrf: str, stack: str, action: str):
    return client.post(f"/api/stacks/{stack}/{action}", headers={"X-CSRF-Token": csrf})


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _audit(settings: Settings) -> list[tuple[str, str, str | None]]:
    conn = db.connect(settings.db_path)
    return [
        (r["action"], r["result"], r["target_stack"])
        for r in conn.execute("SELECT * FROM audit_log WHERE action LIKE 'stack_%' ORDER BY id")
    ]


# ---------------------------------------------------------------- access


def test_actions_need_login(client: TestClient, setup) -> None:
    assert client.post("/api/stacks/pihole/stop").status_code == 401


def test_actions_need_the_csrf_token(client: TestClient, csrf: str, setup) -> None:
    _, runner = setup
    assert client.post("/api/stacks/pihole/stop").status_code == 403
    assert client.post("/api/stacks/pihole/stop", headers={"X-CSRF-Token": "x"}).status_code == 403
    assert runner.calls == []


def test_actions_refuse_foreign_origins(client: TestClient, csrf: str, setup) -> None:
    _, runner = setup
    response = client.post(
        "/api/stacks/pihole/stop",
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert runner.calls == []


def test_unknown_action_and_stack(client: TestClient, csrf: str, setup) -> None:
    assert _act(client, csrf, "pihole", "rm").status_code == 422
    assert _act(client, csrf, "pihole", "down").status_code == 422
    assert _act(client, csrf, "nope", "stop").status_code == 404
    # "../stop" never reaches an action route at all (the path is normalised first).
    assert _act(client, csrf, "..", "stop").status_code in (404, 405)
    assert _act(client, csrf, "%2e%2e", "stop").status_code == 404
    assert setup[1].calls == []  # nothing ran


# ---------------------------------------------------------------- running actions


@pytest.mark.parametrize(
    ("action", "command"),
    [("start", ["up", "--detach"]), ("stop", ["stop"]), ("restart", ["restart"])],
)
def test_simple_actions(
    client: TestClient, csrf: str, setup, settings: Settings, action: str, command: list[str]
) -> None:
    _, runner = setup
    response = _act(client, csrf, "pihole", action)
    assert response.status_code == 202
    job = _wait(client, response.json()["id"])
    assert job["status"] == "ok"
    assert job["lines"] == [f"$ docker compose {' '.join(command)}", f"fake compose {command[0]}"]
    args, cwd = runner.calls[0]
    folder = str(settings.stacks_dir / "pihole")
    assert args[:2] == ["/usr/bin/docker", "compose"]
    assert args[args.index("--project-directory") + 1] == folder
    assert args[args.index("--file") + 1] == f"{folder}/compose.yaml"
    assert args[args.index("--project-name") + 1] == "pihole"
    assert args[-len(command) :] == command
    assert cwd == settings.stacks_dir / "pihole"
    assert _audit(settings) == [
        (f"stack_{action}", "started", "pihole"),
        (f"stack_{action}", "ok", "pihole"),
    ]


def test_failed_action_is_reported_and_audited(
    client: TestClient, csrf: str, setup, settings: Settings
) -> None:
    _, runner = setup
    runner.fail.add("stop")
    job = _wait(client, _act(client, csrf, "pihole", "stop").json()["id"])
    assert job["status"] == "error"
    assert "something went wrong" in job["lines"]
    assert _audit(settings)[-1] == ("stack_stop", "error", "pihole")


def test_missing_docker_cli_is_reported(
    client: TestClient, csrf: str, setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> str:
        raise compose.ComposeError("The docker command is not installed in this container")

    monkeypatch.setattr(compose, "docker_binary", missing)
    job = _wait(client, _act(client, csrf, "pihole", "stop").json()["id"])
    assert job["status"] == "error"
    assert job["lines"] == ["The docker command is not installed in this container"]


def test_own_stack_is_refused(client: TestClient, csrf: str, setup, settings: Settings) -> None:
    gateway, runner = setup
    gateway.self_id = PIHOLE_ID
    for action in ("start", "stop", "restart", "update"):
        response = _act(client, csrf, "pihole", action)
        assert response.status_code == 403
        assert "runs in" in response.json()["detail"]["message"]
    assert runner.calls == []
    assert {a[1] for a in _audit(settings)} == {"denied"}


def test_one_action_per_stack(client: TestClient, csrf: str, setup) -> None:
    _, runner = setup
    runner.block.clear()  # the first action hangs until released
    first = _act(client, csrf, "pihole", "restart").json()
    second = _act(client, csrf, "pihole", "stop")
    assert second.status_code == 409
    assert second.json()["detail"]["job"] == first["id"]
    assert client.get("/api/stacks/pihole").json()["job"] == first["id"]
    runner.block.set()
    _wait(client, first["id"])
    assert client.get("/api/stacks/pihole").json()["job"] is None
    assert _act(client, csrf, "pihole", "stop").status_code == 202


def test_limited_number_of_actions_overall(client: TestClient, csrf: str, setup) -> None:
    _, runner = setup
    runner.block.clear()
    assert _act(client, csrf, "pihole", "restart").status_code == 202
    assert _act(client, csrf, "web", "restart").status_code == 202
    assert _act(client, csrf, "third", "start").status_code == 429
    runner.block.set()


def test_job_output_pages(client: TestClient, csrf: str, setup) -> None:
    job = _wait(client, _act(client, csrf, "pihole", "stop").json()["id"])
    assert job["next"] == 2
    rest = client.get(f"/api/jobs/{job['id']}?after=1").json()
    assert rest["lines"] == ["fake compose stop"]
    assert client.get("/api/jobs/0123456789abcdef").status_code == 404
    assert client.get("/api/jobs/../etc").status_code == 404


# ---------------------------------------------------------------- update


def test_update_keeps_the_previous_image(
    client: TestClient, csrf: str, setup, settings: Settings
) -> None:
    gateway, runner = setup
    runner.new_images = {"example/pihole:1.0": "sha256:pihole-v2"}
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert job["status"] == "ok"
    assert [line for line in job["lines"] if line.startswith("$")] == [
        "$ docker compose pull --ignore-buildable",
        "$ docker compose up --detach",
    ]
    assert job["summary"] == ["pihole: pihole-v1 → pihole-v2"]
    # The old image is kept under one rollback tag; the temporary tag is gone.
    assert gateway.images["example/pihole:rollback-pihole-pihole"] == "sha256:pihole-v1"
    assert "example/pihole:pending-pihole-pihole" not in gateway.images
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM image_history").fetchone()
    assert (row["service"], row["old_image_id"], row["new_image_id"]) == (
        "pihole",
        "sha256:pihole-v1",
        "sha256:pihole-v2",
    )


def test_update_with_nothing_new(client: TestClient, csrf: str, setup) -> None:
    gateway, _ = setup
    before = dict(gateway.images)
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert job["summary"] == ["pihole: already up to date"]
    # No rollback tag and no leftover temporary tag; only the protective current tag.
    assert gateway.images == {**before, CURRENT: "sha256:pihole-v1"}


def test_second_update_removes_the_older_image(client: TestClient, csrf: str, setup) -> None:
    gateway, runner = setup
    runner.new_images = {"example/pihole:1.0": "sha256:pihole-v2"}
    _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    runner.new_images = {"example/pihole:1.0": "sha256:pihole-v3"}
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert job["summary"] == [
        "pihole: pihole-v2 → pihole-v3",
        "pihole: removed the older previous image",
    ]
    assert gateway.images["example/pihole:rollback-pihole-pihole"] == "sha256:pihole-v2"
    assert gateway.removed == ["sha256:pihole-v1"]  # exactly one generation kept


def test_failed_pull_changes_nothing(client: TestClient, csrf: str, setup) -> None:
    gateway, runner = setup
    runner.fail.add("pull")
    before = dict(gateway.images)
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert job["status"] == "error"
    assert not any(line == "$ docker compose up --detach" for line in job["lines"])
    assert gateway.images == {**before, CURRENT: "sha256:pihole-v1"}


def test_image_still_running_after_failed_up_is_kept(client: TestClient, csrf: str, setup) -> None:
    """Pull moved the tag but `up` failed: the running image must not be lost."""
    gateway, runner = setup
    runner.new_images = {"example/pihole:1.0": "sha256:pihole-v2"}
    runner.fail.add("up")
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert job["status"] == "error"
    assert gateway.images["example/pihole:rollback-pihole-pihole"] == "sha256:pihole-v1"
    assert gateway.containers[0]["ImageID"] == "sha256:pihole-v1"


def test_locally_built_services_are_skipped(client: TestClient, csrf: str, setup) -> None:
    job = _wait(client, _act(client, csrf, "web", "update").json()["id"])
    assert "builder: skipped (built locally)" in job["summary"]


def test_audit_has_the_changes_but_no_output(
    client: TestClient, csrf: str, setup, settings: Settings
) -> None:
    _, runner = setup
    runner.new_images = {"example/pihole:1.0": "sha256:pihole-v2"}
    _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    conn = db.connect(settings.db_path)
    detail = json.loads(
        conn.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'stack_update' AND result = 'ok'"
        ).fetchone()["detail_json"]
    )
    assert detail["changes"] == ["pihole: pihole-v1 → pihole-v2"]
    assert "fake compose" not in json.dumps(detail)


def test_audit_is_written_before_the_job_reports_finished(
    client: TestClient, csrf: str, setup, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A slow audit write (a busy disk, a shared CI runner) must not let anyone see
    # the job finished while its closing entry is still missing.
    slow_record = audit.record

    def record(conn, action, result, **kwargs):
        if result != "started":
            time.sleep(0.3)
        return slow_record(conn, action, result, **kwargs)

    monkeypatch.setattr(audit, "record", record)
    _wait(client, _act(client, csrf, "pihole", "restart").json()["id"])
    assert ("stack_restart", "ok", "pihole") in _audit(settings)


# ---------------------------------------------------------------- the compose runner itself


@pytest.mark.parametrize(
    ("ref", "repo"),
    [
        ("nginx", "nginx"),
        ("nginx:1.27", "nginx"),
        ("ghcr.io/user/app:latest", "ghcr.io/user/app"),
        ("localhost:5000/app", "localhost:5000/app"),
        ("localhost:5000/app:2", "localhost:5000/app"),
        ("nginx@sha256:abc", "nginx"),
        ("sha256:abc", None),
        ("", None),
    ],
)
def test_repository(ref: str, repo: str | None) -> None:
    assert repository(ref) == repo


def test_compose_gets_a_clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "5005")
    monkeypatch.setenv("NTFY_TOKEN_FILE", "/run/secrets/ntfy")
    monkeypatch.setenv("DOCKER_HOST", "tcp://socket-proxy:2375")
    env = compose.clean_environment()
    assert env["DOCKER_HOST"] == "tcp://socket-proxy:2375"
    assert "PORT" not in env
    assert "NTFY_TOKEN_FILE" not in env


def test_compose_never_uses_a_shell(tmp_path: Path) -> None:
    lines: list[str] = []
    result = compose.run(["/bin/echo", "$(id)", "; rm -rf /"], tmp_path, lines.append, 5)
    assert result.ok
    assert lines == ["$(id) ; rm -rf /"]  # printed literally, never executed


def test_compose_timeout_kills_the_process(tmp_path: Path) -> None:
    lines: list[str] = []
    started = time.monotonic()
    result = compose.run(["/bin/sleep", "30"], tmp_path, lines.append, 0.5)
    assert time.monotonic() - started < 5
    assert result.timed_out and not result.ok
    assert lines[-1].startswith("Stopped after")


def test_invalid_project_names_are_refused(settings: Settings) -> None:
    from app.stacks.discovery import Stack

    stack = Stack(
        name="x", path=settings.stacks_dir / "x", compose_file="compose.yaml", project="x"
    )
    for bad in ["", "-x", "x y", "x;id", "../x", "X"]:
        with pytest.raises(compose.ComposeError):
            compose.base_args(stack, bad)


def test_compose_output_is_cleaned(tmp_path: Path) -> None:
    lines: list[str] = []
    compose.run(["/bin/printf", "\\033[31mred\\033[0m\\n\\n"], tmp_path, lines.append, 5)
    assert lines == ["red"]


def test_environment_has_no_unexpected_variables() -> None:
    allowed = {"PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
    allowed.add("DOCKER_CONFIG")
    assert set(compose.clean_environment()) <= allowed
    assert os.environ  # the test process itself has plenty


# ---------------------------------------------------------------- tags moved outside the app


def test_start_marks_the_running_images(client: TestClient, csrf: str, setup) -> None:
    gateway, _ = setup
    _wait(client, _act(client, csrf, "pihole", "start").json()["id"])
    assert gateway.images[CURRENT] == "sha256:pihole-v1"


def test_previous_image_survives_a_pull_outside_the_app(
    client: TestClient, csrf: str, setup
) -> None:
    """Dockge or a manual `docker pull` moved the tag first. On the containerd image
    store the old image object is then gone, but the current tag kept it alive."""
    gateway, _ = setup
    _wait(client, _act(client, csrf, "pihole", "start").json()["id"])  # sets the current tag
    gateway.images["example/pihole:1.0"] = "sha256:pihole-v2"  # the outside pull
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert job["summary"] == ["pihole: pihole-v1 → pihole-v2"]
    assert gateway.images[ROLLBACK] == "sha256:pihole-v1"
    assert gateway.images[CURRENT] == "sha256:pihole-v2"


def test_lost_previous_image_is_reported_not_hidden(client: TestClient, csrf: str, setup) -> None:
    """No current tag yet and the old image is gone: say so instead of skipping silently."""
    gateway, _ = setup
    gateway.images["example/pihole:1.0"] = "sha256:pihole-v2"
    gateway.removed.append("sha256:pihole-v1")
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert job["summary"][0].startswith("pihole: the previous image could not be kept")
    assert ROLLBACK not in gateway.images


def test_shared_images_keep_other_stacks_rollback(client: TestClient, csrf: str, setup) -> None:
    """Two stacks on the same image: updating one must not delete the other's rollback."""
    gateway, runner = setup
    other_rollback = "example/pihole:rollback-otherstack-db"
    gateway.images[ROLLBACK] = "sha256:pihole-v0"
    gateway.images[other_rollback] = "sha256:pihole-v0"  # another stack's previous image
    runner.new_images = {"example/pihole:1.0": "sha256:pihole-v2"}
    job = _wait(client, _act(client, csrf, "pihole", "update").json()["id"])
    assert gateway.images[ROLLBACK] == "sha256:pihole-v1"
    assert gateway.images[other_rollback] == "sha256:pihole-v0"  # untouched
    assert "sha256:pihole-v0" not in gateway.removed
    assert not any("removed" in line for line in job["summary"])
