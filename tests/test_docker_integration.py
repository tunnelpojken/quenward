"""Phase 3 against a real Docker daemon. Opt-in:

    DOCKER_TESTS=1 uv run pytest tests/test_docker_integration.py

Creates throw-away containers (labelled like a compose stack) and removes them
afterwards. By default they run busybox; DOCKER_TEST_IMAGE and
DOCKER_TEST_COMMAND (a JSON list, "{name}" is replaced) pick something else.
"""

import json
import os
import secrets
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.stacks.gateway import (
    COMPOSE_PROJECT,
    COMPOSE_SERVICE,
    COMPOSE_WORKING_DIR,
    DockerGateway,
)
from tests.conftest import ORIGIN

pytestmark = pytest.mark.skipif(
    os.environ.get("DOCKER_TESTS") != "1", reason="set DOCKER_TESTS=1 to use a real Docker daemon"
)

# Prints a numbered line every second (with HTML and colour codes in it, which must
# arrive as plain text) and a warning on stderr every 5th line.
BUSYBOX_COMMAND = [
    "sh",
    "-c",
    'trap "exit 0" TERM; i=0; while true; do i=$((i+1)); '
    "printf '%s line %d <b>not html</b> \\033[32mgreen\\033[0m\\n' '{name}' \"$i\"; "
    "[ $((i % 5)) -eq 0 ] && echo '{name} warning' \"$i\" >&2; sleep 1; done",
]
IMAGE = os.environ.get("DOCKER_TEST_IMAGE", "busybox:1.37")
COMMAND = json.loads(os.environ.get("DOCKER_TEST_COMMAND", json.dumps(BUSYBOX_COMMAND)))

SECRETS = {
    "env": "itest-env-secret",
    "label": "itest-label-secret",
    "healthcheck": "itest-healthcheck-secret",
}


@pytest.fixture
def stack(settings: Settings) -> Iterator[dict]:
    import docker

    client = docker.from_env()
    project = f"itest{secrets.token_hex(4)}"
    folder = settings.stacks_dir / "itest"
    folder.mkdir(parents=True)
    (folder / "compose.yaml").write_text("services:\n  web: {}\n  worker: {}\n")

    def run(service: str):
        return client.containers.run(
            IMAGE,
            [part.replace("{name}", service) for part in COMMAND],
            name=f"{project}-{service}-1",
            detach=True,
            environment={"DB_PASSWORD": SECRETS["env"]},
            labels={
                COMPOSE_PROJECT: project,
                COMPOSE_SERVICE: service,
                COMPOSE_WORKING_DIR: str(folder),
                "custom.auth": SECRETS["label"],
            },
            healthcheck={
                "test": ["CMD", "/nonexistent", SECRETS["healthcheck"]],
                "interval": 1_000_000_000,
                "retries": 1,
            },
        )

    containers = {"web": run("web"), "worker": run("worker")}
    time.sleep(2.5)  # let them print a few lines
    try:
        yield {"project": project, "containers": containers, "folder": folder}
    finally:
        for c in containers.values():
            c.remove(force=True)


def _ws(client: TestClient, path: str):
    return client.websocket_connect(f"wss://testserver{path}", headers={"Origin": ORIGIN})


def test_real_stack_is_listed_without_secrets(client: TestClient, csrf: str, stack) -> None:
    listing = client.get("/api/stacks")
    detail = client.get("/api/stacks/itest")
    assert listing.status_code == detail.status_code == 200
    entry = listing.json()["stacks"][0]
    assert (entry["name"], entry["status"], entry["running"]) == ("itest", "running", 2)
    services = sorted(c["service"] for c in detail.json()["containers"])
    assert services == ["web", "worker"]
    for response in (listing, detail):
        for secret in SECRETS.values():
            assert secret not in response.text, f"{secret} leaked"


def test_real_logs_history_then_live(client: TestClient, csrf: str, stack) -> None:
    with _ws(client, "/ws/stacks/itest/logs?tail=2") as ws:
        history = ws.receive_json()["lines"]
        assert {line["s"] for line in history} == {"web", "worker"}
        assert ws.receive_json() == {"type": "live"}
        live = []
        deadline = time.monotonic() + 10
        while len(live) < 3 and time.monotonic() < deadline:
            message = ws.receive_json()
            if message["type"] == "lines":
                live += message["lines"]
        assert len(live) >= 3
        for line in history + live:
            assert "\x1b" not in line["m"]  # colour codes stripped
        sample = next(line["m"] for line in live if "line" in line["m"])
        assert "<b>not html</b>" in sample and "green" in sample  # text, untouched


def test_real_stopped_container_makes_stack_partial(client: TestClient, csrf: str, stack) -> None:
    stack["containers"]["worker"].stop(timeout=5)
    entry = client.get("/api/stacks").json()["stacks"][0]
    assert (entry["status"], entry["running"], entry["total"]) == ("partial", 1, 2)


def test_real_streams_are_released(client: TestClient, csrf: str, stack) -> None:
    for _ in range(3):
        with _ws(client, "/ws/stacks/itest/logs?tail=1") as ws:
            ws.receive_json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name.startswith("logs-")]:
            break
        time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name.startswith("logs-")]
    assert client.app.state.log_sockets == {}


def test_unreachable_docker(
    client: TestClient, csrf: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path}/missing.sock")
    client.app.state.docker = DockerGateway()
    (client.app.state.settings.stacks_dir / "x").mkdir(parents=True)
    (client.app.state.settings.stacks_dir / "x" / "compose.yaml").write_text("services: {}")
    response = client.get("/api/stacks")
    assert response.status_code == 503
    assert response.json() == {"detail": "Docker is not reachable"}
    with pytest.raises(WebSocketDisconnect) as e, _ws(client, "/ws/stacks/x/logs"):
        pass
    assert e.value.code == 1011


# ---------------------------------------------------------------- Phase 4: real actions


def _wait_job(client: TestClient, job_id: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish")


def _escape(part: str) -> str:
    return part.replace("$", "$$")  # compose interpolates $; the shell must see a plain $


@pytest.fixture
def real_stack(settings: Settings) -> Iterator[dict]:
    """A real compose stack whose image we can 'update' by rebuilding its tag."""
    import io
    import shutil
    import subprocess

    import docker

    client = docker.from_env()
    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        client.images.pull(IMAGE)
    name = f"acts{secrets.token_hex(4)}"
    repo = f"itest-{name}/app"

    def build(version: int) -> str:
        dockerfile = f"FROM {IMAGE}\nLABEL itest.version={version}\n".encode()
        return client.images.build(fileobj=io.BytesIO(dockerfile), tag=f"{repo}:latest")[0].id

    folder = settings.stacks_dir / name
    folder.mkdir(parents=True)

    def command(service: str) -> list[str]:
        return [_escape(p).replace("{name}", service) for p in COMMAND]

    # JSON is valid YAML. ${LEAK_TEST:-clean} is interpolated by compose on purpose.
    spec = {
        "services": {
            svc: {"image": f"{repo}:latest", "pull_policy": "never", "command": command(arg)}
            for svc, arg in (("web", "web"), ("worker", "${LEAK_TEST:-clean}"))
        }
    }
    (folder / "compose.yaml").write_text(json.dumps(spec, indent=2))
    info = {"name": name, "repo": repo, "folder": folder, "build": build, "v1": build(1)}
    try:
        yield info
    finally:
        subprocess.run(  # noqa: S603 - fixed arguments
            [
                shutil.which("docker") or "/usr/bin/docker",
                "compose",
                "--project-directory",
                str(folder),
                "down",
                "--timeout",
                "2",
            ],
            cwd=folder,
            capture_output=True,
        )
        for image in client.images.list(name=repo):
            for tag in image.tags:
                client.images.remove(tag, force=True)


def _containers(name: str) -> dict:
    import docker

    found = docker.from_env().containers.list(
        all=True, filters={"label": f"{COMPOSE_PROJECT}={name}"}
    )
    return {c.labels[COMPOSE_SERVICE]: c for c in found}


def _act(client: TestClient, csrf: str, name: str, action: str) -> dict:
    response = client.post(f"/api/stacks/{name}/{action}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202, response.text
    job = _wait_job(client, response.json()["id"])
    assert job["status"] == "ok", "\n".join(job["lines"])
    return job


def test_real_start_stop_restart(
    client: TestClient, csrf: str, real_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the app's environment leaked into compose, the worker would print "leaked".
    monkeypatch.setenv("LEAK_TEST", "leaked")
    name = real_stack["name"]

    _act(client, csrf, name, "start")
    containers = _containers(name)
    assert {s: c.status for s, c in containers.items()} == {"web": "running", "worker": "running"}
    time.sleep(1.5)
    worker_log = containers["worker"].logs().decode()
    assert "clean" in worker_log and "leaked" not in worker_log

    _act(client, csrf, name, "stop")
    assert {c.status for c in _containers(name).values()} == {"exited"}

    started_before = containers["web"].attrs["State"]["StartedAt"]
    _act(client, csrf, name, "restart")
    after = _containers(name)
    assert {c.status for c in after.values()} == {"running"}
    assert after["web"].attrs["State"]["StartedAt"] != started_before


def test_real_update_keeps_exactly_one_previous_image(
    client: TestClient, csrf: str, real_stack
) -> None:
    import docker

    images = docker.from_env().images
    name, repo, build = real_stack["name"], real_stack["repo"], real_stack["build"]
    v1 = real_stack["v1"]
    rollback = f"{repo}:rollback-{name}-web"
    _act(client, csrf, name, "start")

    job = _act(client, csrf, name, "update")  # nothing new yet
    assert "web: already up to date" in job["summary"]
    with pytest.raises(docker.errors.ImageNotFound):
        images.get(rollback)

    v2 = build(2)  # stands in for a pull that brought a new image
    job = _act(client, csrf, name, "update")
    assert _containers(name)["web"].image.id == v2
    assert images.get(rollback).id == v1
    assert (
        f"web: {v1.removeprefix('sha256:')[:12]} → {v2.removeprefix('sha256:')[:12]}"
        in (job["summary"])
    )

    v3 = build(3)
    _act(client, csrf, name, "update")
    assert _containers(name)["web"].image.id == v3
    assert images.get(rollback).id == v2
    with pytest.raises(docker.errors.ImageNotFound):  # the generation before is gone
        images.get(v1)
    leftovers = [t for i in images.list(name=repo) for t in i.tags if ":pending-" in t]
    assert leftovers == []


def test_real_update_after_a_pull_outside_the_app(
    client: TestClient, csrf: str, real_stack
) -> None:
    """The tag moves before Update is clicked (Dockge, a manual pull). On the
    containerd image store that deletes the old image at once, unless the app's
    `current` tag is keeping it alive."""
    import docker

    images = docker.from_env().images
    name, repo, build = real_stack["name"], real_stack["repo"], real_stack["build"]
    v1 = real_stack["v1"]
    _act(client, csrf, name, "start")  # tags the running images as current
    v2 = build(2)  # the outside "pull"
    job = _act(client, csrf, name, "update")
    assert not any("could not be kept" in line for line in job["summary"])
    assert _containers(name)["web"].image.id == v2
    assert images.get(f"{repo}:rollback-{name}-web").id == v1


def test_real_updates_never_delete_another_stacks_image(
    client: TestClient, csrf: str, real_stack, settings: Settings
) -> None:
    """Two stacks share an image and both keep v1 as their rollback. Updating one
    of them again must not delete v1: it is still the other stack's rollback."""
    import subprocess

    import docker

    images = docker.from_env().images
    name, repo, build = real_stack["name"], real_stack["repo"], real_stack["build"]
    v1 = real_stack["v1"]
    other = f"{name}b"
    folder = settings.stacks_dir / other
    folder.mkdir()
    spec = json.loads((real_stack["folder"] / "compose.yaml").read_text())
    spec["services"] = {"db": spec["services"]["web"]}
    (folder / "compose.yaml").write_text(json.dumps(spec))
    try:
        _act(client, csrf, name, "start")
        _act(client, csrf, other, "start")
        build(2)
        _act(client, csrf, name, "update")
        _act(client, csrf, other, "update")  # both now run v2 and keep v1 as rollback
        assert images.get(f"{repo}:rollback-{other}-db").id == v1
        build(3)
        _act(client, csrf, name, "update")  # cleans up its own v1 ... but v1 is shared
        assert images.get(f"{repo}:rollback-{name}-web").id != v1
        assert images.get(f"{repo}:rollback-{other}-db").id == v1  # the other stack's is intact
    finally:
        subprocess.run(  # noqa: S603 - fixed arguments
            [
                shutil.which("docker") or "/usr/bin/docker",
                "compose",
                "--project-directory",
                str(folder),
                "down",
                "--timeout",
                "2",
            ],
            cwd=folder,
            capture_output=True,
        )


# ---------------------------------------------------------------- Phase 5: terminal


def _live_pids(container) -> set[str]:
    """Host pids of the container's processes, without zombies (already dead)."""
    container.reload()
    top = container.top(ps_args="-o pid,stat")
    return {p[0] for p in top["Processes"] if not p[1].startswith("Z")}


def test_real_terminal_runs_commands_and_closing_ends_everything(
    client: TestClient, csrf: str, stack
) -> None:
    from tests.test_terminal import Term

    web = stack["containers"]["web"]
    before = _live_pids(web)
    with _ws(client, f"/ws/stacks/itest/terminal?container={web.id[:12]}&rows=30&cols=100") as ws:
        t = Term(ws)
        t.read_until(b"# ")
        assert t.control[0]["type"] == "ready"
        assert t.control[0]["shell"] == "sh"  # busybox has no bash: the fallback
        assert b"\x1e" not in t.output
        # Evaluated by the shell, not just echoed back: 40+2 must become 42.
        t.type("echo result-$((40+2))\n")
        t.read_until(b"result-42")
        t.type("stty size\n")
        t.read_until(b"30 100")
        # Things started from the shell, including one that ignores a hangup.
        t.type("sleep 901 & (trap '' HUP; sleep 902) &\n")
        t.type("echo started\n")
        t.read_until(b"started\r\n")
        during = _live_pids(web)
        assert len(during - before) >= 3, "the shell and both background jobs"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _live_pids(web) & (during - before):
        time.sleep(0.2)
    assert not _live_pids(web) & (during - before), "processes outlived the terminal"


def test_real_terminal_exit_reports_the_code(client: TestClient, csrf: str, stack) -> None:
    from tests.test_terminal import Term

    worker = stack["containers"]["worker"]
    with _ws(client, f"/ws/stacks/itest/terminal?container={worker.name}") as ws:
        t = Term(ws)
        t.read_until(b"# ")
        t.type("exit 7\n")
        assert t.wait_closed() == 4410
        assert {"type": "exit", "code": 7} in t.control
