from pathlib import Path

import pytest

from app.config import ConfigError, Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["DATA_DIR", "STACKS_DIR", "STATIC_DIR", "PORT"]:
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.data_dir == Path("/data")
    assert settings.stacks_dir == Path("/stacks")
    assert settings.port == 5005
    assert settings.db_path == Path("/data/app.db")


@pytest.mark.parametrize("name", ["DATA_DIR", "STACKS_DIR", "STATIC_DIR"])
def test_relative_paths_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "relative/path")
    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("value", ["abc", "0", "70000"])
def test_bad_ports_are_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PORT", value)
    with pytest.raises(ConfigError, match="PORT"):
        Settings.from_env()
