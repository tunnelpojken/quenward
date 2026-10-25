# Configuration

[← Back to the README](../README.md)

Every setting, phone alerts, themes, and what to do on first start or after a forgotten password.

## Settings

Settings are environment variables in QuenWard's compose file, under
`environment:`. Most installs only need `STACKS_DIR`, and `ALLOWED_ORIGINS`
once QuenWard is behind HTTPS.

| Variable     | Default         | Meaning                               |
| ------------ | --------------- | ------------------------------------- |
| `DATA_DIR`   | `/data`         | SQLite database location              |
| `STACKS_DIR` | `/stacks`       | Folder with one subfolder per stack   |
| `STATIC_DIR` | `frontend/dist` | Built frontend                        |
| `PORT`       | `5005`          | Listening port inside the container   |
| `ALLOWED_ORIGINS` | `http://127.0.0.1:5005,http://localhost:5005` | Comma-separated URLs the UI is opened from. Logins and actions from any other origin are refused. Behind Caddy, set it to your HTTPS address. For `npm run dev`, add `http://127.0.0.1:5173,http://localhost:5173`. |
| `FORWARDED_ALLOW_IPS` | (none) | Address (or CIDR) of your reverse proxy. Only requests from it may set the client IP with `X-Forwarded-For`. See [HTTPS behind a reverse proxy](https.md) |
| `HOST`       | `0.0.0.0` in a container, else `127.0.0.1` | Address to listen on. Inside a container the compose port binding decides what is reachable |
| `DOCKER_HOST` | the local socket | Where Docker is, e.g. a socket proxy (`tcp://socket-proxy:2375`) |
| `DOCKER_CONFIG` | (none) | Folder with a Docker `config.json`, for pulling from private registries |
| `NTFY_URL`   | (off)           | ntfy server for security alerts, e.g. `http://192.168.1.10:8085` |
| `NTFY_TOPIC` | (off)           | Topic for the alerts. Use its own topic, not the one Diun uses |
| `NTFY_TOKEN_FILE` | (off)      | Path to a file holding the ntfy access token |
| `TRANSCRIPT_KEY_FILE` | (off)  | Key file for encrypted terminal transcripts, see [Terminal](terminal.md) |

All paths must be absolute. The three `NTFY_` settings go together: set all of them
or none.

## Security alerts (ntfy)

Failed logins, lockouts, failed setup attempts and password resets are sent to
ntfy. The alerts contain usernames and IP addresses, so give them their own topic
and protect it with an access token, then mount the token as a file:

```yaml
    environment:
      NTFY_URL: http://192.168.1.10:8085
      NTFY_TOPIC: stack-security
      NTFY_TOKEN_FILE: /run/secrets/ntfy_token
    volumes:
      - ./secrets/ntfy_token:/run/secrets/ntfy_token:ro
```

The app runs as user 46337 inside the container, so that user must be able to read
the file: `sudo chown 46337 secrets/ntfy_token && sudo chmod 400 secrets/ntfy_token`.

Alerts are throttled (at most 10 per 5 minutes, then one "suppressed" notice), so a
brute-force attempt cannot flood your phone.

## Themes

Pick a theme in the top bar (or under the login form). **Neutral** follows your
system's light or dark mode; **MAGI** is a dark NERV-style console. The choice
is remembered per browser, not per account.

## Forgot the password

There is deliberately no "forgot password" in the web UI. On the server, run:

```bash
docker exec -it quenward reset-password
```

This logs the user out everywhere and prints a link that works once, for 15 minutes.
The link uses the first address in `ALLOWED_ORIGINS`.

## First start

With no admin account, the app prints a one-time setup token to its log:

```bash
docker logs quenward 2>&1 | grep "setup token"
```

The token creates the first admin and then stops working for good. Until setup is
done, every restart prints a new one.

