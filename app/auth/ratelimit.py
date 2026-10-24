"""Brute-force protection for login and setup.

Failures are counted per username AND per client IP within a sliding window.
Crossing a limit locks that key for a while. Counting usernames that do not
exist the same way as real ones means a lockout never reveals which exist.
The state is in memory, so a restart clears it. That is why nothing reachable
without logging in may be able to crash the app (see body_limit.py and the
limit on parallel password checks in passwords.py).

A login attempt is reserved with begin() BEFORE the password is checked, and
released with end() after. Attempts still being checked count towards the
limit, so a burst of parallel requests cannot all slip past it while the slow
password check runs.
"""

import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Literal

from app import clock

_MAX_TRACKED_KEYS = 10_000


class LoginLimiter:
    def __init__(
        self,
        max_per_user: int = 5,
        max_per_ip: int = 20,
        window: timedelta = timedelta(minutes=15),
        lockout: timedelta = timedelta(minutes=15),
    ) -> None:
        self.max_per_user = max_per_user
        self.max_per_ip = max_per_ip
        self.window = window
        self.lockout = lockout
        self._failures: dict[str, deque[datetime]] = {}
        self._locked_until: dict[str, datetime] = {}
        self._in_flight: dict[str, int] = {}
        self._reported: dict[str, datetime] = {}  # key -> the lock it was reported for
        self._lock = threading.Lock()

    @staticmethod
    def _keys(username: str | None, ip: str | None) -> list[tuple[str, str]]:
        keys = [("ip", f"ip:{ip or 'unknown'}")]
        if username is not None:
            keys.append(("user", f"user:{username.lower()}"))
        return keys

    def is_blocked(self, username: str | None, ip: str | None) -> bool:
        now = clock.now()
        with self._lock:
            for _, key in self._keys(username, ip):
                until = self._locked_until.get(key)
                if until and until > now:
                    return True
            return False

    def begin(
        self, username: str | None, ip: str | None
    ) -> Literal["ok", "locked_first", "locked", "busy"]:
        """Reserve an attempt. Only "ok" may go on to check the password, and must
        then call end() with the same arguments.

        "locked_first" is the first refusal during a lock (worth recording once),
        "locked" any later one, "busy" too many attempts already being checked.
        """
        now = clock.now()
        keys = self._keys(username, ip)
        with self._lock:
            refusal: Literal["locked_first", "locked"] | None = None
            for _, key in keys:
                until = self._locked_until.get(key)
                if until and until > now:
                    refusal = refusal or "locked"
                    if self._reported.get(key) != until:
                        self._reported[key] = until
                        refusal = "locked_first"
            if refusal:
                return refusal
            for kind, key in keys:
                limit = self.max_per_user if kind == "user" else self.max_per_ip
                recent = sum(1 for t in self._failures.get(key, ()) if now - t <= self.window)
                if recent + self._in_flight.get(key, 0) >= limit:
                    return "busy"
            for _, key in keys:
                self._in_flight[key] = self._in_flight.get(key, 0) + 1
            return "ok"

    def end(self, username: str | None, ip: str | None) -> None:
        with self._lock:
            for _, key in self._keys(username, ip):
                left = self._in_flight.get(key, 0) - 1
                if left > 0:
                    self._in_flight[key] = left
                else:
                    self._in_flight.pop(key, None)

    def record_failure(self, username: str | None, ip: str | None) -> list[str]:
        """Count a failure. Returns the kinds ('user', 'ip') that just became locked."""
        now = clock.now()
        newly_locked: list[str] = []
        with self._lock:
            self._prune(now)
            for kind, key in self._keys(username, ip):
                failures = self._failures.setdefault(key, deque())
                failures.append(now)
                while failures and now - failures[0] > self.window:
                    failures.popleft()
                limit = self.max_per_user if kind == "user" else self.max_per_ip
                already = key in self._locked_until and self._locked_until[key] > now
                if len(failures) >= limit and not already:
                    self._locked_until[key] = now + self.lockout
                    failures.clear()
                    newly_locked.append(kind)
        return newly_locked

    def record_success(self, username: str) -> None:
        with self._lock:
            self._failures.pop(f"user:{username.lower()}", None)

    def _prune(self, now: datetime) -> None:
        if len(self._failures) + len(self._locked_until) < _MAX_TRACKED_KEYS:
            return
        for key in [k for k, until in self._locked_until.items() if until <= now]:
            del self._locked_until[key]
        for key in [k for k, until in self._reported.items() if until <= now]:
            del self._reported[key]
        for key in [k for k, f in self._failures.items() if not f or now - f[-1] > self.window]:
            del self._failures[key]
