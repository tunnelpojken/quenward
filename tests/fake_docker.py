"""A stand-in for DockerGateway, so stack and log tests run without Docker."""

import contextlib
import queue
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import docker.errors

from app.stacks.gateway import (
    ContainerSummary,
    DockerUnavailable,
    ShellHandle,
    _details,
    _summary,
)


def container_attrs(
    cid: str,
    service: str,
    project: str,
    working_dir: str,
    state: str = "running",
    status: str = "Up 5 minutes",
    **extra: Any,
) -> dict[str, Any]:
    """Docker list-API style attrs, including fields that must never leak."""
    return {
        "Id": cid.ljust(64, "0"),
        "Names": [f"/{project}-{service}-1"],
        "Image": f"example/{service}:1.0",
        "ImageID": f"sha256:{service}-v1",
        "Command": "run --password=list-command-secret",
        "Created": 1_700_000_000,
        "State": state,
        "Status": status,
        "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
            "com.docker.compose.project.working_dir": working_dir,
            "traefik.http.middlewares.auth.basicauth.users": "admin:label-secret-hash",
        },
        **extra,
    }


def inspect_attrs(summary: dict[str, Any]) -> dict[str, Any]:
    """Docker inspect-API style attrs for the same container, full of secrets."""
    return {
        "Id": summary["Id"],
        "Name": summary["Names"][0],
        "Image": "sha256:" + "ab" * 32,
        "Created": "2026-10-23T06:00:00.000000000Z",
        "RestartCount": 2,
        "Args": ["--password=args-secret"],
        "Path": "/entrypoint.sh",
        "State": {
            "Status": summary["State"],
            "Running": summary["State"] == "running",
            "ExitCode": 0,
            "OOMKilled": False,
            "StartedAt": "2026-10-23T06:00:01Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": {
                "Status": "healthy",
                "Log": [{"Output": "curl -u admin:health-output-secret ok"}],
            },
        },
        "Config": {
            "Image": summary["Image"],
            "Env": ["DB_PASSWORD=env-secret", "PATH=/usr/bin"],
            "Cmd": ["--token", "cmd-secret"],
            "Entrypoint": ["/run.sh", "entrypoint-secret"],
            "Labels": summary["Labels"],
            "Healthcheck": {"Test": ["CMD", "curl", "-u", "admin:healthcheck-secret"]},
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "NetworkSettings": {
            "Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}], "443/tcp": None},
            "Networks": {"web": {"IPAddress": "172.18.0.2"}},
        },
        "Mounts": [{"Type": "bind", "Source": "/srv/data", "Destination": "/data", "RW": True}],
        "SomeFutureField": "future-field-secret",
    }


SECRETS = [
    "list-command-secret",
    "label-secret-hash",
    "args-secret",
    "health-output-secret",
    "env-secret",
    "cmd-secret",
    "entrypoint-secret",
    "healthcheck-secret",
    "future-field-secret",
]


class FakeStream:
    """A blocking log stream like the docker SDK's; close() ends it."""

    def __init__(self) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self.closed = False

    def feed(self, data: bytes) -> None:
        self._queue.put(data)

    def close(self) -> None:
        self.closed = True
        self._queue.put(None)

    def __iter__(self) -> Iterator[bytes]:
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item


class FakeShell:
    """A tiny shell on the far end of a socket pair, like a container's sh.

    Understands `echo TEXT`, `flood N` (prints N bytes) and `exit N`. Everything
    received is kept in .received, like a real TTY would see it.
    """

    def __init__(self, exec_id: str, pid: int, shell: str = "sh", probe: bool = True) -> None:
        self.exec_id = exec_id
        self.pid = pid
        self.app_side, self._side = socket.socketpair()
        self.received = b""
        self.exit_code: int | None = None
        self.ended = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(shell, probe), daemon=True)
        self._thread.start()

    def _send(self, data: bytes) -> None:
        try:
            self._side.sendall(data)
        except OSError:
            self.ended.set()

    def _run(self, shell: str, probe: bool) -> None:
        if not probe:  # like an image without /bin/sh: an error, then nothing
            self._send(b'exec: "/bin/sh": stat /bin/sh: no such file or directory\r\n')
            self._side.close()
            self.exit_code = 127
            return
        self._send(f"\x1e{shell} {self.pid}\x1e$ ".encode())
        pending = b""
        while True:
            try:
                data = self._side.recv(4096)
            except OSError:
                break
            if not data:
                break
            self.received += data
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                command, _, arg = line.decode().strip().partition(" ")
                if command == "echo":
                    self._send(f"{line.decode()}\r\n{arg}\r\n$ ".encode())
                elif command == "flood":
                    self._send(b"x" * int(arg) + b"\r\n$ ")
                elif command == "exit":
                    self.exit_code = int(arg or 0)
                    self._side.close()
                    self.ended.set()
                    return
                else:
                    self._send(b"$ ")
        self.ended.set()

    def kill(self) -> None:
        if self.exit_code is None:
            self.exit_code = 129  # hangup
        with contextlib.suppress(OSError):
            self._side.shutdown(socket.SHUT_RDWR)
        self._side.close()
        self.ended.set()


@dataclass
class FakeGateway:
    containers: list[dict[str, Any]] = field(default_factory=list)
    logs: dict[str, bytes] = field(default_factory=dict)  # container id prefix -> history
    available: bool = True
    self_id: str | None = None
    streams: dict[str, list[FakeStream]] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)  # tag -> image id
    removed: list[str] = field(default_factory=list)
    shells: list[FakeShell] = field(default_factory=list)
    resizes: list[tuple[str, int, int]] = field(default_factory=list)
    ended_sessions: list[tuple[str, int]] = field(default_factory=list)
    has_shell: bool = True

    def _check(self) -> None:
        if not self.available:
            raise DockerUnavailable("Docker is not reachable")

    def compose_containers(self) -> list[ContainerSummary]:
        self._check()
        return [_summary(c) for c in self.containers]

    def inspect(self, container_id: str) -> dict[str, Any]:
        self._check()
        for c in self.containers:
            if c["Id"].startswith(container_id):
                return _details(inspect_attrs(c))
        raise docker.errors.NotFound("no such container")

    def self_container_id(self) -> str | None:
        return self.self_id

    def history(self, container_id: str, tail: int, until: float) -> bytes:
        self._check()
        lines = self.logs.get(container_id, b"").splitlines(keepends=True)
        return b"".join(lines[-tail:]) if tail else b""

    def follow(self, container_id: str, since: float) -> FakeStream:
        self._check()
        stream = FakeStream()
        self.streams.setdefault(container_id, []).append(stream)
        return stream

    # ------------------------------------------------------------ images

    def image_id(self, ref: str) -> str | None:
        if ref in self.images:
            return self.images[ref]
        if not ref.startswith("sha256:") or ref in self.removed:
            return None
        # Like the containerd image store: an image with no tag left is gone.
        return ref if ref in self.images.values() else None

    def tag_image(self, image_id: str, repository: str, tag: str) -> bool:
        self.images[f"{repository}:{tag}"] = image_id
        return True

    def in_use(self, image_id: str) -> bool:
        return any(c.get("ImageID") == image_id for c in self.containers)

    def remove_image(self, ref: str) -> bool:
        if ref in self.images:  # a tag: untag it
            del self.images[ref]
            return True
        if self.in_use(ref) or ref in self.images.values():
            return False  # Docker refuses: in use, or still tagged
        self.removed.append(ref)
        return True

    def remove_untagged(self, image_id: str) -> bool:
        if image_id in self.images.values() or self.in_use(image_id):
            return False
        if image_id in self.removed:
            return False
        self.removed.append(image_id)
        return True

    # ------------------------------------------------------------ terminal

    def open_shell(self, container_id: str) -> ShellHandle:
        self._check()
        shell = FakeShell(
            f"exec{len(self.shells)}", pid=40 + len(self.shells), probe=self.has_shell
        )
        self.shells.append(shell)
        return ShellHandle(exec_id=shell.exec_id, sock=shell.app_side)

    def resize_shell(self, exec_id: str, rows: int, cols: int) -> None:
        self.resizes.append((exec_id, rows, cols))

    def _shell(self, exec_id: str) -> FakeShell:
        for shell in self.shells:
            if shell.exec_id == exec_id:
                return shell
        raise docker.errors.NotFound("no such exec")

    def shell_exit_code(self, exec_id: str) -> int | None:
        shell = self._shell(exec_id)
        return shell.exit_code if shell.ended.is_set() else None

    def end_shell_session(self, container_id: str, pid: int) -> None:
        self.ended_sessions.append((container_id, pid))
        for shell in self.shells:
            if shell.pid == pid:
                shell.kill()
