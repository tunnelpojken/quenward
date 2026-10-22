# docker-project

Working name. A self-hosted web UI for managing Docker Compose stacks: live logs,
start / stop / update, an exec terminal and a compose editor, behind hardened
authentication. See the build draft for the full design.

**Status: Phase 1 (foundation).** No authentication and no Docker access yet.
Do not expose it to any network.

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs the pinned Python 3.13 by itself)
- Node.js 24+ and npm
- Docker with the compose plugin

## Development

Backend:

```bash
uv sync                                  # create .venv from uv.lock
DATA_DIR=$PWD/data uv run python -m app.main
# -> http://127.0.0.1:5005/healthz
```

Frontend with hot reload (in a second terminal, backend still running):

```bash
cd frontend
npm install
npm run dev                              # -> http://127.0.0.1:5173
```

To have the backend serve the built frontend instead, run `npm run build` once;
the app picks up `frontend/dist` automatically.

Everything in a container:

```bash
docker compose -f compose.dev.yaml up --build
# -> http://127.0.0.1:5005
```

## Checks (the same ones CI runs)

```bash
uv run ruff check .
uv run ruff format --check .
uv run bandit -c pyproject.toml -r app
uv run pip-audit
uv run pytest

cd frontend && npm audit --audit-level=high && npm run build
```

## Configuration

| Variable     | Default         | Meaning                               |
| ------------ | --------------- | ------------------------------------- |
| `DATA_DIR`   | `/data`         | SQLite database location              |
| `STACKS_DIR` | `/stacks`       | Folder with one subfolder per stack   |
| `STATIC_DIR` | `frontend/dist` | Built frontend                        |
| `PORT`       | `5005`          | Listening port inside the container   |

All paths must be absolute.

## Database migrations

Plain SQL files in `app/migrations/`, numbered `0001_…`, `0002_…`, run once each
at startup inside a transaction. Never edit an applied migration; add a new one.
