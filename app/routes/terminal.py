"""The interactive terminal (WebSocket) and the terminal history.

A terminal is a shell inside a container (docker exec), never on the host. It is
the most powerful thing this app offers, so on top of the usual WebSocket checks
(session and Origin at the handshake, re-checked every few seconds):

- only running containers of a known stack, never the app's own stack;
- at most 2 terminals per session and 4 in total;
- closed after 15 minutes without a keystroke;
- the shell and everything started from it are ended when the terminal closes;
- opening and closing are audited; what is typed is recorded only in the
  encrypted transcript, never in the audit log or the app's own log.

Protocol. Browser to server: JSON text frames {"type": "input", "data": "..."}
and {"type": "resize", "rows": R, "cols": C}. Server to browser: binary frames
with terminal output, JSON text frames {"type": "ready", ...} and
{"type": "exit", "code": N}.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Path, Request, WebSocket, status
from fastapi.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from app import audit
from app.auth import sessions
from app.auth.deps import CurrentUser, client_ip, get_db, require_user, require_user_ws
from app.stacks import service
from app.stacks.gateway import DockerUnavailable
from app.terminal import transcripts
from app.terminal.bridge import Bridge, ShellUnavailable

log = logging.getLogger("app.terminal")

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])
ws_router = APIRouter()

MAX_PER_SESSION = 2
MAX_TOTAL = 4
MAX_INPUT = 64 * 1024  # characters per input message (a large paste)
MAX_FRAME = 64 * 1024  # bytes of output per frame sent to the browser
_FLUSH_EVERY = 2.0  # seconds between transcript writes


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _slot(app: Any, session_id: int) -> bool:
    counts: dict[int, int] = app.state.terminal_sockets
    if sum(counts.values()) >= MAX_TOTAL or counts.get(session_id, 0) >= MAX_PER_SESSION:
        return False
    counts[session_id] = counts.get(session_id, 0) + 1
    return True


def _release(app: Any, session_id: int) -> None:
    counts: dict[int, int] = app.state.terminal_sockets
    counts[session_id] -= 1
    if counts[session_id] <= 0:
        del counts[session_id]


@ws_router.websocket("/ws/stacks/{name}/terminal")
async def terminal(
    websocket: WebSocket,
    name: str,
    user: CurrentUser = Depends(require_user_ws),
    conn: sqlite3.Connection = Depends(get_db),
) -> None:
    app = websocket.app
    settings = app.state.settings
    gateway = app.state.docker
    # Plain query parameters, checked by hand: a FastAPI validation error would
    # close the socket with the same code as a failed login.
    wanted = websocket.query_params.get("container", "")[:128]
    rows = _clamp(websocket.query_params.get("rows"), 2, 500, 24)
    cols = _clamp(websocket.query_params.get("cols"), 10, 1000, 80)

    try:
        state = await run_in_threadpool(service.get_stack, settings, gateway, name)
    except service.StackNotFound:
        await websocket.close(code=4404, reason="No such stack")
        return
    except DockerUnavailable as e:
        await websocket.close(code=1011, reason=str(e))
        return
    if state.is_self:
        await websocket.close(code=4403, reason="This app's own stack is read-only")
        return
    found = [c for c in state.containers if wanted and wanted in (c.id, c.name)]
    if not found:
        await websocket.close(code=4404, reason="No such container in this stack")
        return
    container = found[0]
    if container.state != "running":
        await websocket.close(code=4409, reason="The container is not running")
        return
    if not _slot(app, user.session.id):
        await websocket.close(code=1013, reason="Too many terminals open")
        return

    bridge = Bridge(gateway, container.id, asyncio.get_running_loop())
    recorder: transcripts.Recorder | None = None
    session_row: transcripts.SessionInfo | None = None
    started_at = time.monotonic()
    reason = "closed"
    exit_code: int | None = None
    try:
        await websocket.accept()
        try:
            started = await run_in_threadpool(bridge.start, rows, cols)
        except (ShellUnavailable, DockerUnavailable) as e:
            await websocket.close(code=1011, reason=str(e))
            return
        key = app.state.transcript_key
        session_row = transcripts.create_session(
            conn,
            user_id=user.id,
            stack=state.stack.name,
            service=container.service,
            container_id=container.id,
            container_name=container.name,
            exec_id=bridge.handle.exec_id if bridge.handle else "",
            shell_name=started.shell_name,
            shell_pid=started.pid,
            recorded=key is not None,
        )
        if key is not None:
            recorder = transcripts.Recorder(settings.db_path, key, session_row)
        audit.record(
            conn,
            "terminal_open",
            "ok",
            user_id=user.id,
            target_stack=state.stack.name,
            target_container=container.name,
            session=session_row.public_id,
            shell_name=started.shell_name,
            recorded=recorder is not None,
            ip=websocket.client.host if websocket.client else None,
        )
        await websocket.send_json(
            {
                "type": "ready",
                "shell": started.shell_name,
                "recorded": recorder is not None,
                "idle_minutes": int(settings.terminal_idle.total_seconds() // 60),
            }
        )
        if started.first_output:
            if recorder:
                recorder.add("o", started.first_output)
            await websocket.send_bytes(started.first_output)
        reason, exit_code = await _run(websocket, bridge, recorder, conn, settings)
    except WebSocketDisconnect:
        reason = "closed"
    finally:
        # Shielded from cancellation: when the server cancels this connection (it
        # shuts down, or drops the task right after a disconnect), the shell must
        # still be ended and the terminal slot freed. Without the shield, the first
        # await here would be cancelled and none of it would happen.
        with anyio.CancelScope(shield=True):
            try:
                code = await run_in_threadpool(bridge.close)
                exit_code = exit_code if exit_code is not None else code
                if recorder is not None:
                    try:
                        await run_in_threadpool(recorder.flush)
                    except Exception:
                        log.exception("Could not store the end of a terminal transcript")
                if session_row is not None:
                    transcripts.finish_session(conn, session_row.row_id, reason, exit_code)
                    audit.record(
                        conn,
                        "terminal_close",
                        "ok",
                        user_id=user.id,
                        target_stack=state.stack.name,
                        target_container=container.name,
                        session=session_row.public_id,
                        reason=reason,
                        exit_code=exit_code,
                        seconds=round(time.monotonic() - started_at, 1),
                        recorded_bytes=recorder.total if recorder else 0,
                    )
            finally:
                _release(app, user.session.id)


async def _run(
    websocket: WebSocket,
    bridge: Bridge,
    recorder: transcripts.Recorder | None,
    conn: sqlite3.Connection,
    settings: Any,
) -> tuple[str, int | None]:
    """Pump data both ways until one side ends. Returns (reason, exit code)."""
    loop = asyncio.get_running_loop()
    token = websocket.cookies.get(sessions.COOKIE_NAME)
    last_input = loop.time()
    typed_since_check = False

    async def flush() -> None:
        if recorder is not None:
            await run_in_threadpool(recorder.flush)

    async def pump_output() -> tuple[str, int | None]:
        while True:
            data = await bridge.output.get()
            if data is None:
                code = await run_in_threadpool(bridge.close)
                await websocket.send_json({"type": "exit", "code": code})
                await websocket.close(code=4410, reason="The shell ended")
                return "exited", code
            frame = bytearray(data)
            while len(frame) < MAX_FRAME and not bridge.output.empty():
                more = bridge.output.get_nowait()
                if more is None:
                    bridge.output.put_nowait(None)  # handled on the next round
                    break
                frame += more
            if recorder is not None and recorder.add("o", bytes(frame)):
                await flush()
            await websocket.send_bytes(bytes(frame))
            bridge.consumed(len(frame))

    async def pump_input() -> tuple[str, int | None]:
        nonlocal last_input, typed_since_check
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return "closed", None
            text = message.get("text")
            if text is None:
                continue  # binary frames from the browser are not part of the protocol
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "input" and isinstance(event.get("data"), str):
                if len(event["data"]) > MAX_INPUT:
                    await websocket.close(code=1009, reason="Input too large")
                    return "input too large", None
                data = event["data"].encode("utf-8", errors="replace")
                last_input = loop.time()
                typed_since_check = True
                if recorder is not None and recorder.add("i", data):
                    await flush()
                await run_in_threadpool(bridge.write, data)
            elif event.get("type") == "resize":
                await run_in_threadpool(
                    bridge.resize,
                    _clamp(event.get("rows"), 2, 500, 24),
                    _clamp(event.get("cols"), 10, 1000, 80),
                )

    async def watch() -> tuple[str, int | None]:
        nonlocal typed_since_check
        idle = settings.terminal_idle.total_seconds()
        check_every = settings.ws_session_check.total_seconds()
        next_check = loop.time() + check_every
        while True:
            await asyncio.sleep(min(check_every, _FLUSH_EVERY))
            await flush()
            if loop.time() >= next_check:
                next_check = loop.time() + check_every
                # Typing in the terminal is real activity, so it keeps the session
                # alive; an open but untouched terminal does not.
                if sessions.validate(conn, token, settings, touch=typed_since_check) is None:
                    await websocket.close(code=4401, reason="Session expired")
                    return "session expired", None
                typed_since_check = False
            if loop.time() - last_input >= idle:
                await websocket.close(code=4408, reason="Closed after inactivity")
                return "idle", None

    tasks = [asyncio.create_task(t()) for t in (pump_output, pump_input, watch)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is None:
                return task.result()
        for task in done:
            error = task.exception()
            if isinstance(error, WebSocketDisconnect | RuntimeError):
                return "closed", None
            if error is not None:
                raise error
        return "closed", None
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task


# ---------------------------------------------------------------- history


@router.get("/terminal-sessions")
def list_terminal_sessions(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "recording": request.app.state.transcript_key is not None,
        "retention_days": settings.transcript_retention.days,
        "sessions": transcripts.list_sessions(conn),
    }


@router.get("/terminal-sessions/{session_id}/transcript")
def get_transcript(
    request: Request,
    session_id: str = Path(pattern=r"^[0-9a-f]{16}$"),
    user: CurrentUser = Depends(require_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    found = transcripts.get_session(conn, session_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such terminal session")
    row_id, info = found
    if not info["recorded"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "This session was not recorded")
    try:
        events = transcripts.read_transcript(
            conn, request.app.state.transcript_key, row_id, session_id
        )
    except transcripts.TranscriptUnreadable as e:
        audit.record(
            conn,
            "transcript_view",
            "error",
            user_id=user.id,
            session=session_id,
            ip=client_ip(request),
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    # Reading a transcript can reveal secrets, so it is audited like an action.
    audit.record(
        conn,
        "transcript_view",
        "ok",
        user_id=user.id,
        target_stack=info["stack"],
        target_container=info["container_name"],
        session=session_id,
        ip=client_ip(request),
    )
    return {"session": info, "events": events}
