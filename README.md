# docker-project

Working name. A self-hosted web UI for managing Docker Compose stacks: live logs,
start / stop / update, an exec terminal and a compose editor, behind hardened
authentication. See the build draft for the full design.

**Status: Phase 2 (authentication).** No Docker access yet.
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

Frontend with hot reload (in a second terminal). The backend must then accept
the dev server's address, so start it with:

```bash
ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173 DATA_DIR=$PWD/data uv run python -m app.main
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
| `ALLOWED_ORIGINS` | `http://127.0.0.1:5005,http://localhost:5005` | Comma-separated URLs the UI is opened from. Logins and actions from any other origin are refused. Behind Caddy, set it to your HTTPS address. For `npm run dev`, add `http://127.0.0.1:5173,http://localhost:5173`. |

| `NTFY_URL`   | (off)           | ntfy server for security alerts, e.g. `http://192.168.0.37:8085` |
| `NTFY_TOPIC` | (off)           | Topic for the alerts. Use its own topic, not the one Diun uses |
| `NTFY_TOKEN_FILE` | (off)      | Path to a file holding the ntfy access token |

All paths must be absolute. The three `NTFY_` settings go together: set all of them
or none.

## Security alerts (ntfy)

Failed logins, lockouts, failed setup attempts and password resets are sent to
ntfy. The alerts contain usernames and IP addresses, so give them their own topic
and protect it with an access token, then mount the token as a file:

```yaml
    environment:
      NTFY_URL: http://192.168.0.37:8085
      NTFY_TOPIC: stack-security
      NTFY_TOKEN_FILE: /run/secrets/ntfy_token
    volumes:
      - ./secrets/ntfy_token:/run/secrets/ntfy_token:ro
```

The app runs as user 10001 inside the container, so that user must be able to read
the file: `sudo chown 10001 secrets/ntfy_token && sudo chmod 400 secrets/ntfy_token`.

Alerts are throttled (at most 10 per 5 minutes, then one "suppressed" notice), so a
brute-force attempt cannot flood your phone.

## Forgot the password

There is deliberately no "forgot password" in the web UI. On the server, run:

```bash
docker exec -it docker-project-dev reset-password
```

This logs the user out everywhere and prints a link that works once, for 15 minutes.
The link uses the first address in `ALLOWED_ORIGINS`.

## First start

With no admin account, the app prints a one-time setup token to its log:

```bash
docker logs docker-project-dev 2>&1 | grep "setup token"
```

The token creates the first admin and then stops working for good. Until setup is
done, every restart prints a new one.

## Database migrations

Plain SQL files in `app/migrations/`, numbered `0001_…`, `0002_…`, run once each
at startup inside a transaction. Never edit an applied migration; add a new one.
