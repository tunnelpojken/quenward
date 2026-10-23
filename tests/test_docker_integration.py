"""Phase 3 against a real Docker daemon. Opt-in:

    DOCKER_TESTS=1 uv run pytest tests/test_docker_integration.py

Creates throw-away containers (labelled like a compose stack) and removes them
afterwards. By default they run busybox; DOCKER_TEST_IMAGE and
DOCKER_TEST_COMMAND (a JSON list, "{name}" is replaced) pick something else.
"""

import json
import os
import secrets
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
