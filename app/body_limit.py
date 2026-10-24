"""A cap on request body size, enforced before anything reads the body.

FastAPI reads and parses a JSON body before it runs the route's checks (origin,
login), so without a cap one request from anyone who can reach the port could
make the app hold hundreds of megabytes and crash it. The cap is checked
against Content-Length up front, and counted while the body streams in for
requests that do not send one (chunked).
"""

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The largest legitimate body is a compose file (at most 256 KiB), which JSON
# escaping can roughly double. 1 MiB leaves room and is harmless to hold.
MAX_BODY = 1024 * 1024

_TOO_LARGE = b'{"detail":"Request body too large"}'


class BodyLimitMiddleware:
    """Plain ASGI middleware (not BaseHTTPMiddleware) so it also works with streaming."""

    def __init__(self, app: ASGIApp, max_body: int = MAX_BODY) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    length = self.max_body + 1
                if length > self.max_body:
                    await _reject(send)
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    # Raised while the route reads its body; FastAPI passes an
                    # HTTPException through, so the client gets a 413.
                    raise HTTPException(413, "Request body too large")
            return message

        await self.app(scope, limited_receive, send)


async def _reject(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_TOO_LARGE)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _TOO_LARGE})
