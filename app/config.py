"""Settings, read once from environment variables at startup.

Every path must be absolute, so a typo in the compose file fails loudly at
startup instead of silently writing data somewhere unexpected.
"""

import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

_DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


def _abs_path(name: str, default: str) -> Path:
    path = Path(os.environ.get(name, default))
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path, got {str(path)!r}")
    return path


def _port(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        port = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535, got {port}")
    return port


def _origins(name: str, port: int) -> tuple[str, ...]:
    """Origins the browser UI may be served from, e.g. https://stacks.example.duckdns.org.

    State-changing requests whose Origin header is not in this list are refused.
    The default only covers direct local access during development.
    """
    raw = os.environ.get(name)
    if raw is None:
        return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
    origins = tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())
    if not origins:
        raise ConfigError(f"{name} must list at least one origin")
    for origin in origins:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(f"{name}: {origin!r} must look like https://host[:port]")
        if parts.path or parts.query or parts.fragment or "@" in parts.netloc:
            raise ConfigError(f"{name}: {origin!r} must not contain a path, query or user")
    return origins


@dataclass(frozen=True)
class NtfyConfig:
    url: str  # e.g. http://192.168.0.37:8085
    topic: str
    token_file: Path  # file holding the ntfy access token, never the token itself


_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _ntfy() -> NtfyConfig | None:
    """Security alerts are optional; when used, all three settings are required."""
    names = ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN_FILE")
    values = {n: os.environ.get(n, "").strip() for n in names}
    if not any(values.values()):
        return None
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise ConfigError(f"ntfy alerts need {', '.join(names)}; missing: {', '.join(missing)}")
    url = values["NTFY_URL"].rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or "@" in url:
        raise ConfigError("NTFY_URL must look like http(s)://host[:port][/path]")
    if not _TOPIC.fullmatch(values["NTFY_TOPIC"]):
        raise ConfigError("NTFY_TOPIC may only contain letters, digits, - and _ (max 64)")
    token_file = _abs_path("NTFY_TOKEN_FILE", "")
    if not token_file.is_file():
        raise ConfigError(f"NTFY_TOKEN_FILE {str(token_file)!r} does not exist or is not a file")
    return NtfyConfig(url=url, topic=values["NTFY_TOPIC"], token_file=token_file)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    stacks_dir: Path
    static_dir: Path
    port: int
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:5005", "http://localhost:5005")
    session_idle: timedelta = timedelta(minutes=30)
    session_absolute: timedelta = timedelta(hours=3)
    reset_link_lifetime: timedelta = timedelta(minutes=15)
    ws_session_check: timedelta = timedelta(seconds=5)
    ntfy: NtfyConfig | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def public_url(self) -> str:
        """The address the UI is reached at; used to build password reset links."""
        return self.allowed_origins[0]

    @classmethod
    def from_env(cls) -> "Settings":
        port = _port("PORT", 5005)
        return cls(
            data_dir=_abs_path("DATA_DIR", "/data"),
            stacks_dir=_abs_path("STACKS_DIR", "/stacks"),
            static_dir=_abs_path("STATIC_DIR", str(_DEFAULT_STATIC_DIR)),
            port=port,
            allowed_origins=_origins("ALLOWED_ORIGINS", port),
            ntfy=_ntfy(),
        )
