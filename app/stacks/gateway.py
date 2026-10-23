"""The only module that talks to Docker.

Raw Docker data never leaves this file. Everything it returns is built from an
explicit whitelist of fields (_summary, _details), so values that often hold
secrets are never copied anywhere: environment variables, labels, the command
and its arguments, the entrypoint, healthcheck commands and their output.
"""

import os
import re
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
import docker.errors
import requests.exceptions

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
COMPOSE_WORKING_DIR = "com.docker.compose.project.working_dir"

# Shown when a port is published on every interface (display only).
_ALL_INTERFACES = "0.0.0.0"  # noqa: S104  # nosec B104
_HEALTH_IN_STATUS = re.compile(r"\((healthy|unhealthy|health: starting)\)")


class DockerUnavailable(Exception):
    """Docker could not be reached. The message is safe to show to the user."""


@dataclass(frozen=True)
class ContainerSummary:
    id: str
    name: str
    project: str | None
    service: str | None
    working_dir: str | None  # internal: used to match containers to stack folders
    image: str
    state: str
    status: str
    health: str | None
    created: int
    ports: tuple[str, ...]
    image_id: str = ""  # internal: full image id, used when updating

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "service": self.service,
            "image": self.image,
            "state": self.state,
            "status": self.status,
            "health": self.health,
            "created": self.created,
            "ports": list(self.ports),
        }


def _port_key(port: str) -> tuple[int, str]:
    """'80/tcp' sorts before '443/tcp' (by number, not as text)."""
    number, _, proto = str(port).partition("/")
    return (int(number) if number.isdigit() else 0, proto)


def _str(value: Any, limit: int = 512) -> str:
    return str(value)[:limit] if value is not None else ""


def _summary(attrs: dict[str, Any]) -> ContainerSummary:
    """Whitelist for the container list API (sparse attrs)."""
    labels = attrs.get("Labels") or {}
    names = attrs.get("Names") or []
    status = _str(attrs.get("Status"), 128)
    health = _HEALTH_IN_STATUS.search(status)
    ports = set()
    for p in attrs.get("Ports") or []:
        if not isinstance(p, dict):
            continue
        inside = f"{p.get('PrivatePort')}/{p.get('Type')}"
        if p.get("PublicPort"):
            ports.add(f"{p.get('IP') or _ALL_INTERFACES}:{p['PublicPort']}->{inside}")
        else:
            ports.add(inside)
    return ContainerSummary(
        id=_str(attrs.get("Id"))[:12],
        name=_str(names[0] if names else "").lstrip("/"),
        project=labels.get(COMPOSE_PROJECT),
        service=labels.get(COMPOSE_SERVICE),
        working_dir=labels.get(COMPOSE_WORKING_DIR),
        image=_str(attrs.get("Image"), 256),
        state=_str(attrs.get("State"), 32),
        status=status,
        health=health.group(1).replace("health: ", "") if health else None,
        created=int(attrs.get("Created") or 0),
        ports=tuple(sorted(ports, key=lambda p: _port_key(p.rsplit("->", 1)[-1]))),
        image_id=_str(attrs.get("ImageID"), 80),
    )


def _details(attrs: dict[str, Any]) -> dict[str, Any]:
    """Whitelist for a full container inspect. Add fields here deliberately, one by one."""
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    host_config = attrs.get("HostConfig") or {}
    network = attrs.get("NetworkSettings") or {}
    labels = config.get("Labels") or {}

    ports = []
    for container_port, bindings in sorted(
        (network.get("Ports") or {}).items(), key=lambda item: _port_key(item[0])
    ):
        if not bindings:
            ports.append(_str(container_port, 32))
            continue
        for b in bindings:
            host = b.get("HostIp") or _ALL_INTERFACES
            ports.append(f"{host}:{b.get('HostPort')}->{container_port}")

    return {
        "id": _str(attrs.get("Id"))[:12],
        "name": _str(attrs.get("Name")).lstrip("/"),
        "service": labels.get(COMPOSE_SERVICE),
        "image": _str(config.get("Image"), 256),
        "image_id": _str(attrs.get("Image")).removeprefix("sha256:")[:12],
        "state": _str(state.get("Status"), 32),
        # The status word only, never the health log. Only while running: Docker
        # keeps the last status on a stopped container, where it means nothing.
        "health": (state.get("Health") or {}).get("Status") if state.get("Running") else None,
        "exit_code": state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled")),
        "started_at": _str(state.get("StartedAt"), 64),
        "finished_at": _str(state.get("FinishedAt"), 64),
        "created": _str(attrs.get("Created"), 64),
        "restart_count": int(attrs.get("RestartCount") or 0),
        "restart_policy": _str((host_config.get("RestartPolicy") or {}).get("Name"), 32),
        "ports": ports,
        "networks": sorted(_str(n, 128) for n in (network.get("Networks") or {})),
        "mounts": [
            {
                "type": _str(m.get("Type"), 16),
                "source": _str(m.get("Source"), 512),
                "destination": _str(m.get("Destination"), 512),
                "read_only": not m.get("RW", True),
            }
            for m in attrs.get("Mounts") or []
            if isinstance(m, dict)
        ],
    }


def _permission_denied(error: BaseException) -> bool:
    """True when the socket exists but this user may not open it (wrong group)."""
    seen: set[int] = set()
    stack: list[object] = [error]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, PermissionError):
            return True
        if isinstance(item, BaseException):
            stack.extend([item.__cause__, item.__context__, *item.args])
        elif isinstance(item, tuple | list):  # requests nests errors in tuples
            stack.extend(item)
    return False


def _unavailable(error: BaseException) -> DockerUnavailable:
    """Turn a Docker error into a message that says what to fix."""
    if isinstance(error, docker.errors.APIError) and error.status_code == 403:
        return DockerUnavailable("Docker refused the request")
    if _permission_denied(error):
        return DockerUnavailable("No permission to use the Docker socket: check DOCKER_GID")
    return DockerUnavailable("Docker is not reachable")


class DockerGateway:
    def __init__(self, timeout: int = 10) -> None:
        self._timeout = timeout
        self._client: docker.DockerClient | None = None
        self._lock = threading.Lock()
        self._self_id: str | None = None
        self._self_checked = False

    def _get_client(self) -> docker.DockerClient:
        with self._lock:
            if self._client is None:
                try:
                    # Reads DOCKER_HOST, so a socket proxy can be used without code changes.
                    # Creating the client already talks to Docker (API version check).
                    self._client = docker.DockerClient.from_env(timeout=self._timeout)
                except docker.errors.DockerException as e:
                    raise _unavailable(e) from None
            return self._client

    def _call[T](self, fn: Callable[[docker.DockerClient], T]) -> T:
        try:
            return fn(self._get_client())
        except docker.errors.NotFound:
            raise
        except (docker.errors.DockerException, requests.exceptions.RequestException) as e:
            with self._lock:
                self._client = None  # reconnect next time
            raise _unavailable(e) from None

    def ping(self) -> bool:
        try:
            return bool(self._call(lambda c: c.ping()))
        except DockerUnavailable:
            return False

    def compose_containers(self) -> list[ContainerSummary]:
        """All containers created by docker compose, running or not."""
        containers = self._call(
            lambda c: c.containers.list(all=True, sparse=True, filters={"label": COMPOSE_PROJECT})
        )
        return [_summary(c.attrs) for c in containers]

    def inspect(self, container_id: str) -> dict[str, Any]:
        container = self._call(lambda c: c.containers.get(container_id))
        return _details(container.attrs)

    def self_container_id(self) -> str | None:
        """The id of the container this app runs in, or None when not in a container."""
        if not self._self_checked:
            self._self_checked = True
            if Path("/.dockerenv").exists():
                try:
                    # Docker sets the hostname to the container id unless told otherwise.
                    self._self_id = self.inspect(socket.gethostname())["id"]
                except (docker.errors.NotFound, DockerUnavailable):
                    self._self_id = os.environ.get("SELF_CONTAINER_ID") or None
        return self._self_id

    def history(self, container_id: str, tail: int, until: float) -> bytes:
        return self._call(
            lambda c: c.api.logs(
                container_id, stdout=True, stderr=True, timestamps=True, tail=tail, until=until
            )
        )

    def follow(self, container_id: str, since: float) -> Iterator[bytes]:
        """A blocking stream of new log output. Call .close() on it to stop."""
        return self._call(
            lambda c: c.api.logs(
                container_id,
                stdout=True,
                stderr=True,
                timestamps=True,
                stream=True,
                follow=True,
                since=since,
            )
        )

    # ------------------------------------------------------------ images (updates)

    def _image_op(self, fn: Callable[[docker.DockerClient], Any]) -> bool:
        """Run an image operation. False when the image is missing or still in use."""
        try:
            fn(self._get_client())
            return True
        except docker.errors.NotFound:
            return False
        except docker.errors.APIError as e:
            if e.status_code == 409:  # in use by a container, or still has other tags
                return False
            raise _unavailable(e) from None
        except (docker.errors.DockerException, requests.exceptions.RequestException) as e:
            raise _unavailable(e) from None

    def image_id(self, ref: str) -> str | None:
        """The id of an image reference (tag or id), or None if it does not exist."""
        found: list[str] = []
        if self._image_op(lambda c: found.append(c.images.get(ref).id)):
            return found[0]
        return None

    def tag_image(self, image_id: str, repository: str, tag: str) -> bool:
        """Point repository:tag at image_id (moving the tag if it already exists)."""
        return self._image_op(lambda c: c.api.tag(image_id, repository, tag, force=True))

    def remove_image(self, ref: str) -> bool:
        """Remove a tag, or an image by id. Never forced: Docker refuses while in use."""
        return self._image_op(lambda c: c.api.remove_image(ref, force=False))

    def remove_untagged(self, image_id: str) -> bool:
        """Remove an image by id only if no tag points at it any more.

        Removing by id would also strip remaining tags, and another stack's
        rollback or current tag may still be on a shared image.
        """
        tags: list[list[str]] = []
        if not self._image_op(lambda c: tags.append(c.images.get(image_id).tags)):
            return False  # already gone (the containerd store deletes untagged images)
        if tags[0]:
            return False
        return self.remove_image(image_id)

    # ------------------------------------------------------------ terminal (exec)

    def open_shell(self, container_id: str) -> "ShellHandle":
        """Start an interactive shell (bash if present, else sh) with a TTY."""

        def start(c: docker.DockerClient) -> ShellHandle:
            exec_id = c.api.exec_create(
                container_id,
                ["/bin/sh", "-c", SHELL_PROBE],
                stdin=True,
                tty=True,
                environment={"TERM": "xterm-256color"},
            )["Id"]
            response = c.api.exec_start(exec_id, tty=True, socket=True)
            # The SDK wraps the hijacked connection; the raw socket is what we read.
            raw = getattr(response, "_sock", response)
            return ShellHandle(exec_id=exec_id, sock=raw)

        return self._call(start)

    def resize_shell(self, exec_id: str, rows: int, cols: int) -> None:
        self._call(lambda c: c.api.exec_resize(exec_id, height=rows, width=cols))

    def shell_exit_code(self, exec_id: str) -> int | None:
        """The exit code once the shell has ended; None while it still runs."""
        info = self._call(lambda c: c.api.exec_inspect(exec_id))
        return None if info.get("Running") else info.get("ExitCode")

    def end_shell_session(self, container_id: str, pid: int) -> None:
        """End the shell and everything started from it.

        Closing the connection does not stop a Docker exec. A TTY exec makes the
        shell a session leader, so every process it started shares its session
        id: those get a hangup, then a kill one second later. Uses shell builtins
        only, so it works in minimal images. The pid is passed as an argument,
        never pasted into the script.
        """
        if pid <= 1:
            raise ValueError("refusing to signal pid 0 or the container's init")
        self._call(
            lambda c: c.api.exec_start(
                c.api.exec_create(container_id, ["/bin/sh", "-c", _END_SESSION, "end", str(pid)])[
                    "Id"
                ]
            )
        )


# The first thing the shell prints is a marker with its name and pid (inside the
# container), read and removed by the app before any output reaches the browser.
# \036 is the ASCII record separator, which never appears in normal output.
SHELL_PROBE = (
    r'if command -v bash >/dev/null 2>&1; then printf "\036bash %s\036" "$$"; exec bash; fi; '
    r'printf "\036sh %s\036" "$$"; exec sh'
)

_END_SESSION = r"""
sid=$1
for sig in HUP KILL; do
  for d in /proc/[0-9]*; do
    read -r s < "$d/stat" 2>/dev/null || continue
    s=${s##*) }
    set -- $s
    [ "$4" = "$sid" ] && kill -"$sig" "${d#/proc/}" 2>/dev/null
  done
  [ "$sig" = HUP ] && sleep 1
done
true
"""


@dataclass
class ShellHandle:
    exec_id: str
    sock: socket.socket
