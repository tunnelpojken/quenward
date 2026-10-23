"""Live log streaming for one or more containers.

History: the last N lines of every container, merged in timestamp order.
Follow: new lines as they arrive, read by one background thread per container.

Log lines are untrusted text (anything a container prints). They are cleaned of
terminal escape sequences and control characters and cut to a maximum length;
the browser shows them as plain text, never as HTML.
"""

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.stacks.gateway import DockerGateway, DockerUnavailable

log = logging.getLogger("app.logs")

# Memory bounds, sized for a small server: at most about 16 MB per open log view
# even when a container floods its output and the browser cannot keep up.
MAX_LINE = 8192
MAX_QUEUE = 2_000  # lines buffered per socket before new ones are dropped
MAX_HISTORY = 2_000  # lines sent when a view opens, across all containers

# CSI (colours, cursor moves), OSC (titles, hyperlinks) and other ESC sequences.
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z$")


def clean(text: str) -> str:
    text = _ESCAPES.sub("", text)
    text = _CONTROL.sub("", text.replace("\r", ""))
    return text if len(text) <= MAX_LINE else text[:MAX_LINE] + " …[truncated]"


def _parse(raw: str) -> tuple[str, str] | None:
    """Split 'TIMESTAMP message' into (sortable timestamp, message)."""
    ts, _, message = raw.partition(" ")
    match = _TIMESTAMP.match(ts)
    if not match:
        return None
    # Pad the fraction so that string order equals time order.
    return f"{match.group(1)}.{(match.group(2) or '').ljust(9, '0')}Z", message


@dataclass
class Line:
    ts: str
    source: str  # service or container name
    text: str

    def public(self) -> dict[str, str]:
        return {"t": self.ts, "s": self.source, "m": self.text}


def split_lines(data: bytes, source: str, pending: bytearray) -> list[Line]:
    """Turn a chunk of log output into whole lines; keeps an incomplete tail in `pending`."""
    pending.extend(data)
    *complete, rest = bytes(pending).split(b"\n")
    pending[:] = rest[-MAX_LINE * 4 :]  # a line with no newline cannot grow forever
    lines = []
    for raw in complete:
        parsed = _parse(raw.decode("utf-8", errors="replace"))
        if parsed:
            ts, message = parsed
            lines.append(Line(ts, source, clean(message)))
    return lines


@dataclass
class Target:
    container_id: str
    source: str


@dataclass
class LogFollower:
    """Streams logs of several containers into one asyncio queue."""

    gateway: DockerGateway
    targets: list[Target]
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Line] = field(default_factory=asyncio.Queue)
    dropped: int = 0
    _streams: list[Any] = field(default_factory=list)
    _threads: list[threading.Thread] = field(default_factory=list)
    _stopped: threading.Event = field(default_factory=threading.Event)

    def history(self, tail: int, until: float) -> list[Line]:
        lines: list[Line] = []
        for target in self.targets:
            pending = bytearray()
            data = self.gateway.history(target.container_id, tail, until)
            lines.extend(split_lines(data + b"\n", target.source, pending))
        lines.sort(key=lambda line: line.ts)
        return lines[-MAX_HISTORY:]

    def start(self, since: float) -> None:
        for target in self.targets:
            thread = threading.Thread(
                target=self._run, args=(target, since), name=f"logs-{target.source}", daemon=True
            )
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stopped.set()
        for stream in list(self._streams):
            with contextlib.suppress(Exception):  # already closed is fine
                stream.close()  # unblocks the reading thread

    def _push(self, lines: list[Line]) -> None:
        for line in lines:
            if self.queue.qsize() >= MAX_QUEUE:
                self.dropped += 1
            else:
                self.queue.put_nowait(line)

    def _run(self, target: Target, since: float) -> None:
        try:
            stream: Iterator[bytes] = self.gateway.follow(target.container_id, since)
        except (DockerUnavailable, Exception) as e:
            log.info("Log stream for %s not started: %s", target.source, type(e).__name__)
            return
        self._streams.append(stream)
        if self._stopped.is_set():
            self.stop()
        pending = bytearray()
        try:
            for chunk in stream:
                if self._stopped.is_set():
                    break
                lines = split_lines(chunk, target.source, pending)
                if lines:
                    self.loop.call_soon_threadsafe(self._push, lines)
        except Exception as e:
            if not self._stopped.is_set():
                log.info("Log stream for %s ended: %s", target.source, type(e).__name__)


def now() -> float:
    return time.time()
