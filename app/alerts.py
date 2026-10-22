"""Security alerts to ntfy (failed logins, lockouts, password resets).

Alerts are sent from a background thread: a slow or unreachable ntfy server
never delays or breaks a login. They are also throttled, so a brute-force run
cannot flood your phone; after the limit you get one "suppressed" notice.

Alerts contain usernames and IP addresses, which is why they belong on their
own topic protected by an access token, not on an open topic like `diun`.
"""

import logging
import queue
import threading
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from app import clock
from app.config import NtfyConfig

log = logging.getLogger("app.alerts")


@dataclass(frozen=True)
class Alert:
    title: str
    message: str
    priority: str = "default"  # ntfy: min, low, default, high, urgent
    tags: tuple[str, ...] = field(default_factory=tuple)


class Alerter(Protocol):
    def send(self, alert: Alert) -> None: ...


class NullAlerter:
    """Used when ntfy is not configured."""

    def send(self, alert: Alert) -> None:
        pass


class NtfyAlerter:
    def __init__(
        self,
        config: NtfyConfig,
        max_per_window: int = 10,
        window: timedelta = timedelta(minutes=5),
    ) -> None:
        self._endpoint = f"{config.url}/{config.topic}"
        self._token = config.token_file.read_text(encoding="utf-8").strip()
        if not self._token:
            raise ValueError("The ntfy token file is empty")
        self._max = max_per_window
        self._window = window
        self._sent: deque[datetime] = deque()
        self._suppressed = False
        self._lock = threading.Lock()
        self._queue: queue.Queue[Alert] = queue.Queue(maxsize=100)
        self._thread = threading.Thread(target=self._run, name="ntfy-alerts", daemon=True)
        self._thread.start()

    def send(self, alert: Alert) -> None:
        now = clock.now()
        with self._lock:
            while self._sent and now - self._sent[0] > self._window:
                self._sent.popleft()
            if len(self._sent) >= self._max:
                if self._suppressed:
                    return
                self._suppressed = True
                alert = Alert(
                    "Security alerts suppressed",
                    "Too many security events in a short time. Further alerts are paused "
                    "for a few minutes; check the audit log.",
                    priority="high",
                    tags=("rotating_light",),
                )
            else:
                self._suppressed = False
            self._sent.append(now)
        try:
            self._queue.put_nowait(alert)
        except queue.Full:
            log.warning("ntfy alert queue full; dropping alert %r", alert.title)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued alerts are sent (used by the CLI and tests)."""
        done = threading.Event()

        def waiter() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=waiter, daemon=True).start()
        return done.wait(timeout)

    def _run(self) -> None:
        while True:
            alert = self._queue.get()
            try:
                self._post(alert)
            except Exception as e:  # never let the sender thread die
                log.warning("Could not send ntfy alert %r: %s", alert.title, type(e).__name__)
            finally:
                self._queue.task_done()

    def _post(self, alert: Alert) -> None:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Title": alert.title,
            "Priority": alert.priority,
        }
        if alert.tags:
            headers["Tags"] = ",".join(alert.tags)
        request = urllib.request.Request(  # noqa: S310 - scheme validated in config
            self._endpoint, data=alert.message.encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310  # nosec B310
            if response.status >= 300:
                raise RuntimeError(f"ntfy answered {response.status}")


def from_config(config: NtfyConfig | None) -> Alerter:
    return NtfyAlerter(config) if config else NullAlerter()
