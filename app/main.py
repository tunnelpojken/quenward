"""Application entry point.

Run locally:  uv run python -m app.main
In Docker:    the image runs the same command.
"""

import asyncio
import contextlib
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import alerts, db
from app.auth import sessions
from app.auth.ratelimit import LoginLimiter
from app.config import Settings
from app.routes import auth as auth_routes
from app.routes import stacks as stack_routes
from app.routes import terminal as terminal_routes
from app.security_headers import SecurityHeadersMiddleware
from app.stacks.actions import JobManager
from app.stacks.gateway import DockerGateway
from app.terminal import transcripts

log = logging.getLogger("app")


class _HideHealthchecks(logging.Filter):
    """Docker calls /healthz every 30 s; keep those lines out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def _announce_setup_token(token: str) -> None:
    bar = "=" * 64
    log.warning(
        "\n%s\nNo admin account exists yet.\nOne-time setup token: %s\n"
        "Use it on the setup page to create the admin. It stops working after that,\n"
        "and a new one is generated on restart until setup is done.\n%s",
        bar,
        token,
        bar,
    )


MAINTENANCE_EVERY = 3600  # seconds


def _maintenance(settings: Settings) -> None:
    """Hourly: drop expired sessions and terminal history past its retention time."""
    conn = db.connect(settings.db_path)
    try:
        sessions.purge_expired(conn, settings)
        transcripts.purge(conn, settings.transcript_retention)
    finally:
        conn.close()


def _end_leftover_terminals(settings: Settings, gateway: DockerGateway) -> None:
    conn = db.connect(settings.db_path)
    try:
        ended = transcripts.end_leftovers(conn, gateway)
        if ended:
            log.warning("Ended %d terminal shell(s) left open by the last run", ended)
    finally:
        conn.close()


async def _maintenance_loop(settings: Settings) -> None:
    while True:
        await asyncio.sleep(MAINTENANCE_EVERY)
        try:
            await run_in_threadpool(_maintenance, settings)
        except Exception:
            log.exception("Maintenance failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    # Read once at startup. A bad key file stops the app with a clear message
    # instead of silently not recording.
    transcript_key = (
        transcripts.load_key(settings.transcript_key_file) if settings.transcript_key_file else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Anything this process creates is private to its user (files 600, dirs 700).
        os.umask(0o077)
        logging.getLogger("uvicorn.access").addFilter(_HideHealthchecks())
        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
            sessions.purge_expired(conn, settings)
            transcripts.purge(conn, settings.transcript_retention)
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
                app.state.setup_token = secrets.token_urlsafe(24)
                _announce_setup_token(app.state.setup_token)
        finally:
            conn.close()
        # Docker may be slow or away at startup; this must not delay the app.
        leftovers = asyncio.create_task(
            run_in_threadpool(_end_leftover_terminals, settings, app.state.docker)
        )
        maintenance = asyncio.create_task(_maintenance_loop(settings))
        yield
        for task in (leftovers, maintenance):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    app = FastAPI(
        title="QuenWard",
        lifespan=lifespan,
        # No interactive API docs: they would publish the full route list and
        # load scripts from a CDN, which the Content-Security-Policy forbids.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    # New on every start: restarting invalidates CSRF tokens (the UI refetches them),
    # never sessions.
    app.state.csrf_secret = secrets.token_bytes(32)
    app.state.limiter = LoginLimiter()
    app.state.alerter = alerts.from_config(settings.ntfy)
    app.state.docker = DockerGateway()
    app.state.log_sockets = {}
    app.state.terminal_sockets = {}
    app.state.transcript_key = transcript_key
    app.state.jobs = JobManager(settings, lambda: app.state.docker)
    app.state.setup_token = None
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 echoes the submitted values back, which for the
        # login form would include the password. Only say what was wrong.
        errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        """Liveness check for Docker. Deliberately reveals nothing but 'ok'."""
        return PlainTextResponse("ok")

    # API routers go BEFORE the static mount, so /api/... is never shadowed.
    app.include_router(auth_routes.public_router)
    app.include_router(auth_routes.protected_router)
    app.include_router(stack_routes.router)
    app.include_router(stack_routes.ws_router)
    app.include_router(terminal_routes.router)
    app.include_router(terminal_routes.ws_router)

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")

    return app


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        # Inside the container; real exposure is decided by the compose port binding.
        host="0.0.0.0",  # noqa: S104  # nosec B104
        port=settings.port,
        server_header=False,
        date_header=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
