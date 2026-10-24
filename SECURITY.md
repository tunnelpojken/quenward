# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use
**Security → Report a vulnerability** on this repository (a private GitHub
security advisory). Include what you found, how to reproduce it, and the version
or commit.

You can expect a first answer within a week. This is a one-person hobby project,
so fixes are best effort, but security reports come before everything else.
You will be credited in the advisory unless you prefer not to be.

## Supported versions

Only the latest release gets security fixes. Before the first release, only the
`main` branch.

## What this app is, and what it protects

The app manages Docker on the machine it runs on. **Access to the Docker socket
is root access to that machine**: anyone who can log in can start a container
that mounts the host's filesystem, or open a terminal in one. The app does not
try to hide or limit that power from its users. What it does is make sure that
only they get it.

It is built for one admin (or a few trusted ones) on a home network, reached over
the LAN or a VPN, behind HTTPS. **It is not built to face the internet**, and
there is no plan to support that.

In scope:

- Getting in without a valid login: authentication, sessions, password reset,
  first-time setup.
- Doing things as a logged-in user from another website: CSRF, cross-site
  WebSocket hijacking, clickjacking.
- Running script in the app's page: XSS through stack names, container names,
  log lines, compose files or terminal output.
- Reading secrets the app keeps away from the browser: environment variables,
  labels, commands, `.env` files.
- Reading terminal transcripts without the key file.
- Escaping the app's own limits: acting on the app's own stack, reaching files
  outside the stacks folder through the API, more terminals than allowed.

Out of scope:

- Anything a logged-in user can do with Docker. That is the purpose of the app.
- Attacks that need root on the host, or the Docker socket by other means.
- Running it on the internet, without HTTPS, or with `FORWARDED_ALLOW_IPS`
  trusting more than the reverse proxy.
- Denial of service by a logged-in user.

## How it is protected

**Logins**

- Passwords hashed with Argon2id. At least 12 characters, and common passwords
  are refused.
- At most 5 failed logins per username and 20 per IP address in 15 minutes, then
  a 15 minute lockout. Failures, lockouts and resets can be sent to ntfy.
- The first admin is created with a one-time setup token printed to the server's
  log. There is no "forgot password" in the web UI: a reset needs a command on
  the server and gives a link that works once, for 15 minutes.

**Sessions**

- A random token in a `__Host-` cookie: `Secure`, `HttpOnly`, `SameSite=Strict`,
  bound to the exact host. Only its hash is stored.
- Ends after 30 minutes without activity, and after 3 hours in any case.
  Background polling does not count as activity.
- Logging out ends all of your sessions, on every device. Open log streams and
  terminals close within seconds.
- Every change needs a CSRF token and an `Origin` that is in `ALLOWED_ORIGINS`.
  WebSockets (logs, terminal) check the `Origin` too, since browsers do not
  apply the same-origin policy to them.

**The page**

- A strict Content-Security-Policy: scripts, styles, fonts and connections only
  from the app itself, no inline script or style, no framing. Fonts are bundled;
  nothing is loaded from a CDN.
- Log lines, compose files and terminal output are shown as text, never as HTML.
  Terminal escape sequences only reach the terminal emulator.
- `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy` and a
  `Permissions-Policy` on every response. HSTS is left to the HTTPS proxy.

**Docker and the host**

- The container runs as an unprivileged user (UID 46337) with a read-only
  filesystem, all Linux capabilities dropped and `no-new-privileges`. It reaches
  the Docker socket through the socket's group, never as root.
- Container details sent to the browser come from an explicit list of safe
  fields. Environment variables, labels, commands and healthcheck output are
  never sent. `.env` files are never shown or edited.
- `docker compose` runs with a clean environment, so the app's own settings never
  reach a stack.
- The app's own stack is read-only in the UI.
- A terminal is a shell inside a running container, never on the host. Closing
  it ends the shell and everything started from it.

**Records**

- An audit log of logins, actions, compose edits and terminal sessions. It never
  holds passwords, tokens, compose file contents or what was typed.
- Terminal transcripts are off by default. When on, they are encrypted with
  AES-256-GCM using a key kept in a separate file, never in the database, and
  deleted after 14 days.

## How it is checked

Every push runs, in CI:

- Ruff and Bandit on the Python code, and the test suite (including tests
  against a real Docker daemon).
- `pip-audit` and `npm audit` for known vulnerabilities in dependencies.
- **Trivy** on the built image: fails on HIGH or CRITICAL vulnerabilities that
  have a fix, and on secrets found in the image.
- A **ZAP** baseline scan of the running container: fails on Medium or High.

All dependencies are pinned in lock files, and the scanners are downloaded from
their releases and checked against pinned SHA-256 checksums, not pulled in as
third-party actions.

ZAP active scans (which attack the app) have been run by hand, with and without
the login rate limit. They found no Medium or High issues and caused no server
errors.

**Accepted findings** are listed in [`.trivyignore`](.trivyignore), each with the
reason it does not apply and an expiry date, after which the scan fails on it
again.

## Running it safely

- Serve it over HTTPS on your LAN or VPN only. See "HTTPS behind a reverse proxy"
  in the README and [`examples/behind-caddy/`](examples/behind-caddy/).
- Do not forward its port on your router and do not put it behind a public
  tunnel (Cloudflare Tunnel, ngrok and the like).
- Bind the app's own port to `127.0.0.1`, as in the examples.
- Set `ALLOWED_ORIGINS` to the exact address you use, and `FORWARDED_ALLOW_IPS`
  to your proxy's address only.
- Keep secrets (ntfy token, transcript key, DNS API token) in files readable only
  by the user that needs them, not in environment variables.
- Back up the transcript key and the database separately.
- Make sure no other container on the machine runs as UID 46337.
