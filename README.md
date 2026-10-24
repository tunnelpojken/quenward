# QuenWard

A self-hosted web UI for managing Docker Compose stacks: live logs,
start / stop / update, an exec terminal and a compose editor, behind hardened
authentication. See the build draft for the full design.

**Status: Phase 6 (hardening before a first release).** Stacks, live logs, start /
stop / restart / update, an interactive terminal into containers, and editing
compose files. Meant for your LAN or VPN only, behind HTTPS: see
[HTTPS behind a reverse proxy](#https-behind-a-reverse-proxy) and
[SECURITY.md](SECURITY.md). Never expose it to the internet.

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

## Docker access

**Access to the Docker socket is root access to the host.** Anyone who can use
this app can start containers that read any file on the machine, or open a
terminal in one that does. That is why login comes first, why the app is meant for LAN / VPN use
only, and why it runs as an unprivileged user that reaches the socket only
through the socket's group (`group_add` with `DOCKER_GID`).

**Mount the stacks folder at the same path as on the host**, for example
`/home/fested/docker:/home/fested/docker`. Compose records on every container
the folder it was started from, and that is how containers are matched to their
stack. A project with the same name started from another folder is a different
stack; the dashboard shows a hint when it finds one. Folders and compose files
that are symlinks are ignored.

**What the browser never receives:** environment variables, labels, commands,
entrypoints, healthcheck commands and their output. Container details are built
from an explicit list of safe fields. Log lines are shown as plain text.

## HTTPS behind a reverse proxy

The session cookie is `Secure`, so browsers only send it over HTTPS or to
`localhost`. To use the app from another machine, put it behind a reverse proxy
with a real certificate. [`examples/behind-caddy/`](examples/behind-caddy/) is a
complete, working setup: the app plus its own Caddy, in one compose file.

**Why its own Caddy.** The app is not something to share a proxy with a public
website. A separate Caddy, on its own port, has no route from your public sites
or tunnels to the app, and a mistake in one config cannot expose the other.

**The certificate comes from a DNS challenge**, so the server does not have to be
reachable from the internet. Caddy creates a temporary TXT record through your
DNS provider's API and Let's Encrypt checks it. The example uses Cloudflare:

1. Your domain's DNS must be on Cloudflare. Create an API token with
   **Zone → DNS → Edit** and **Zone → Zone → Read**, for that zone only.
2. Save it in a file only Caddy's user can read (never in the environment, where
   `docker inspect` shows it):

   ```bash
   mkdir -p secrets caddy/data caddy/config
   nano secrets/cloudflare_token          # paste the token, save
   sudo chown 46338:46338 secrets/cloudflare_token caddy/data caddy/config
   sudo chmod 400 secrets/cloudflare_token
   ```

3. Point the name at the server **on your local DNS only** (Pi-hole, your router),
   not in public DNS. For example `stacks.example.com → 192.168.0.37`. A public
   record is not needed for the certificate, and leaving it out keeps the name
   from leading anywhere outside your network.
4. Build Caddy with the Cloudflare module, then start everything:

   ```bash
   docker build -t caddy-cloudflare:local caddy-image
   echo "DOCKER_GID=$(stat -c %g /var/run/docker.sock)" > .env
   docker compose up -d
   docker logs -f quenward-caddy     # "certificate obtained successfully"
   ```

Then open `https://stacks.example.com:8443`.

Things the example does on purpose, and that matter if you build your own:

- **`ALLOWED_ORIGINS` must list the HTTPS address** exactly as typed in the
  browser, port included. Logins and actions from any other origin are refused.
- **`FORWARDED_ALLOW_IPS` names the proxy, and only the proxy.** The app then
  takes the client's IP from `X-Forwarded-For`, which the login rate limit, the
  security alerts and the audit log use. Trusting it from anywhere else lets
  anyone pick the IP they appear to come from. The example gives Caddy a fixed
  address on a small network for this. A CIDR range works too, but keep it to
  networks only the proxy is on. Leave it unset without a proxy.
- **Caddy runs as its own user** (46338), so it can read the Cloudflare token
  and the app cannot, and the other way around for the app's secrets.
- **`admin off`**: nothing can reconfigure Caddy while it runs.
- **`setcap -r` in the Caddy image.** The official image gives the binary a
  capability to bind ports below 1024. With `cap_drop: ALL` and
  `no-new-privileges` it then refuses to start (`exec ... operation not
  permitted`). Port 8443 needs no capability.
- **`resolvers 1.1.1.1 1.0.0.1`**: Caddy checks the challenge record on public
  DNS. A local resolver that answers the name itself (Pi-hole) would never show
  the TXT record.

**Using a proxy you already have** (Caddy, Traefik, nginx) works the same way:
proxy to `app:5005` (WebSockets must be passed through, which Caddy and Traefik
do by default), set `ALLOWED_ORIGINS` to the HTTPS address, and set
`FORWARDED_ALLOW_IPS` to the proxy's address on the network it shares with the
app. Do not route it through a public tunnel (Cloudflare Tunnel, ngrok) and do
not forward its port on your router.

**If the proxy is down**, the app is still reachable over an SSH tunnel through
its `127.0.0.1` port binding:

```bash
ssh -L 5005:127.0.0.1:5005 you@server
# then open http://127.0.0.1:5005 (keep it in ALLOWED_ORIGINS for this)
```

## Stack actions

Start, Stop, Restart and Update run `docker compose` in the stack's folder, in
the background, with the output shown live. Stop, Restart and Update ask first.

- **One action per stack at a time**, and at most two overall (parallel pulls
  are heavy on a small server).
- **The app's own stack is read-only**: stopping it would cut you off mid-action.
  Update it over SSH.
- **Compose runs with a clean environment.** Compose fills `${VARIABLES}` from
  its own environment before the stack's `.env`, so the app's settings would
  otherwise leak into every stack.
- **Every action is in the audit log**, with what changed. Command output is
  shown but never stored there.

**Update** pulls new images and recreates the containers that changed. Services
with `build:` are skipped. The previous image of every service is kept, exactly
one generation, using tags the app manages on your images:

| Tag | Points at | Why |
| --- | --- | --- |
| `<repo>:current-<stack>-<service>` | the image the service runs | Keeps it alive if something else (Dockge, a manual `docker pull`) moves the main tag. On Docker's containerd image store an untagged image is deleted at once, even while in use. |
| `<repo>:rollback-<stack>-<service>` | the image it ran before the last update | The kept previous version. The one before that is removed, unless another stack still uses it. |
| `<repo>:pending-<stack>-<service>` | the running image, during an update only | Protects it while the update runs; removed afterwards. |

The `current` tags are set whenever the app starts, restarts or updates a stack.
For a stack the app has never touched, an update after an outside pull can
report that the previous image could not be kept.

**Private registries:** mount a Docker config with your credentials and point
`DOCKER_CONFIG` at it, for example `~/.docker:/run/docker-config:ro` and
`DOCKER_CONFIG=/run/docker-config`.

## Terminal

**Terminal** on a running container opens a shell inside it (`docker exec`):
bash if the image has it, otherwise sh. Never a shell on the host. It is the most
powerful thing in the app, so:

- **Only running containers of a stack**, never the app's own stack.
- **At most 2 terminals per login, 4 in total.**
- **Closed after 15 minutes without typing.** Typing counts as activity for the
  login session; an open terminal nobody types in does not keep you logged in.
- **Closing the terminal ends the shell and everything started from it.** Docker
  itself leaves an exec running when the connection drops, so the app ends the
  shell's whole session (hangup, then kill a second later), background jobs and
  `nohup` included. Processes that detach themselves completely (daemons calling
  `setsid`) are left alone, as with `docker exec`. Shells left open when the app
  stopped are ended at its next start.
- **Opening and closing are in the audit log**, never what was typed.

### Transcripts (optional, off by default)

With a key, every terminal session is recorded (everything typed and shown,
including what the screen hides, such as a password typed at a prompt) and kept
for 14 days under **Terminal history**. Viewing one is audited.

Transcripts are encrypted (AES-256-GCM) with a key that lives in a file, never in
the database: a copy of the database alone reveals nothing of what was typed.
Create the key once on the server:

```bash
mkdir -p secrets
openssl rand -base64 32 > secrets/transcript.key
sudo chown 46337 secrets/transcript.key && sudo chmod 400 secrets/transcript.key
```

```yaml
    environment:
      TRANSCRIPT_KEY_FILE: /run/secrets/transcript.key
    volumes:
      - ./secrets/transcript.key:/run/secrets/transcript.key:ro
```

**Back up the key and the database separately**, never in the same place (for
example: the database with the server's normal backups, the key in a password
manager). Without the key, old transcripts cannot be read by anyone; with both
together, they can. Changing the key makes older transcripts unreadable.

## Compose editor

Click a stack's compose file name to view and edit it. Saving:

1. **Refuses if the file changed on disk** since you opened it (an edit over SSH
   is never overwritten), and while an action runs on the stack.
2. **Checks the YAML**, then runs **`docker compose config`** on the new text in
   the stack's own folder, so relative paths and the stack's `.env` resolve
   exactly as they will on start. Compose's own error message is shown.
3. **Keeps the previous version as `compose.yaml.bak`** (one generation), then
   writes the file **in place**, so it keeps its owner and permissions.

Saving does not touch running containers: **Apply** (`docker compose up -d`)
recreates the services that changed. `.env` files are never shown or edited, and
the audit log records hashes and line counts, never the content (compose files
often hold passwords). The app's own stack is shown read-only.

**Allowing the app to write.** The app runs as user 46337, and your compose files
belong to you. Give it write access to the stack folders and compose files only,
not to everything inside (stack data stays out of reach), with an ACL:

```bash
sudo apt install acl   # if setfacl is missing
cd /home/fested/docker
for d in */; do
  sudo setfacl -m u:46337:rwx "$d"                 # to create the .bak and a check file
  for f in "$d"compose.yaml "$d"compose.yml "$d"docker-compose.yaml "$d"docker-compose.yml; do
    [ -f "$f" ] && sudo setfacl -m u:46337:rw "$f"
  done
done
```

Run it again after adding a stack. Without write access the editor shows the file
read-only. Some editors replace a file when saving (a new file without the ACL);
if a stack turns read-only after an SSH edit, run the loop again.

The stacks folder must be mounted without `:ro` for this.

Why 46337: user IDs are shared between the host and every container. Many images
run as 10001 or 1000, and a container with the same UID as this app could read
and write what the app can (and the other way around). Check with
`docker ps -q | xargs -n1 docker top | awk 'NR>1{print $1}' | sort -u` that no
container of yours runs as 46337. To use another UID, build with
`--build-arg APP_UID=...` and use that number in the commands above.

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

## Configuration

| Variable     | Default         | Meaning                               |
| ------------ | --------------- | ------------------------------------- |
| `DATA_DIR`   | `/data`         | SQLite database location              |
| `STACKS_DIR` | `/stacks`       | Folder with one subfolder per stack   |
| `STATIC_DIR` | `frontend/dist` | Built frontend                        |
| `PORT`       | `5005`          | Listening port inside the container   |
| `ALLOWED_ORIGINS` | `http://127.0.0.1:5005,http://localhost:5005` | Comma-separated URLs the UI is opened from. Logins and actions from any other origin are refused. Behind Caddy, set it to your HTTPS address. For `npm run dev`, add `http://127.0.0.1:5173,http://localhost:5173`. |
| `FORWARDED_ALLOW_IPS` | (none) | Address (or CIDR) of your reverse proxy. Only requests from it may set the client IP with `X-Forwarded-For`. See [HTTPS behind a reverse proxy](#https-behind-a-reverse-proxy) |
| `DOCKER_HOST` | the local socket | Where Docker is, e.g. a socket proxy (`tcp://socket-proxy:2375`) |
| `DOCKER_CONFIG` | (none) | Folder with a Docker `config.json`, for pulling from private registries |
| `NTFY_URL`   | (off)           | ntfy server for security alerts, e.g. `http://192.168.0.37:8085` |
| `NTFY_TOPIC` | (off)           | Topic for the alerts. Use its own topic, not the one Diun uses |
| `NTFY_TOKEN_FILE` | (off)      | Path to a file holding the ntfy access token |
| `TRANSCRIPT_KEY_FILE` | (off)  | Key file for encrypted terminal transcripts, see [Terminal](#terminal) |

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

The app runs as user 46337 inside the container, so that user must be able to read
the file: `sudo chown 46337 secrets/ntfy_token && sudo chmod 400 secrets/ntfy_token`.

Alerts are throttled (at most 10 per 5 minutes, then one "suppressed" notice), so a
brute-force attempt cannot flood your phone.

## Forgot the password

There is deliberately no "forgot password" in the web UI. On the server, run:

```bash
docker exec -it quenward-dev reset-password
```

This logs the user out everywhere and prints a link that works once, for 15 minutes.
The link uses the first address in `ALLOWED_ORIGINS`.

## First start

With no admin account, the app prints a one-time setup token to its log:

```bash
docker logs quenward-dev 2>&1 | grep "setup token"
```

The token creates the first admin and then stops working for good. Until setup is
done, every restart prints a new one.

## Database migrations

Plain SQL files in `app/migrations/`, numbered `0001_…`, `0002_…`, run once each
at startup inside a transaction. Never edit an applied migration; add a new one.
