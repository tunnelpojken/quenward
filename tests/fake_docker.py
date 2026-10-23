"""A stand-in for DockerGateway, so stack and log tests run without Docker."""

import queue
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import docker.errors

from app.stacks.gateway import ContainerSummary, DockerUnavailable, _details, _summary


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


@dataclass
class FakeGateway:
    containers: list[dict[str, Any]] = field(default_factory=list)
    logs: dict[str, bytes] = field(default_factory=dict)  # container id prefix -> history
    available: bool = True
    self_id: str | None = None
    streams: dict[str, list[FakeStream]] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)  # tag -> image id
    removed: list[str] = field(default_factory=list)

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
