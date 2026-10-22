"""Brute-force protection for login and setup.

Failures are counted per username AND per client IP within a sliding window.
Crossing a limit locks that key for a while. Counting usernames that do not
exist the same way as real ones means a lockout never reveals which exist.
The state is in memory: an attacker cannot restart the container to reset it,
and a restart by the owner clearing locks is acceptable.
"""

import threading
from collections import deque
from datetime import datetime, timedelta

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
        for key in [k for k, f in self._failures.items() if not f or now - f[-1] > self.window]:
            del self._failures[key]
