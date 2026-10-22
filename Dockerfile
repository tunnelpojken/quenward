# syntax=docker/dockerfile:1

# ---- 1. Build the Svelte frontend ------------------------------------------
FROM node:24-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# ---- 2. Install Python dependencies into a virtualenv ----------------------
FROM python:3.13-slim AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev

# ---- 3. Runtime image: no compilers, no uv, no node -------------------------
FROM python:3.13-slim AS runtime

# Unprivileged user. Phase 3 adds it to the Docker socket's group (GID from an env var).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data \
    && chmod 700 /data

WORKDIR /app
COPY --from=python-deps /app/.venv /app/.venv
COPY app/ ./app/
COPY --from=frontend /build/dist ./frontend/dist
RUN python -m compileall -q app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    STACKS_DIR=/stacks \
    PORT=5005

USER app
EXPOSE 5005
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('PORT', '5005'); body = urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2).read(); sys.exit(0 if body == b'ok' else 1)"]

CMD ["python", "-m", "app.main"]
