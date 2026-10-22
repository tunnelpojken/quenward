from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.config import Settings
from app.main import create_app

ORIGIN = "https://testserver"
USERNAME = "fested"
PASSWORD = "correct-horse-battery-staple-42"


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
        allowed_origins=(ORIGIN,),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    # https base URL so the Secure cookie is sent back, like a real browser over TLS.
    # The context manager runs the lifespan, so migrations happen like in production.
    with TestClient(create_app(settings), base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> tuple[str, str]:
    """An app where setup has been completed."""
    response = client.post(
        "/api/setup",
        json={"token": client.app.state.setup_token, "username": USERNAME, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return USERNAME, PASSWORD


@pytest.fixture
def csrf(client: TestClient, admin: tuple[str, str]) -> str:
    """Log in; the client now holds the session cookie. Returns the CSRF token."""
    response = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


class FakeClock:
    def __init__(self) -> None:
        self.current = clock.now()

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(clock, "now", fake)
    return fake
