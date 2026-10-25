# Stacks, Docker access and updates

[← Back to the README](../README.md)

How QuenWard finds your stacks, what it can see, and what the stack buttons do.

## Docker access

**Access to the Docker socket is root access to the host.** Anyone who can use
this app can start containers that read any file on the machine, or open a
terminal in one that does. That is why login comes first, why the app is meant for LAN / VPN use
only, and why it runs as an unprivileged user that reaches the socket only
through the socket's group (`group_add` with `DOCKER_GID`).

**Mount the stacks folder at the same path as on the host**, for example
`/home/you/docker:/home/you/docker`. Compose records on every container
the folder it was started from, and that is how containers are matched to their
stack. A project with the same name started from another folder is a different
stack; the dashboard shows a hint when it finds one. Folders and compose files
that are symlinks are ignored.

**What the browser never receives:** environment variables, labels, commands,
entrypoints, healthcheck commands and their output. Container details are built
from an explicit list of safe fields. Log lines are shown as plain text.

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

**Going back after an update** is done by hand on the server, for now. Point the
service's image tag back at the kept version, then press **Start** in QuenWard
(compose recreates the container because its image changed). For example, for
the `bookstack` service of the `bookstack` stack:

```bash
docker tag lscr.io/linuxserver/bookstack:rollback-bookstack-bookstack \
           lscr.io/linuxserver/bookstack:latest
```

The next Update pulls the newest version again. To stay on the old one, pin
its version in the compose file instead of `latest`.

**Private registries:** mount a Docker config with your credentials and point
`DOCKER_CONFIG` at it, for example `~/.docker:/run/docker-config:ro` and
`DOCKER_CONFIG=/run/docker-config`.

