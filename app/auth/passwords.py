"""Password hashing with Argon2id, via argon2-cffi. No homemade crypto.

Only the Argon2 hash string is ever stored. It contains its own random salt and
the cost parameters, so the parameters can be raised later and old hashes are
upgraded on the next successful login.
"""

import contextlib
import re
import threading
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Library defaults follow RFC 9106's recommended profile (Argon2id, 64 MiB, 3 passes).
_hasher = PasswordHasher()

# Each hash or check uses 64 MiB of memory for a moment. Only one runs at a time,
# so a burst of logins cannot use up the container's memory (256 MiB in the
# examples) and crash the app. One check takes well under a second; with one
# admin, nobody waits for another's.
MAX_PARALLEL = 1
_slots = threading.BoundedSemaphore(MAX_PARALLEL)


class Busy(Exception):
    """Too many password checks are running; try again in a moment."""


@contextlib.contextmanager
def _slot(wait: float) -> Iterator[None]:
    if not _slots.acquire(timeout=wait):
        raise Busy
    try:
        yield
    finally:
        _slots.release()


MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256  # an upper bound keeps hashing cost predictable
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class PolicyError(ValueError):
    """A username or password does not meet the rules. The message is safe to show."""


def validate_username(username: str) -> None:
    if not _USERNAME.fullmatch(username):
        raise PolicyError(
            "Username must be 1-64 characters: letters, digits, dot, dash or underscore"
        )


@cache
def _common_passwords() -> frozenset[str]:
    text = (Path(__file__).parent / "common_passwords.txt").read_text(encoding="utf-8")
    return frozenset(
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


def validate_password(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PolicyError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters")
    if username and username.lower() in password.lower():
        raise PolicyError("Password must not contain the username")
    if password.lower() in _common_passwords():
        raise PolicyError("This password is on a list of commonly used passwords")


def hash_password(password: str) -> str:
    with _slot(wait=10):
        return _hasher.hash(password)


@cache
def _dummy_hash() -> str:
    return _hasher.hash("dummy password used only to equalise login timing")


def verify_password(
    stored_hash: str | None, password: str, wait: float = 2.0
) -> tuple[bool, str | None]:
    with _slot(wait):
        return _verify(stored_hash, password)


def _verify(stored_hash: str | None, password: str) -> tuple[bool, str | None]:
    """Check a password. Returns (ok, upgraded_hash_or_None).

    When the user does not exist, pass stored_hash=None: a dummy hash is checked
    instead, so a wrong username takes as long as a wrong password and the
    response time does not reveal which usernames exist.
    """
    if stored_hash is None:
        with contextlib.suppress(VerificationError):
            _hasher.verify(_dummy_hash(), password)
        return False, None
    try:
        _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    if _hasher.check_needs_rehash(stored_hash):
        return True, _hasher.hash(password)
    return True, None
