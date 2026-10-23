"""Reading and saving a stack's compose file.

- Only the stack's own compose file, found by discovery: user input never
  becomes a path. Neither the folder nor the file may be a symlink, and the
  file must sit directly in the stack's folder inside STACKS_DIR. `.env` files
  are never read or written.
- Saving checks, in order: the file did not change since it was opened (an
  edit over SSH meanwhile is never overwritten), the text is YAML with a
  `services` mapping, and `docker compose config` accepts it. That last check
  runs on a temporary copy in the stack's folder, so relative paths and the
  stack's .env resolve exactly as they will when the stack is started.
- The previous version is kept as <file>.bak (one generation). The file is
  then rewritten in place, so it keeps its owner and permissions: replacing
  it would make it owned by the app's user, and the app cannot chown.
"""

import contextlib
import difflib
import hashlib
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.stacks import compose
from app.stacks.discovery import Stack

MAX_COMPOSE = 256 * 1024
_CHECK_TIMEOUT = 30
_MAX_OUTPUT_LINES = 40


class EditRefused(Exception):
    """The file cannot be read or saved. The message is safe to show."""

    def __init__(self, message: str, status: int = 422, output: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.output = output or []


@dataclass(frozen=True)
class ComposeFile:
    name: str
    content: str
    etag: str
    writable: bool


@dataclass(frozen=True)
class Saved:
    content: str  # exactly what was written (a final newline may have been added)
    etag: str
    old_etag: str
    added: int
    removed: int
    warnings: list[str]


def etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compose_path(stacks_dir: Path, stack: Stack) -> Path:
    """The stack's compose file, after checking it is where it must be."""
    folder = stacks_dir / stack.name
    path = folder / stack.compose_file
    if folder.is_symlink() or path.is_symlink():
        raise EditRefused("The compose file is a symlink, which is never edited", 403)
    if path.resolve().parent != stacks_dir.resolve() / stack.name:
        raise EditRefused("The compose file is outside the stacks folder", 403)
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise EditRefused("The compose file no longer exists", 404) from None
    if not stat.S_ISREG(mode):
        raise EditRefused("The compose file is not a regular file", 403)
    return path


def _read_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as f:
            data = f.read(MAX_COMPOSE + 1)
    except OSError:
        raise EditRefused("The compose file could not be read", 500) from None
    if len(data) > MAX_COMPOSE:
        raise EditRefused("The compose file is larger than 256 KiB", 413)
    return data


def read(stacks_dir: Path, stack: Stack) -> ComposeFile:
    path = compose_path(stacks_dir, stack)
    data = _read_bytes(path)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise EditRefused("The compose file is not UTF-8 text", 415) from None
    writable = os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)
    return ComposeFile(name=stack.compose_file, content=content, etag=etag(data), writable=writable)


def check_yaml(text: str) -> None:
    """The cheap check first, with a line number that points at the mistake."""
    if "\x00" in text:
        raise EditRefused("The text contains a NUL character")
    try:
        data = yaml.safe_load(text)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise EditRefused(f"Not valid YAML{where}: {e.problem or e.context}") from None
    except yaml.YAMLError:
        raise EditRefused("Not valid YAML") from None
    if not isinstance(data, dict):
        raise EditRefused("A compose file must be a mapping (key: value)")
    if not isinstance(data.get("services"), dict) or not data["services"]:
        raise EditRefused("A compose file needs a `services:` section with at least one service")


def _private_file(path: Path, data: bytes, mode: int) -> None:
    """Create a new file (never follow or reuse an existing one) and write data.

    The mode is set explicitly: the app's umask (077) would otherwise make a
    .bak that the owner of the stack cannot read over SSH.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def check_compose(
    stack: Stack, path: Path, data: bytes, runner: Callable[..., compose.Result]
) -> list[str]:
    """Run `docker compose config` on the new text. Returns its warnings."""
    check = path.parent / f".{path.name}.check-{secrets.token_hex(4)}"
    output: list[str] = []
    try:
        _private_file(check, data, 0o600)
    except OSError:
        raise EditRefused(
            "The app cannot write in this stack's folder. See 'Compose editor' in the README.",
            403,
        ) from None
    try:
        args = [
            compose.docker_binary(),
            "compose",
            "--ansi",
            "never",
            "--project-directory",
            str(path.parent),
            "--file",
            str(check),
            "--project-name",
            stack.project or "stack",
            "config",
            "--quiet",
        ]
        result = runner(args, cwd=path.parent, on_line=output.append, timeout=_CHECK_TIMEOUT)
    except compose.ComposeError as e:
        raise EditRefused(str(e), 500) from None
    finally:
        with contextlib.suppress(OSError):
            check.unlink()
    # Compose names the temporary file in its messages; show the real name.
    output = [line.replace(str(check), path.name).replace(check.name, path.name) for line in output]
    output = output[-_MAX_OUTPUT_LINES:]
    if not result.ok:
        message = "docker compose did not accept the file"
        if result.timed_out:
            message = "Checking the file took too long"
        raise EditRefused(message, 422, output)
    return output


def _write_in_place(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _backup(path: Path, data: bytes, mode: int) -> None:
    """<file>.bak holds the previous version (one generation).

    Written to a new file and renamed over the old .bak: the rename is atomic,
    replaces a planted symlink instead of following it, and works whoever owns
    the old .bak. Readable like the original, but never writable by group or
    others: the app's group is not the stack owner's group.
    """
    bak = path.with_name(path.name + ".bak")
    if bak.is_dir() and not bak.is_symlink():
        raise EditRefused(f"{bak.name} is a folder", 403)
    tmp = path.with_name(f".{path.name}.bak-{secrets.token_hex(4)}")
    _private_file(tmp, data, mode & ~0o022)
    try:
        os.replace(tmp, bak)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def save(
    stacks_dir: Path,
    stack: Stack,
    text: str,
    expected_etag: str,
    runner: Callable[..., compose.Result],
) -> Saved:
    path = compose_path(stacks_dir, stack)
    if text and not text.endswith("\n"):
        text += "\n"  # text files end with a newline; `echo x >> file` relies on it
    data = text.encode("utf-8")
    if len(data) > MAX_COMPOSE:
        raise EditRefused("The compose file may be at most 256 KiB", 413)
    current = _read_bytes(path)
    if etag(current) != expected_etag:
        raise EditRefused(
            "The file was changed on disk since you opened it. Reload to see the new version.",
            409,
        )
    check_yaml(text)
    warnings = check_compose(stack, path, data, runner)
    if etag(_read_bytes(path)) != expected_etag:  # changed while compose was checking
        raise EditRefused("The file was changed on disk meanwhile. Reload and try again.", 409)

    mode = stat.S_IMODE(path.stat().st_mode)
    try:
        _backup(path, current, mode)
        _write_in_place(path, data)
    except PermissionError:
        raise EditRefused(
            "The app may not write this file. See 'Compose editor' in the README.", 403
        ) from None
    except OSError:
        raise EditRefused("The file could not be written", 500) from None

    diff = list(
        difflib.unified_diff(
            current.decode("utf-8", errors="replace").splitlines(), text.splitlines(), lineterm=""
        )
    )
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    return Saved(
        content=text,
        etag=etag(data),
        old_etag=expected_etag,
        added=added,
        removed=removed,
        warnings=warnings,
    )
