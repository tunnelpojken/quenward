"""Connects one browser terminal to one shell inside a container.

- The shell's first output is a marker with its name and pid, removed here so the
  browser never sees it. The pid is what lets the shell be ended afterwards:
  closing the connection alone leaves a Docker exec running.
- Output is read by one thread and handed to the event loop. When the browser
  falls behind, the thread pauses (backpressure) instead of buffering without
  limit or dropping output, which would corrupt the screen.
"""

import asyncio
import contextlib
import logging
import re
import socket
import threading
from dataclasses import dataclass

from app.stacks.gateway import DockerGateway, DockerUnavailable, ShellHandle

log = logging.getLogger("app.terminal")

MAX_PENDING = 1 << 20  # bytes of output waiting for the browser before reading pauses
_READ_SIZE = 32 * 1024
_PROBE = re.compile(rb"\x1e(bash|sh) (\d+)\x1e")
_PROBE_LIMIT = 4096  # anything before the marker is at most an error message
_PROBE_TIMEOUT = 5.0


class ShellUnavailable(Exception):
    """The container has no usable shell. The message is safe to show."""


@dataclass(frozen=True)
class Started:
    shell_name: str
    pid: int
    first_output: bytes


def read_probe(sock: socket.socket) -> Started:
    """Read until the marker; whatever follows it is the shell's first real output."""
    sock.settimeout(_PROBE_TIMEOUT)
    buffer = b""
    try:
        while len(buffer) < _PROBE_LIMIT:
            chunk = sock.recv(_READ_SIZE)
            if not chunk:
                break
            buffer += chunk
            match = _PROBE.search(buffer)
            if match:
                return Started(
                    shell_name=match.group(1).decode(),
                    pid=int(match.group(2)),
                    first_output=buffer[match.end() :],
                )
    except TimeoutError:
        pass
    raise ShellUnavailable("This container has no shell (sh or bash) to open a terminal with")


class Bridge:
    def __init__(
        self, gateway: DockerGateway, container_id: str, loop: asyncio.AbstractEventLoop
    ) -> None:
        self.gateway = gateway
        self.container_id = container_id
        self.loop = loop
        self.output: asyncio.Queue[bytes | None] = asyncio.Queue()  # None: shell ended
        self.handle: ShellHandle | None = None
        self.started: Started | None = None
        self._pending = 0
        self._room = threading.Condition()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self._ended = threading.Lock()
        self._exit_code: int | None = None
        self._finished = False

    # -- called in a worker thread (blocking) --------------------------------

    def start(self, rows: int, cols: int) -> Started:
        self.handle = self.gateway.open_shell(self.container_id)
        try:
            self.started = read_probe(self.handle.sock)
        except Exception:
            self._close_socket()
            raise
        self.handle.sock.settimeout(None)
        self.resize(rows, cols)
        self._reader = threading.Thread(target=self._read, name="terminal-reader", daemon=True)
        self._reader.start()
        return self.started

    def write(self, data: bytes) -> None:
        if self.handle is not None and not self._closed.is_set():
            self.handle.sock.sendall(data)

    def resize(self, rows: int, cols: int) -> None:
        if self.handle is None:
            return
        try:
            self.gateway.resize_shell(self.handle.exec_id, rows, cols)
        except (DockerUnavailable, Exception) as e:  # a lost resize is not worth a failure
            log.info("Terminal resize failed: %s", type(e).__name__)

    def close(self) -> int | None:
        """End the shell and everything started from it. Returns its exit code if known.

        Safe to call more than once (the shell ending and the socket closing can
        both trigger it): the work is done the first time.
        """
        with self._ended:
            if not self._finished:
                self._exit_code = self._close()
                self._finished = True
            return self._exit_code

    def _close(self) -> int | None:
        self._closed.set()
        with self._room:
            self._room.notify_all()
        self._close_socket()
        if self.handle is None or self.started is None:
            return None
        try:
            self.gateway.end_shell_session(self.container_id, self.started.pid)
        except Exception as e:  # the container may be gone already; nothing left to end
            log.info("Ending the terminal's shell failed: %s", type(e).__name__)
        try:
            return self.gateway.shell_exit_code(self.handle.exec_id)
        except Exception:
            return None

    # -- the reading thread --------------------------------------------------

    def _read(self) -> None:
        try:
            if self.handle is None:  # set before the thread starts; checked, not assumed
                return
            sock = self.handle.sock
            while not self._closed.is_set():
                data = sock.recv(_READ_SIZE)
                if not data:
                    break
                with self._room:
                    self._pending += len(data)
                self.loop.call_soon_threadsafe(self.output.put_nowait, data)
                with self._room:
                    while self._pending > MAX_PENDING and not self._closed.is_set():
                        self._room.wait(0.5)
        except OSError:
            pass  # the socket was closed: the session is ending
        finally:
            with contextlib.suppress(RuntimeError):  # the event loop already stopped
                self.loop.call_soon_threadsafe(self.output.put_nowait, None)

    # -- called on the event loop ----------------------------------------------

    def consumed(self, size: int) -> None:
        """The browser was sent `size` bytes; let the reader continue."""
        with self._room:
            self._pending -= size
            self._room.notify_all()

    def _close_socket(self) -> None:
        if self.handle is None:
            return
        with contextlib.suppress(OSError):
            self.handle.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.handle.sock.close()
