"""Application entry point.

Run locally:  uv run python -m app.main
In Docker:    the image runs the same command.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.config import Settings
from app.security_headers import SecurityHeadersMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Anything this process creates is private to its user (files 600, dirs 700).
        os.umask(0o077)
        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(
        title="docker-project",
        lifespan=lifespan,
        # No interactive API docs: they would publish the full route list and
        # load scripts from a CDN, which the Content-Security-Policy forbids.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        """Liveness check for Docker. Deliberately reveals nothing but 'ok'."""
        return PlainTextResponse("ok")

    # API routers are added here in Phase 2 onwards, BEFORE the static mount,
    # so /api/... is never shadowed by the frontend.

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")

    return app


def run() -> None:
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
