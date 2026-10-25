# Development

[← Back to the README](../README.md)

For working on QuenWard itself: running it from source, the checks CI runs, and how releases are made.

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs the pinned Python 3.13 by itself)
- Node.js 24+ and npm
- Docker with the compose plugin

## Running from source

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

Everything in a container (it needs your Docker socket's group id once):

```bash
echo "DOCKER_GID=$(stat -c %g /var/run/docker.sock)" > .env
docker compose -f compose.dev.yaml up --build -d
# -> http://127.0.0.1:5005
```

The dev container shows the sample stacks in `dev-stacks/`. Start some to see
them in the dashboard:

```bash
(cd dev-stacks/demo && docker compose up -d)
(cd dev-stacks/oneshot && docker compose up -d)
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

CI also scans the built image: **Trivy** for known vulnerabilities (fails on
fixable HIGH and CRITICAL; accepted exceptions are in `.trivyignore`, each with
a reason and an expiry date) and a **ZAP** baseline scan of the running
container (fails on Medium and High; the plan is `.github/zap/baseline.yaml`).

Tests against a real Docker daemon run in CI (`docker-integration`) and are
opt-in locally. They create throw-away stacks (busybox), start, stop, restart
and update them, and remove everything afterwards:

```bash
DOCKER_TESTS=1 uv run pytest tests/test_docker_integration.py
```

## Releases

Pushing a version tag builds the image for amd64 and arm64, runs the same checks
as CI (smoke test, user check, Trivy), pushes it to
`ghcr.io/tunnelpojken/quenward` and drafts a GitHub release to review and publish
by hand. The tag must match `version` in `pyproject.toml`:

```bash
git tag -a v0.1.0 -m "QuenWard 0.1.0"
git push origin v0.1.0
```

A release `x.y.z` is tagged `x.y.z`, `x.y` and `latest`; a pre-release such as
`0.2.0-rc.1` only gets its own tag. Every image carries an SBOM and build
provenance (`docker buildx imagetools inspect ghcr.io/tunnelpojken/quenward:0.1`).

While the repository is private, so is the image. To pull it on a server, log in
once with a personal access token (classic) that has only `read:packages`:

```bash
docker login ghcr.io -u tunnelpojken    # paste the token as the password
```

## Database migrations

Plain SQL files in `app/migrations/`, numbered `0001_…`, `0002_…`, run once each
at startup inside a transaction. Never edit an applied migration; add a new one.
