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
    def expected(self) -> int:
        """Containers a fully running stack has: every service without a profile,
        plus any optional (profile) service that was started anyway."""
        required = [s for s in self.stack.services if s not in self.stack.optional]
        return max(len(required), len(self.containers))

    @property
    def status(self) -> str:
        if not self.containers:
            return "not_deployed"
        if self.running == 0:
            return "stopped"
        return "running" if self.running >= self.expected else "partial"

    def public(self) -> dict[str, Any]:
        return {
            "name": self.stack.name,
            "status": self.status,
            "running": self.running,
            "total": len(self.containers),
            "expected": self.expected,
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


def _self_project(gateway: DockerGateway, containers: list[ContainerSummary]) -> str | None:
    """The compose project the app's own container belongs to."""
    self_id = gateway.self_container_id()
    if not self_id:
        return None
    for c in containers:
        if c.id == self_id:
            return c.project
    return None


def app_project(gateway: DockerGateway) -> str | None:
    """The compose project of the app's own container, if it runs under compose."""
    return _self_project(gateway, gateway.compose_containers())


def compose_project(stack: Stack, containers: tuple[ContainerSummary, ...]) -> str:
    """The project name actions run with: the one the containers were created
    with if there are any (and they agree), otherwise the compose file's."""
    projects = {c.project for c in containers if c.project}
    return projects.pop() if len(projects) == 1 else stack.project


@dataclass(frozen=True)
class Overview:
    stacks: list[StackState]
    started_elsewhere: list[str]  # stacks whose project name also runs from another folder


def overview(settings: Any, gateway: DockerGateway) -> Overview:
    stacks = discover(settings.stacks_dir)
    containers = gateway.compose_containers()
    matched, elsewhere = _match(stacks, containers)
    self_name = _self_stack(gateway, matched)
    self_project = _self_project(gateway, containers)
    states = []
    for n in stacks:
        mine = tuple(matched[n])
        # Read-only: the app's own stack, and any other stack that would run
        # compose with the app's project name (a copied compose file, or a
        # `name:` set to it), which would stop or recreate the app itself.
        is_self = n == self_name or (
            self_project is not None and compose_project(stacks[n], mine) == self_project
        )
        states.append(StackState(stack=stacks[n], containers=mine, is_self=is_self))
    return Overview(stacks=states, started_elsewhere=elsewhere)


def get_stack(settings: Any, gateway: DockerGateway, name: str) -> StackState:
    # The name is only compared against discovered folder names, never used as a path.
    for state in overview(settings, gateway).stacks:
        if state.stack.name == name:
            return state
    raise StackNotFound(name)
