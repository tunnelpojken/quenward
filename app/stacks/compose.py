"""Running `docker compose` for a stack.

- Always an argument list, never a shell: nothing in a stack name or path is ever
  interpreted as shell syntax.
- A clean environment: compose fills ${VARIABLES} in compose files from its own
  process environment *before* the stack's .env file. Run with the app's
  environment, a stack using ${PORT} would silently get the app's PORT. So only
  what the Docker CLI needs is passed on.
- A timeout that kills the whole process group, so a hung pull cannot hold the
  stack's lock forever.
"""

import contextlib
import os
import re
import shutil
import signal

# Argument lists only, never a shell: see run().
import subprocess  # nosec B404
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.stacks.discovery import Stack
from app.stacks.logs import clean

_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Passed through so a socket proxy, TLS or registry credentials keep working.
_PASS_THROUGH = ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG")
# The CLI wants a home directory. /tmp is the container's only writable place and
# holds nothing secret: the app writes no files there.
_HOME = "/tmp"  # noqa: S108  # nosec B108


class ComposeError(Exception):
    """Compose could not be run at all. The message is safe to show."""


@dataclass(frozen=True)
class Result:
    exit_code: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def docker_binary() -> str:
    path = shutil.which("docker")
    if path is None:
        raise ComposeError("The docker command is not installed in this container")
    return os.path.realpath(path)


def clean_environment() -> dict[str, str]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": _HOME, "LANG": "C.UTF-8"}
    for name in _PASS_THROUGH:
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def base_args(stack: Stack, project: str) -> list[str]:
    if not _PROJECT.fullmatch(project):
        raise ComposeError("The stack's compose project name is not valid")
    return [
        docker_binary(),
        "compose",
        "--ansi",
        "never",
        "--progress",
        "plain",
        "--project-directory",
        str(stack.path),
        "--file",
        str(stack.path / stack.compose_file),
        "--project-name",
        project,
    ]


def run(
    args: list[str],
    cwd: Path,
    on_line: Callable[[str], None],
    timeout: float,
) -> Result:
    """Run a command, sending each cleaned output line to on_line."""
    # A fixed argument list and no shell, so nothing in it is interpreted.
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        args,
        cwd=cwd,
        env=clean_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group, so a timeout can stop it all
    )
    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        with contextlib.suppress(ProcessLookupError):  # already finished
            os.killpg(process.pid, signal.SIGKILL)

    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        if process.stdout is None:  # cannot happen with stdout=PIPE; checked, not assumed
            raise ComposeError("Could not read the command's output")
        for raw in process.stdout:
            line = clean(raw.decode("utf-8", errors="replace").rstrip("\n"))
            if line.strip():
                on_line(line)
        exit_code = process.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        on_line(f"Stopped after {int(timeout)} seconds without finishing.")
    return Result(exit_code=exit_code, timed_out=timed_out.is_set())
