"""Settings, read once from environment variables at startup.

Every path must be absolute, so a typo in the compose file fails loudly at
startup instead of silently writing data somewhere unexpected.
"""

import os
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    stacks_dir: Path
    static_dir: Path
    port: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=_abs_path("DATA_DIR", "/data"),
            stacks_dir=_abs_path("STACKS_DIR", "/stacks"),
            static_dir=_abs_path("STATIC_DIR", str(_DEFAULT_STATIC_DIR)),
            port=_port("PORT", 5005),
        )
