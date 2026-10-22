from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>test</title>")
    return Settings(
        data_dir=tmp_path / "data",
        stacks_dir=tmp_path / "stacks",
        static_dir=static,
        port=5005,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    # The context manager runs the lifespan, so migrations happen like in production.
    with TestClient(create_app(settings)) as c:
        yield c
