"""Phase 5: the interactive terminal and its encrypted transcripts.

FakeGateway's shell sits on the far end of a real socket pair, so the bridge code
(probe, reader thread, backpressure, ending the shell) runs for real.
tests/test_docker_integration.py repeats the important parts against real Docker.
"""

import asyncio
import base64
import contextlib
import dataclasses
import json
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import db
from app.auth import sessions
from app.config import ConfigError, Settings
from app.main import create_app
from app.terminal import bridge as bridge_module
from app.terminal import transcripts
from tests.conftest import ORIGIN, FakeClock
from tests.fake_docker import FakeGateway, container_attrs

PIHOLE_ID = "aaaaaaaaaaaa"
UNBOUND_ID = "bbbbbbbbbbbb"
STOPPED_ID = "cccccccccccc"
KEY = base64.b64encode(bytes(range(32))).decode()
SECRET = "hunter2-typed-secret"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return dataclasses.replace(settings, ws_session_check=timedelta(milliseconds=50))


@pytest.fixture
def fake(client: TestClient, settings: Settings) -> FakeGateway:
    stack = settings.stacks_dir / "pihole"
    stack.mkdir(parents=True)
    (stack / "compose.yaml").write_text("services:\n  pihole: {}\n  unbound: {}\n  old: {}\n")
    gateway = FakeGateway(
        containers=[
            container_attrs(PIHOLE_ID, "pihole", "pihole", str(stack)),
            container_attrs(UNBOUND_ID, "unbound", "pihole", str(stack)),
            container_attrs(STOPPED_ID, "old", "pihole", str(stack), "exited", "Exited (0)"),
        ]
    )
    client.app.state.docker = gateway
    return gateway


@pytest.fixture
def recording(client: TestClient) -> bytes:
    """Turn transcript recording on, as if TRANSCRIPT_KEY_FILE were set."""
    key = transcripts.load_key(_key_file(client.app.state.settings.data_dir.parent, KEY))
    client.app.state.transcript_key = key
    return key


def _key_file(directory: Path, text: str) -> Path:
    path = directory / "transcript.key"
    if path.exists():
        path.chmod(0o600)  # made read-only by an earlier call; only root could rewrite it
    path.write_text(text + "\n")
    path.chmod(0o400)
    return path


def _url(container: str = PIHOLE_ID, stack: str = "pihole", **params: Any) -> str:
    query = "&".join(f"{k}={v}" for k, v in {"container": container, **params}.items())
    return f"wss://testserver/ws/stacks/{stack}/terminal?{query}"


class Term:
    """A small client for the terminal protocol."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.output = b""
        self.control: list[dict[str, Any]] = []
        self.closed: int | None = None

    def type(self, text: str) -> None:
        self.ws.send_json({"type": "input", "data": text})

    def read_until(self, needle: bytes, limit: int = 200) -> bytes:
        for _ in range(limit):
            if needle in self.output:
                return self.output
            self._receive()
            if self.closed is not None:
                break
        raise AssertionError(f"{needle!r} never arrived; got {self.output[-200:]!r}")

    def wait_closed(self, limit: int = 200) -> int:
        for _ in range(limit):
            self._receive()
            if self.closed is not None:
                return self.closed
        raise AssertionError("the terminal did not close")

    def _receive(self) -> None:
        message = self.ws.receive()
        if message["type"] == "websocket.close":
            self.closed = message["code"]
        elif message.get("bytes") is not None:
            self.output += message["bytes"]
        elif message.get("text") is not None:
            self.control.append(json.loads(message["text"]))


@pytest.fixture
def term(client: TestClient, csrf: str, fake: FakeGateway) -> Iterator[Any]:
    sockets = []

    def open_(url: str | None = None) -> Term:
        cm = client.websocket_connect(url or _url(), headers={"Origin": ORIGIN})
        t = Term(cm.__enter__())
        sockets.append(cm)
        return t

    yield open_
    for cm in reversed(sockets):
        with contextlib.suppress(Exception):  # already closed by the server
            cm.__exit__(None, None, None)


def _eventually(check: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _close_code(client: TestClient, url: str) -> int | None:
    """The code the server closed with, whether before or after accepting."""
    try:
        with client.websocket_connect(url, headers={"Origin": ORIGIN}) as ws:
            return Term(ws).wait_closed()
    except WebSocketDisconnect as e:
        return e.code


def _audit(settings: Settings, action: str) -> list[dict[str, Any]]:
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------- the terminal


def test_typing_runs_in_the_shell_and_closing_ends_it(term: Any, fake: FakeGateway) -> None:
    t = term()
    t.read_until(b"$ ")
    assert t.control[0] == {"type": "ready", "shell": "sh", "recorded": False, "idle_minutes": 15}
    assert b"\x1e" not in t.output, "the pid marker must never reach the browser"
    t.type("echo ok\n")
    assert b"\r\nok\r\n" in t.read_until(b"ok\r\n$ ")
    t.ws.close()
    # Closing a Docker exec's connection does not stop the shell: the app must.
    _eventually(lambda: (PIHOLE_ID, 40) in fake.ended_sessions)


def test_shell_exit_is_reported(term: Any) -> None:
    t = term()
    t.read_until(b"$ ")
    t.type("exit 3\n")
    assert t.wait_closed() == 4410
    assert {"type": "exit", "code": 3} in t.control


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (_url(container="ffffffffffff"), 4404),  # not a container of this stack
        (_url(container=""), 4404),
        (_url(stack="nope"), 4404),
        (_url(container=STOPPED_ID), 4409),  # exists, but not running
        (_url(container="dddddddddddd"), 4404),
    ],
)
def test_only_running_containers_of_the_stack(
    client: TestClient, csrf: str, fake: FakeGateway, url: str, code: int
) -> None:
    assert _close_code(client, url) == code
    assert fake.shells == [], "no exec may be created for a refused terminal"


def test_container_can_be_named(term: Any) -> None:
    t = term(_url(container="pihole-unbound-1"))
    t.read_until(b"$ ")


def test_the_apps_own_stack_is_refused(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    fake.self_id = PIHOLE_ID
    assert _close_code(client, _url()) == 4403
    assert fake.shells == []


def test_container_without_a_shell(client: TestClient, csrf: str, fake: FakeGateway) -> None:
    fake.has_shell = False
    assert _close_code(client, _url()) == 1011


def test_resize_is_passed_on_and_clamped(term: Any, fake: FakeGateway) -> None:
    t = term(_url(rows=30, cols=100))
    t.read_until(b"$ ")
    t.ws.send_json({"type": "resize", "rows": 40, "cols": 120})
    t.ws.send_json({"type": "resize", "rows": 99999, "cols": -5})
    t.ws.send_json({"type": "resize", "rows": "x"})
    t.type("echo sync\n")
    t.read_until(b"sync\r\n$ ")
    assert fake.resizes == [
        ("exec0", 30, 100),
        ("exec0", 40, 120),
        ("exec0", 500, 10),
        ("exec0", 24, 80),
    ]


def test_bad_messages_are_ignored(term: Any) -> None:
    t = term()
    t.read_until(b"$ ")
    t.ws.send_text("not json")
    t.ws.send_text("[1, 2]")
    t.ws.send_json({"type": "input", "data": 5})
    t.ws.send_bytes(b"echo binary\n")
    t.type("echo still-alive\n")
    t.read_until(b"still-alive\r\n$ ")
    assert b"binary" not in t.output


def test_huge_input_is_refused(term: Any) -> None:
    t = term()
    t.read_until(b"$ ")
    t.type("x" * (64 * 1024 + 1))
    assert t.wait_closed() == 1009


def test_at_most_two_terminals_per_session(client: TestClient, term: Any) -> None:
    term().read_until(b"$ ")
    term(_url(container=UNBOUND_ID)).read_until(b"$ ")
    assert _close_code(client, _url()) == 1013


def test_idle_terminal_is_closed(client: TestClient, term: Any, fake: FakeGateway) -> None:
    client.app.state.settings = dataclasses.replace(
        client.app.state.settings, terminal_idle=timedelta(milliseconds=300)
    )
    t = term()
    t.read_until(b"$ ")
    assert t.wait_closed() == 4408
    _eventually(lambda: (PIHOLE_ID, 40) in fake.ended_sessions)


def test_logging_out_elsewhere_closes_the_terminal(
    client: TestClient, settings: Settings, term: Any, fake: FakeGateway
) -> None:
    t = term()
    t.read_until(b"$ ")
    conn = db.connect(settings.db_path)
    sessions.delete_all_for_user(conn, 1)
    conn.close()
    assert t.wait_closed() == 4401
    _eventually(lambda: (PIHOLE_ID, 40) in fake.ended_sessions)


def test_typing_keeps_the_session_alive_but_an_open_terminal_does_not(
    client: TestClient, settings: Settings, term: Any, fake_clock: FakeClock
) -> None:
    t = term()
    t.read_until(b"$ ")
    fake_clock.advance(minutes=20)
    t.type("echo typing\n")
    t.read_until(b"typing\r\n$ ")
    time.sleep(0.2)  # a session check sees the typing
    fake_clock.advance(minutes=20)  # 40 min since login, 20 since typing: still valid
    t.type("echo again\n")
    t.read_until(b"again\r\n$ ")
    fake_clock.advance(minutes=31)  # nothing typed for longer than the idle timeout
    assert t.wait_closed() == 4401


def test_open_and_close_are_audited_without_what_was_typed(term: Any, settings: Settings) -> None:
    t = term()
    t.read_until(b"$ ")
    t.type(f"echo {SECRET}\n")
    t.read_until(SECRET.encode() + b"\r\n$ ")
    t.type("exit 0\n")
    t.wait_closed()
    _eventually(lambda: len(_audit(settings, "terminal_close")) == 1)
    opened = _audit(settings, "terminal_open")
    closed = _audit(settings, "terminal_close")
    assert opened[0]["target_stack"] == "pihole"
    assert opened[0]["target_container"] == "pihole-pihole-1"
    assert json.loads(opened[0]["detail_json"])["recorded"] is False
    assert json.loads(closed[0]["detail_json"])["reason"] == "exited"
    everything = json.dumps(_audit(settings, "terminal_open") + closed)
    assert SECRET not in everything


def test_backpressure_pauses_reading_instead_of_buffering(
    fake: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "MAX_PENDING", 64 * 1024)

    async def scenario() -> int:
        b = bridge_module.Bridge(fake, PIHOLE_ID, asyncio.get_running_loop())
        await asyncio.to_thread(b.start, 24, 80)
        await asyncio.to_thread(b.write, b"flood 4000000\n")
        await asyncio.sleep(0.5)  # nobody consumes: the reader must stop
        pending = b._pending
        while not b.output.empty():  # now the browser catches up
            data = b.output.get_nowait()
            if data:
                b.consumed(len(data))
        await asyncio.to_thread(b.close)
        return pending

    pending = asyncio.run(scenario())
    assert pending <= 64 * 1024 + bridge_module._READ_SIZE


# ---------------------------------------------------------------- transcripts


def _raw_database(settings: Settings) -> bytes:
    data = b""
    for path in settings.data_dir.iterdir():
        data += path.read_bytes()
    return data


def _record_a_session(term: Any) -> None:
    t = term()
    t.read_until(b"$ ")
    t.type(f"echo {SECRET}\n")
    t.read_until(SECRET.encode() + b"\r\n$ ")
    t.type("exit 0\n")
    t.wait_closed()


def test_transcripts_are_encrypted_and_readable_with_the_key(
    client: TestClient, settings: Settings, term: Any, recording: bytes
) -> None:
    _record_a_session(term)
    _eventually(lambda: len(_audit(settings, "terminal_close")) == 1)
    assert SECRET.encode() not in _raw_database(settings)

    listing = client.get("/api/terminal-sessions").json()
    assert listing["recording"] is True
    assert listing["retention_days"] == 14
    session = listing["sessions"][0]
    assert session["recorded"] is True
    assert session["stack"] == "pihole"
    assert session["end_reason"] == "exited"
    assert session["exit_code"] == 0

    response = client.get(f"/api/terminal-sessions/{session['id']}/transcript")
    assert response.status_code == 200
    events = response.json()["events"]
    typed = b"".join(base64.b64decode(e[2]) for e in events if e[1] == "i")
    printed = b"".join(base64.b64decode(e[2]) for e in events if e[1] == "o")
    assert typed == f"echo {SECRET}\nexit 0\n".encode()
    assert SECRET.encode() + b"\r\n$ " in printed
    assert all(isinstance(e[0], int) for e in events)
    assert _audit(settings, "transcript_view")[0]["result"] == "ok"


def test_without_a_key_nothing_is_recorded(
    client: TestClient, settings: Settings, term: Any
) -> None:
    _record_a_session(term)
    _eventually(lambda: len(_audit(settings, "terminal_close")) == 1)
    listing = client.get("/api/terminal-sessions").json()
    assert listing["recording"] is False
    session = listing["sessions"][0]
    assert session["recorded"] is False
    assert client.get(f"/api/terminal-sessions/{session['id']}/transcript").status_code == 409
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transcript_chunks").fetchone()[0] == 0


def test_another_key_cannot_read_a_transcript(
    client: TestClient, settings: Settings, term: Any, recording: bytes
) -> None:
    _record_a_session(term)
    _eventually(lambda: len(_audit(settings, "terminal_close")) == 1)
    session = client.get("/api/terminal-sessions").json()["sessions"][0]
    client.app.state.transcript_key = bytes(32)
    response = client.get(f"/api/terminal-sessions/{session['id']}/transcript")
    assert response.status_code == 422
    assert "cannot be decrypted" in response.json()["detail"]
    client.app.state.transcript_key = None
    response = client.get(f"/api/terminal-sessions/{session['id']}/transcript")
    assert response.status_code == 422
    assert _audit(settings, "transcript_view")[0]["result"] == "error"


def test_transcript_ids_are_checked(client: TestClient, csrf: str) -> None:
    assert client.get("/api/terminal-sessions/0123456789abcdef/transcript").status_code == 404
    assert client.get("/api/terminal-sessions/../transcript").status_code == 404
    assert client.get("/api/terminal-sessions/XYZ/transcript").status_code == 422


def test_long_sessions_are_truncated(
    client: TestClient, settings: Settings, term: Any, recording: bytes, monkeypatch: Any
) -> None:
    monkeypatch.setattr(transcripts, "MAX_TRANSCRIPT", 2000)
    t = term()
    t.read_until(b"$ ")
    t.type("flood 5000\n")
    t.read_until(b"x\r\n$ ")
    t.type("exit 0\n")
    t.wait_closed()
    _eventually(lambda: len(_audit(settings, "terminal_close")) == 1)
    session = client.get("/api/terminal-sessions").json()["sessions"][0]
    assert session["truncated"] is True
    assert session["transcript_bytes"] == 2000


def test_pieces_cannot_be_moved_between_sessions_or_positions() -> None:
    key = bytes(range(32))
    piece = transcripts.encrypt_chunk(key, "aaaa", 0, b"[]")
    assert transcripts.decrypt_chunk(key, "aaaa", 0, piece) == b"[]"
    for public_id, seq in (("bbbb", 0), ("aaaa", 1)):
        with pytest.raises(transcripts.TranscriptUnreadable):
            transcripts.decrypt_chunk(key, public_id, seq, piece)
    tampered = piece[:-1] + bytes([piece[-1] ^ 1])
    with pytest.raises(transcripts.TranscriptUnreadable):
        transcripts.decrypt_chunk(key, "aaaa", 0, tampered)


def test_old_sessions_are_deleted_after_the_retention_time(
    client: TestClient, settings: Settings, term: Any, recording: bytes, fake_clock: FakeClock
) -> None:
    _record_a_session(term)
    _eventually(lambda: len(_audit(settings, "terminal_close")) == 1)
    conn = db.connect(settings.db_path)
    fake_clock.advance(days=13)
    assert transcripts.purge(conn, settings.transcript_retention) == 0
    fake_clock.advance(days=2)
    assert transcripts.purge(conn, settings.transcript_retention) == 1
    assert conn.execute("SELECT COUNT(*) FROM transcript_chunks").fetchone()[0] == 0
    assert len(_audit(settings, "terminal_open")) == 1, "the audit log keeps its entries"


def test_shells_left_open_by_a_crash_are_ended_at_startup(
    client: TestClient, settings: Settings, fake: FakeGateway
) -> None:
    still_running = fake.open_shell(PIHOLE_ID)
    conn = db.connect(settings.db_path)
    common = {"user_id": None, "stack": "pihole", "service": "pihole", "shell_name": "sh"}
    for exec_id, pid in ((still_running.exec_id, 40), ("gone-exec", 77)):
        transcripts.create_session(
            conn,
            **common,
            container_id=PIHOLE_ID,
            container_name="pihole-pihole-1",
            exec_id=exec_id,
            shell_pid=pid,
            recorded=False,
        )
    assert transcripts.end_leftovers(conn, fake) == 1
    assert fake.ended_sessions == [(PIHOLE_ID, 40)], "an exec Docker no longer knows is left alone"
    assert transcripts.unfinished_sessions(conn) == []


# ---------------------------------------------------------------- the key file


def test_key_file_is_validated(tmp_path: Path) -> None:
    assert transcripts.load_key(_key_file(tmp_path, KEY)) == bytes(range(32))
    urlsafe = base64.urlsafe_b64encode(bytes(range(200, 232))).decode()
    assert len(transcripts.load_key(_key_file(tmp_path, urlsafe))) == 32
    for bad in ("not base64 !!", base64.b64encode(b"short").decode(), ""):
        with pytest.raises(transcripts.TranscriptKeyError):
            transcripts.load_key(_key_file(tmp_path, bad))


def test_readable_key_file_gives_a_warning(tmp_path: Path, caplog: Any) -> None:
    path = _key_file(tmp_path, KEY)
    path.chmod(0o644)
    transcripts.load_key(path)
    assert "chmod 400" in caplog.text


def test_key_file_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIPT_KEY_FILE", str(tmp_path / "missing.key"))
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("TRANSCRIPT_KEY_FILE", "relative.key")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("TRANSCRIPT_KEY_FILE", str(_key_file(tmp_path, KEY)))
    assert Settings.from_env().transcript_key_file == tmp_path / "transcript.key"


def test_a_bad_key_stops_the_app(settings: Settings, tmp_path: Path) -> None:
    broken = dataclasses.replace(
        settings, transcript_key_file=_key_file(tmp_path, base64.b64encode(b"x").decode())
    )
    with pytest.raises(transcripts.TranscriptKeyError):
        create_app(broken)


def test_reader_thread_ends_with_the_shell(fake: FakeGateway) -> None:
    """No thread may outlive its terminal."""

    async def scenario() -> None:
        b = bridge_module.Bridge(fake, PIHOLE_ID, asyncio.get_running_loop())
        await asyncio.to_thread(b.start, 24, 80)
        await asyncio.to_thread(b.close)
        assert b._reader is not None
        await asyncio.to_thread(b._reader.join, 2)
        assert not b._reader.is_alive()

    before = threading.active_count()
    asyncio.run(scenario())
    assert threading.active_count() <= before + 1  # the fake shell's own thread may linger
