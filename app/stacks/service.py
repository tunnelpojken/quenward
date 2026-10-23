"""Combining stack folders with the containers Docker reports."""

from dataclasses import dataclass
from typing import Any

from app.stacks.discovery import Stack, discover
from app.stacks.gateway import ContainerSummary, DockerGateway


class StackNotFound(Exception):
    pass


@dataclass(frozen=True)
class StackState:
    stack: Stack
    containers: tuple[ContainerSummary, ...]
    is_self: bool

    @property
    def running(self) -> int:
        return sum(1 for c in self.containers if c.state == "running")

    @property
    def status(self) -> str:
        total = len(self.containers)
        if total == 0:
            return "not_deployed"
        if self.running == 0:
            return "stopped"
        expected = max(len(self.stack.services), total)
        return "running" if self.running >= expected else "partial"

    def public(self) -> dict[str, Any]:
        return {
            "name": self.stack.name,
            "status": self.status,
            "running": self.running,
            "total": len(self.containers),
            "services": list(self.stack.services),
            "unhealthy": any(c.health == "unhealthy" for c in self.containers),
            "self": self.is_self,
            "compose_file": self.stack.compose_file,
            "error": self.stack.error,
        }


def _match(
    stacks: dict[str, Stack], containers: list[ContainerSummary]
) -> tuple[dict[str, list[ContainerSummary]], list[str]]:
    """Assign containers to stacks by the folder compose ran them from.

    Compose records that folder in a label. A container belongs to a stack only
    when it is exactly that stack's folder: a project with the same name started
    somewhere else is a different stack, not this one. This is why the stacks
    folder must be mounted at the same path as on the host.

    Returns the matches, plus stack names for which same-named projects were
    found in another folder (shown to the user as a hint about the mount).
    """
    by_path: dict[str, str] = {}
    for stack in stacks.values():
        by_path[str(stack.path)] = stack.name
        by_path[str(stack.path.resolve())] = stack.name
    by_project = {stack.project: stack.name for stack in stacks.values()}

    matched: dict[str, list[ContainerSummary]] = {name: [] for name in stacks}
    elsewhere: set[str] = set()
    for container in containers:
        name = by_path.get(container.working_dir or "")
        if name is not None:
            matched[name].append(container)
        elif container.project in by_project:
            elsewhere.add(by_project[container.project])
    for group in matched.values():
        group.sort(key=lambda c: (c.service or "", c.name))
    return matched, sorted(elsewhere)


def _self_stack(gateway: DockerGateway, matched: dict[str, list[ContainerSummary]]) -> str | None:
    self_id = gateway.self_container_id()
    if not self_id:
        return None
    for name, containers in matched.items():
        if any(c.id == self_id for c in containers):
            return name
    return None


@dataclass(frozen=True)
class Overview:
    stacks: list[StackState]
    started_elsewhere: list[str]  # stacks whose project name also runs from another folder


def overview(settings: Any, gateway: DockerGateway) -> Overview:
    stacks = discover(settings.stacks_dir)
    matched, elsewhere = _match(stacks, gateway.compose_containers())
    self_name = _self_stack(gateway, matched)
    return Overview(
        stacks=[
            StackState(stack=stacks[n], containers=tuple(matched[n]), is_self=n == self_name)
            for n in stacks
        ],
        started_elsewhere=elsewhere,
    )


def get_stack(settings: Any, gateway: DockerGateway, name: str) -> StackState:
    # The name is only compared against discovered folder names, never used as a path.
    for state in overview(settings, gateway).stacks:
        if state.stack.name == name:
            return state
    raise StackNotFound(name)
