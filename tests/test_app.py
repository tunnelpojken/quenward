from fastapi.testclient import TestClient

from app.security_headers import SECURITY_HEADERS


def test_healthz_says_only_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_security_headers_on_every_response(client: TestClient) -> None:
    for path in ["/healthz", "/", "/does-not-exist"]:
        response = client.get(path)
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, f"{name} missing on {path}"


def test_no_server_banner_or_api_docs(client: TestClient) -> None:
    assert "server" not in client.get("/healthz").headers
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert client.get(path).status_code == 404, path


def test_frontend_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>test</title>" in response.text


def test_static_files_cannot_escape_their_directory(client: TestClient) -> None:
    for path in ["/../data/app.db", "/%2e%2e/data/app.db", "/..%2fdata/app.db"]:
        assert client.get(path).status_code == 404, path
