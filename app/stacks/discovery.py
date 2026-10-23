"""Finding stacks: one folder per stack in STACKS_DIR, each with a compose file.

User input (a stack name in a URL) is never turned into a path. It is only
looked up in the set of folders found here, so "../etc" and friends simply do
not exist as stacks.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Same order of preference as `docker compose` itself.
COMPOSE_FILE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
STACK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_COMPOSE_BYTES = 1024 * 1024
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class Stack:
    name: str  # the folder name
    path: Path  # absolute path of the folder
    compose_file: str  # which of COMPOSE_FILE_NAMES it uses
    project: str  # compose project name (folder name normalised, or the file's `name:`)
    services: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None  # set when the compose file could not be read


def normalise_project_name(name: str) -> str:
    """What `docker compose` uses as project name for a folder name."""
    return re.sub(r"[^a-z0-9_-]", "", name.lower()).lstrip("_-")


def _read_compose(path: Path) -> tuple[str | None, tuple[str, ...], str | None]:
    """Return (project name from `name:`, service names, error). Never returns content."""
    try:
        if path.stat().st_size > _MAX_COMPOSE_BYTES:
            return None, (), "Compose file is larger than 1 MiB"
        # safe_load builds plain data only: no Python objects, no code execution.
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None, (), "Compose file could not be read"
    except yaml.YAMLError:
        return None, (), "Compose file is not valid YAML"
    if data is None:
        return None, (), "Compose file is empty"
    if not isinstance(data, dict):
        return None, (), "Compose file does not contain a mapping"

    name = data.get("name")
    name = normalise_project_name(name) if isinstance(name, str) and name.strip() else None
    services = data.get("services")
    if services is None:
        return name, (), None
    if not isinstance(services, dict):
        return name, (), "`services` is not a mapping"
    names = tuple(sorted(s for s in services if isinstance(s, str) and _SERVICE_NAME.match(s)))
    return name, names, None


def discover(stacks_dir: Path) -> dict[str, Stack]:
    """All stacks in stacks_dir, by folder name.

    Symlinked folders and compose files are skipped on purpose: a link could
    point anywhere on the host, and later phases write to these folders.
    """
    stacks: dict[str, Stack] = {}
    try:
        entries = sorted(stacks_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return stacks
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or not STACK_NAME.match(entry.name):
            continue
        compose = next(
            (
                entry / n
                for n in COMPOSE_FILE_NAMES
                if (entry / n).is_file() and not (entry / n).is_symlink()
            ),
            None,
        )
        if compose is None:
            continue
        name_override, services, error = _read_compose(compose)
        stacks[entry.name] = Stack(
            name=entry.name,
            path=entry,
            compose_file=compose.name,
            project=name_override or normalise_project_name(entry.name),
            services=services,
            error=error,
        )
    return stacks
