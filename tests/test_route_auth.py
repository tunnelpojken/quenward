"""The safety net for "a new route forgot to require login".

Every route the app exposes must either require a logged-in user or be on the
PUBLIC list below. Adding a route without auth makes this file fail, so it can
never be merged by accident.
"""

import re
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, RouteContext, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from app.auth.deps import check_origin, require_user, require_user_ws
from tests.conftest import ORIGIN

# Keep this list as short as possible. Each entry needs a reason.
PUBLIC = {
    ("GET", "/healthz"),  # Docker healthcheck, answers only "ok"
    ("GET", "/api/setup/status"),  # tells the UI whether to show setup or login
    ("POST", "/api/setup"),  # protected by the one-time setup token
    ("POST", "/api/login"),  # the way in
    ("POST", "/api/password-reset"),  # protected by the one-time link from the CLI
}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _all_calls(dependant: Dependant) -> Iterator[object]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _all_calls(sub)


def _routes(app: FastAPI) -> Iterator[tuple[str, str, RouteContext]]:
    """Every (method, path) the app answers, with routers already merged in.

    iter_route_contexts gives each route as FastAPI actually serves it, including
    the dependencies inherited from its router (that is where require_user sits).
    WebSocket routes are reported with the method "WS".
    """
    for ctx in iter_route_contexts(app.routes):
        original = ctx.original_route
        if isinstance(original, Mount) and original.name == "frontend":
            continue  # static files: no data, no actions
        if isinstance(original, APIWebSocketRoute):
            yield "WS", ctx.path, ctx
            continue
        assert isinstance(original, APIRoute), (
            f"Unexpected route type {type(original).__name__} at {ctx.path}: "
            "raw Starlette routes need their own auth check and a test here"
        )
        for method in sorted((ctx.methods or set()) - {"HEAD"}):
            yield method, ctx.path, ctx


def test_every_route_is_protected_or_explicitly_public(client: TestClient) -> None:
    for method, path, route in _routes(client.app):
        calls = set(_all_calls(route.dependant))
        if (method, path) in PUBLIC:
            if method in UNSAFE:
                assert check_origin in calls, f"public {method} {path} lacks the Origin check"
            continue
        if method == "WS":
            assert require_user_ws in calls, f"WebSocket {path} does not require login"
        else:
            assert require_user in calls, f"{method} {path} does not require login"


def test_public_list_has_no_stale_entries(client: TestClient) -> None:
    existing = {(m, p) for m, p, _ in _routes(client.app)}
    assert existing >= PUBLIC, f"PUBLIC lists routes that no longer exist: {PUBLIC - existing}"


def test_protected_routes_answer_401_without_a_session(client: TestClient) -> None:
    """Behavioural check on top of the static one: actually call every route."""
    checked = 0
    for method, path, _ in _routes(client.app):
        if (method, path) in PUBLIC or method == "WS":
            continue
        url = re.sub(r"\{[^}]+\}", "x", path)
        response = client.request(method, url)
        assert response.status_code == 401, f"{method} {url} -> {response.status_code}"
        checked += 1
    assert checked > 0


POLICY_VIOLATION = 1008  # the close code require_user_ws uses


def _ws_close_code(client: TestClient, url: str, headers: dict[str, str]) -> int | None:
    """Close code the handshake ended with, or None if the socket was accepted.

    wss:// on purpose: the test client defaults to ws://, over which the browser
    (and httpx) correctly withholds the Secure session cookie.
    """
    try:
        with client.websocket_connect(f"wss://testserver{url}", headers=headers):
            return None
    except WebSocketDisconnect as e:
        return e.code


def test_websockets_refuse_unauthenticated_and_foreign_connections(
    client: TestClient, csrf: str
) -> None:
    """Every WebSocket must refuse a missing session and a foreign Origin at the handshake."""
    session_cookie = client.cookies.get("__Host-session")
    checked = 0
    for method, path, _ in _routes(client.app):
        if method != "WS":
            continue
        url = re.sub(r"\{[^}]+\}", "x", path)
        # Positive control: logged in, same origin. It may fail for other reasons
        # (stack "x" does not exist), but never with the auth close code. Without
        # this, the checks below could pass for the wrong reason.
        assert _ws_close_code(client, url, {"Origin": ORIGIN}) != POLICY_VIOLATION, url
        # Logged in, but the page is on another website: cross-site WebSocket hijacking.
        assert _ws_close_code(client, url, {"Origin": "https://evil.example"}) == POLICY_VIOLATION
        # No usable Origin header.
        assert _ws_close_code(client, url, {"Origin": ""}) == POLICY_VIOLATION
        # Right origin, no session.
        client.cookies.clear()
        assert _ws_close_code(client, url, {"Origin": ORIGIN}) == POLICY_VIOLATION
        client.cookies.set("__Host-session", session_cookie)
        checked += 1
    assert checked > 0
