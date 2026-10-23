"""Phase 3: stack discovery, container details, and the authenticated log stream.

These use FakeGateway, so they run without Docker. tests/test_docker_integration.py
repeats the important ones against a real Docker daemon.
"""

import dataclasses
import json
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.stacks import discovery, logs
from tests.conftest import ORIGIN, FakeClock
from tests.fake_docker import SECRETS, FakeGateway, container_attrs

# ---------------------------------------------------------------- fixtures


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # Check the session of open log streams often, so expiry tests are fast.
    return dataclasses.replace(settings, ws_session_check=timedelta(milliseconds=50))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def stacks_dir(settings: Settings) -> Path:
    d = settings.stacks_dir
    _write(d / "pihole" / "compose.yaml", "services:\n  pihole: {}\n  unbound: {}\n")
    _write(d / "BookStack" / "docker-compose.yml", "services:\n  app: {}\n  db: {}\n")
    _write(d / "renamed" / "compose.yml", "name: Custom_Name\nservices:\n  web: {}\n")
    _write(d / "broken" / "compose.yaml", "services: [unclosed\n")
    _write(d / "pyobj" / "compose.yaml", "services: !!python/object/apply:os.system ['id']\n")
    _write(d / "notastack" / "README.md", "no compose file here")
    _write(d / ".hidden" / "compose.yaml", "services:\n  x: {}\n")
    (d / "evil").mkdir()
    (d / "evil" / "compose.yaml").symlink_to("/etc/passwd")
    outside = d.parent / "outside"
    _write(outside / "compose.yaml", "services:\n  x: {}\n")
    (d / "linked").symlink_to(outside)
    return d


PIHOLE_ID = "aaaaaaaaaaaa"
UNBOUND_ID = "bbbbbbbbbbbb"
BOOK_ID = "cccccccccccc"


@pytest.fixture
def fake(client: TestClient, stacks_dir: Path) -> FakeGateway:
    gateway = FakeGateway(
        containers=[
            container_attrs(PIHOLE_ID, "pihole", "pihole", str(stacks_dir / "pihole")),
            container_attrs(UNBOUND_ID, "unbound", "pihole", str(stacks_dir / "pihole")),
            container_attrs(
                BOOK_ID, "app", "bookstack", str(stacks_dir / "BookStack"), "exited", "Exited (1)"
            ),
            # Same project name as a stack, but from a folder that is not ours.
            container_attrs("dddddddddddd", "web", "pihole", "/somewhere/else/pihole"),
        ],
        logs={
            PIHOLE_ID: b"2026-10-23T06:00:00.5Z pihole first\n2026-10-23T06:00:02Z pihole third\n",
            UNBOUND_ID: b"2026-10-23T06:00:01.25Z unbound second \x1b[31mred\x1b[0m\x07\n",
        },
    )
    client.app.state.docker = gateway
    return gateway


def _ws(client: TestClient, path: str):
    # wss: the session cookie is Secure, so the browser only sends it over TLS.
    return client.websocket_connect(f"wss://testserver{path}", headers={"Origin": ORIGIN})


def _until(condition, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# ---------------------------------------------------------------- discovery


def test_discovery_finds_only_real_stacks(stacks_dir: Path) -> None:
    found = discovery.discover(stacks_dir)
    assert sorted(found) == ["BookStack", "broken", "pihole", "pyobj", "renamed"]
    assert found["pihole"].services == ("pihole", "unbound")
    assert found["BookStack"].compose_file == "docker-compose.yml"
    assert found["BookStack"].project == "bookstack"
    assert found["renamed"].project == "custom_name"


def test_symlinks_are_never_followed(stacks_dir: Path) -> None:
    found = discovery.discover(stacks_dir)
    assert "evil" not in found  # compose.yaml -> /etc/passwd
    assert "linked" not in found  # folder pointing outside the stacks dir


def test_bad_compose_files_report_errors_without_content(stacks_dir: Path) -> None:
    found = discovery.discover(stacks_dir)
    assert found["broken"].error == "Compose file is not valid YAML"
    assert found["broken"].services == ()
    # safe_load refuses Python tags: the os.system call is never executed.
    assert found["pyobj"].error == "Compose file is not valid YAML"


def test_huge_compose_file_is_not_read(tmp_path: Path) -> None:
    _write(tmp_path / "big" / "compose.yaml", "#" * (1024 * 1024 + 1))
    assert discovery.discover(tmp_path)["big"].error == "Compose file is larger than 1 MiB"


def test_missing_stacks_dir_is_empty(tmp_path: Path) -> None:
    assert discovery.discover(tmp_path / "nope") == {}


# ---------------------------------------------------------------- stack API


def test_stacks_require_login(client: TestClient, fake) -> None:
    assert client.get("/api/stacks").status_code == 401
    assert client.get("/api/stacks/pihole").status_code == 401


def test_stack_list(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    data = client.get("/api/stacks").json()
    by_name = {s["name"]: s for s in data["stacks"]}
    assert data["warning"] is None
    assert by_name["pihole"]["status"] == "running"
    assert (by_name["pihole"]["running"], by_name["pihole"]["total"]) == (2, 2)
    assert by_name["BookStack"]["status"] == "stopped"
    assert by_name["renamed"]["status"] == "not_deployed"
    assert by_name["broken"]["error"] == "Compose file is not valid YAML"


def test_containers_from_another_folder_are_not_claimed(
    client: TestClient, csrf: str, fake: FakeGateway
) -> None:
    """A same-named project started elsewhere is a different stack, and gets a hint."""
    detail = client.get("/api/stacks/pihole").json()
    assert sorted(c["id"] for c in detail["containers"]) == [PIHOLE_ID, UNBOUND_ID]
    assert client.get("/api/stacks").json()["started_elsewhere"] == ["pihole"]


def test_partial_stack(client: TestClient, csrf: str, fake: FakeGateway, stacks_dir) -> None:
    fake.containers[1] = container_attrs(
        UNBOUND_ID, "unbound", "pihole", str(stacks_dir / "pihole"), "exited", "Exited (0)"
    )
    pihole = client.get("/api/stacks").json()["stacks"]
    assert next(s for s in pihole if s["name"] == "pihole")["status"] == "partial"


def test_stack_detail(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    detail = client.get("/api/stacks/pihole").json()
    assert detail["missing_services"] == []
    container = detail["containers"][0]
    assert container["ports"] == ["0.0.0.0:8080->80/tcp", "443/tcp"]
    assert container["health"] == "healthy"
    assert container["restart_count"] == 2
    assert container["mounts"] == [
        {"type": "bind", "source": "/srv/data", "destination": "/data", "read_only": False}
    ]


def test_no_secret_field_ever_reaches_the_browser(
    client: TestClient, csrf: str, fake: FakeGateway
) -> None:
    """Env, labels, command, entrypoint, args, healthcheck and its output: all stay out."""
    responses = [client.get("/api/stacks"), client.get("/api/stacks/pihole")]
    for response in responses:
        assert response.status_code == 200
        for secret in SECRETS:
            assert secret not in response.text, f"{secret} leaked"


def test_unknown_stack_names(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    for name in ["nope", "notastack", ".hidden", "evil", "linked", "..", "..%2f..%2fetc"]:
        assert client.get(f"/api/stacks/{name}").status_code == 404, name


def test_docker_down_gives_a_clear_503(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    fake.available = False
    response = client.get("/api/stacks")
    assert response.status_code == 503
    assert response.json() == {"detail": "Docker is not reachable"}


def test_missing_stacks_dir_gives_a_warning(
    client: TestClient, csrf: str, settings: Settings
) -> None:
    client.app.state.docker = FakeGateway()
    data = client.get("/api/stacks").json()
    assert data["stacks"] == []
    assert data["warning"] == "The stacks folder does not exist or is not mounted"


def test_own_stack_is_flagged(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    fake.self_id = PIHOLE_ID
    stacks = {s["name"]: s for s in client.get("/api/stacks").json()["stacks"]}
    assert stacks["pihole"]["self"] is True
    assert stacks["BookStack"]["self"] is False


# ---------------------------------------------------------------- idle timeout vs polling


def test_background_polling_does_not_keep_a_session_alive(
    client: TestClient, csrf: str, fake: FakeGateway, fake_clock: FakeClock
) -> None:
    for _ in range(3):  # the dashboard refreshing on its own for 27 minutes
        fake_clock.advance(minutes=9)
        assert client.get("/api/stacks", headers={"X-Background-Request": "1"}).status_code == 200
    fake_clock.advance(minutes=4)  # 31 minutes without real activity
    assert client.get("/api/stacks").status_code == 401


def test_real_activity_keeps_a_session_alive(
    client: TestClient, csrf: str, fake: FakeGateway, fake_clock: FakeClock
) -> None:
    for _ in range(4):
        fake_clock.advance(minutes=10)
        assert client.get("/api/stacks").status_code == 200


# ---------------------------------------------------------------- log cleaning


def test_clean_removes_terminal_escapes_and_control_characters() -> None:
    raw = (
        "\x1b[1;32mok\x1b[0m \x1b]0;title\x07\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\ a\x00b\x08c\r"
    )
    assert logs.clean(raw) == "ok link abc"
    assert logs.clean("tab\tkept") == "tab\tkept"


def test_clean_truncates_huge_lines() -> None:
    assert logs.clean("x" * 20_000).endswith("…[truncated]")
    assert len(logs.clean("x" * 20_000)) < logs.MAX_LINE + 20


def test_split_lines_handles_chunks_and_sorts_timestamps() -> None:
    pending = bytearray()
    assert logs.split_lines(b"2026-10-23T06:00:00.5Z hel", "s", pending) == []
    lines = logs.split_lines(b"lo\n2026-10-23T06:00:00.25Z next\n", "s", pending)
    assert [line.text for line in lines] == ["hello", "next"]
    # .25 is earlier than .5, also as a string once padded.
    assert sorted(lines, key=lambda line: line.ts)[0].text == "next"


def test_lines_without_timestamp_are_ignored() -> None:
    assert logs.split_lines(b"garbage line\n", "s", bytearray()) == []


# ---------------------------------------------------------------- log WebSocket


def test_log_history_is_merged_and_cleaned(client: TestClient, csrf: str, fake) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?tail=10") as ws:
        history = ws.receive_json()
        assert history["type"] == "lines"
        assert [(line["s"], line["m"]) for line in history["lines"]] == [
            ("pihole", "pihole first"),
            ("unbound", "unbound second red"),
            ("pihole", "pihole third"),
        ]
        assert ws.receive_json() == {"type": "live"}


def test_live_lines_follow(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?tail=0") as ws:
        assert ws.receive_json() == {"type": "live"}
        _until(lambda: PIHOLE_ID in fake.streams)
        stream = fake.streams[PIHOLE_ID][0]
        stream.feed(b"2026-10-23T06:00:03Z live <script>alert(1)</script>\n")
        stream.feed(b"2026-10-23T06:00:04Z split ")
        stream.feed(b"line\n")
        received = []
        while len(received) < 2:
            received += ws.receive_json()["lines"]
        # Sent as data, untouched: the browser shows it as text, never as HTML.
        assert [line["m"] for line in received] == ["live <script>alert(1)</script>", "split line"]


def test_closing_the_socket_stops_the_streams(
    client: TestClient, csrf: str, fake: FakeGateway
) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?tail=0") as ws:
        ws.receive_json()
        _until(lambda: len(fake.streams) == 2)
    _until(lambda: client.app.state.log_sockets == {})
    assert all(s.closed for streams in fake.streams.values() for s in streams)
    # No reader thread is left behind.
    _until(lambda: not [t for t in threading.enumerate() if t.name.startswith("logs-")])


def test_container_filter(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?container=unbound") as ws:
        lines = ws.receive_json()["lines"]
        assert {line["s"] for line in lines} == {"unbound"}


def test_containers_of_other_stacks_cannot_be_selected(
    client: TestClient, csrf: str, fake: FakeGateway
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as e,
        _ws(client, f"/ws/stacks/pihole/logs?container={BOOK_ID}"),
    ):
        pass
    assert e.value.code == 4404


def test_unknown_stack_socket(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    with pytest.raises(WebSocketDisconnect) as e, _ws(client, "/ws/stacks/nope/logs"):
        pass
    assert e.value.code == 4404


def test_tail_is_limited(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    with pytest.raises(WebSocketDisconnect) as e, _ws(client, "/ws/stacks/pihole/logs?tail=100000"):
        pass
    assert e.value.code == 1008


def test_too_many_open_log_views(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    sockets = [_ws(client, "/ws/stacks/pihole/logs?tail=0") for _ in range(4)]
    opened = [s.__enter__() for s in sockets]
    try:
        for ws in opened:
            ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as e, _ws(client, "/ws/stacks/pihole/logs"):
            pass
        assert e.value.code == 1013
    finally:
        for s in sockets:
            s.__exit__(None, None, None)
    _until(lambda: client.app.state.log_sockets == {})


def test_expired_session_closes_the_stream(
    client: TestClient, csrf: str, fake: FakeGateway, fake_clock: FakeClock
) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?tail=0") as ws:
        assert ws.receive_json() == {"type": "live"}
        fake_clock.advance(hours=4)
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                ws.receive_json()
        assert e.value.code == 4401


def test_logout_closes_the_stream(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?tail=0") as ws:
        assert ws.receive_json() == {"type": "live"}
        assert client.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                ws.receive_json()
        assert e.value.code == 4401


def test_watching_logs_does_not_keep_a_session_alive(
    client: TestClient, csrf: str, fake: FakeGateway, fake_clock: FakeClock
) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?tail=0") as ws:
        assert ws.receive_json() == {"type": "live"}
        fake_clock.advance(minutes=31)
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                ws.receive_json()
        assert e.value.code == 4401


def test_log_payload_has_no_extra_fields(client: TestClient, csrf: str, fake) -> None:
    with _ws(client, "/ws/stacks/pihole/logs?tail=10") as ws:
        lines = ws.receive_json()["lines"]
        assert all(set(line) == {"t", "s", "m"} for line in lines)
        assert "secret" not in json.dumps(lines)


# ---------------------------------------------------------------- Docker error messages


def test_socket_permission_problems_are_named() -> None:
    import docker.errors
    import requests.exceptions

    from app.stacks.gateway import _unavailable

    def chained() -> Exception:
        inner = requests.exceptions.ConnectionError(
            ("Connection aborted.", PermissionError(13, "Permission denied"))
        )
        try:
            raise docker.errors.DockerException(
                "Error while fetching server API version"
            ) from inner
        except docker.errors.DockerException as e:
            return e

    assert (
        str(_unavailable(chained())) == "No permission to use the Docker socket: check DOCKER_GID"
    )
    missing = docker.errors.DockerException("Error while fetching server API version: no such file")
    assert str(_unavailable(missing)) == "Docker is not reachable"


def test_history_is_capped_across_containers(
    client: TestClient, csrf: str, fake: FakeGateway
) -> None:
    many = b"".join(
        b"2026-10-23T06:%02d:%02dZ line %d\n" % (i // 60, i % 60, i) for i in range(1500)
    )
    fake.logs[PIHOLE_ID] = many
    fake.logs[UNBOUND_ID] = many
    with _ws(client, "/ws/stacks/pihole/logs?tail=1000") as ws:
        received = []
        while True:
            message = ws.receive_json()
            if message["type"] == "live":
                break
            received += message["lines"]
    assert len(received) == logs.MAX_HISTORY
    assert received[-1]["m"] == "line 1499"  # the newest lines are kept
