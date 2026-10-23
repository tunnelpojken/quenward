"""Read-only stack views: the stack list, one stack's containers, and live logs."""

import asyncio
import contextlib
import logging
import sqlite3
from typing import Any

import docker.errors
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from app.auth import sessions
from app.auth.deps import CurrentUser, get_db, require_user, require_user_ws
from app.stacks import service
from app.stacks.gateway import DockerUnavailable
from app.stacks.logs import LogFollower, Target, now

log = logging.getLogger("app.stacks")

# require_user runs for every route on this router.
router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])
ws_router = APIRouter()

MAX_SOCKETS_PER_SESSION = 4
MAX_SOCKETS_TOTAL = 16
_BATCH_LINES = 500
_BATCH_INTERVAL = 0.1  # seconds


def _unavailable(e: DockerUnavailable) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))


@router.get("/stacks")
def list_stacks(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    if not settings.stacks_dir.is_dir():
        return {
            "stacks": [],
            "warning": "The stacks folder does not exist or is not mounted",
            "started_elsewhere": [],
        }
    try:
        result = service.overview(settings, request.app.state.docker)
    except DockerUnavailable as e:
        raise _unavailable(e) from None
    return {
        "stacks": [s.public() for s in result.stacks],
        "warning": None,
        "started_elsewhere": result.started_elsewhere,
    }


@router.get("/stacks/{name}")
def get_stack(name: str, request: Request) -> dict[str, Any]:
    gateway = request.app.state.docker
    try:
        state = service.get_stack(request.app.state.settings, gateway, name)
        containers = []
        for summary in state.containers:
            with contextlib.suppress(docker.errors.NotFound):  # removed meanwhile
                containers.append(gateway.inspect(summary.id))
    except service.StackNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such stack") from None
    except DockerUnavailable as e:
        raise _unavailable(e) from None
    with_container = {c["service"] for c in containers}
    return {
        **state.public(),
        "containers": containers,
        "missing_services": [s for s in state.stack.services if s not in with_container],
    }


def _socket_slot(websocket: WebSocket, session_id: int) -> bool:
    """Reserve a log socket; refuse when this session or the app has too many open."""
    counts: dict[int, int] = websocket.app.state.log_sockets
    if sum(counts.values()) >= MAX_SOCKETS_TOTAL:
        return False
    if counts.get(session_id, 0) >= MAX_SOCKETS_PER_SESSION:
        return False
    counts[session_id] = counts.get(session_id, 0) + 1
    return True


def _release_slot(websocket: WebSocket, session_id: int) -> None:
    counts: dict[int, int] = websocket.app.state.log_sockets
    counts[session_id] -= 1
    if counts[session_id] <= 0:
        del counts[session_id]


@ws_router.websocket("/ws/stacks/{name}/logs")
async def stack_logs(
    websocket: WebSocket,
    name: str,
    user: CurrentUser = Depends(require_user_ws),
    conn: sqlite3.Connection = Depends(get_db),
    tail: int = Query(200, ge=0, le=1000),
    container: str | None = Query(None, max_length=128),
) -> None:
    app = websocket.app
    settings = app.state.settings
    gateway = app.state.docker
    token = websocket.cookies.get(sessions.COOKIE_NAME)

    try:
        state = await run_in_threadpool(service.get_stack, settings, gateway, name)
    except service.StackNotFound:
        await websocket.close(code=4404, reason="No such stack")
        return
    except DockerUnavailable as e:
        await websocket.close(code=1011, reason=str(e))
        return

    # Only containers that belong to this stack can be selected, by id or service name.
    targets = [
        Target(c.id, c.service or c.name)
        for c in state.containers
        if container is None or container in (c.id, c.service, c.name)
    ]
    if container is not None and not targets:
        await websocket.close(code=4404, reason="No such container in this stack")
        return

    if not _socket_slot(websocket, user.session.id):
        await websocket.close(code=1013, reason="Too many open log views")
        return
    follower = LogFollower(gateway, targets, asyncio.get_running_loop())
    try:
        await websocket.accept()
        boundary = now()
        try:
            history = await run_in_threadpool(follower.history, tail, boundary)
        except DockerUnavailable as e:
            await websocket.close(code=1011, reason=str(e))
            return
        for i in range(0, len(history), _BATCH_LINES):
            batch = history[i : i + _BATCH_LINES]
            await websocket.send_json({"type": "lines", "lines": [x.public() for x in batch]})
        await websocket.send_json({"type": "live"})
        follower.start(since=boundary)
        await _pump(websocket, follower, conn, token, settings)
    except WebSocketDisconnect:
        pass
    finally:
        follower.stop()
        _release_slot(websocket, user.session.id)


async def _pump(
    websocket: WebSocket,
    follower: LogFollower,
    conn: sqlite3.Connection,
    token: str | None,
    settings: Any,
) -> None:
    """Forward new lines in small batches; re-check the session regularly."""
    loop = asyncio.get_running_loop()
    next_check = loop.time() + settings.ws_session_check.total_seconds()
    reported_drops = 0
    receiver = asyncio.create_task(websocket.receive_text())  # notices a closed socket
    try:
        while True:
            if receiver.done():
                return
            batch = []
            try:
                batch.append(await asyncio.wait_for(follower.queue.get(), _BATCH_INTERVAL))
                while len(batch) < _BATCH_LINES and not follower.queue.empty():
                    batch.append(follower.queue.get_nowait())
            except TimeoutError:
                pass
            if batch:
                await websocket.send_json({"type": "lines", "lines": [x.public() for x in batch]})
            if follower.dropped != reported_drops:
                await websocket.send_json({"type": "dropped", "count": follower.dropped})
                reported_drops = follower.dropped
            if loop.time() >= next_check:
                next_check = loop.time() + settings.ws_session_check.total_seconds()
                # A watched log is not user activity: touch=False.
                if sessions.validate(conn, token, settings, touch=False) is None:
                    await websocket.close(code=4401, reason="Session expired")
                    return
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await receiver
