"""Security headers added to every response.

The Content-Security-Policy only allows scripts, styles and connections from
our own origin, so injected inline scripts will not run even if a bug lets
someone put HTML on the page.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}

_ENCODED = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]


class SecurityHeadersMiddleware:
    """Plain ASGI middleware (not BaseHTTPMiddleware) so it also works with streaming."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        extra = _ENCODED
        if scope["path"].startswith("/api/"):
            # API answers (session info, CSRF tokens) must never be cached anywhere.
            extra = _ENCODED + [(b"cache-control", b"no-store")]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in extra}
                headers = [h for h in message.get("headers", []) if h[0].lower() not in names]
                message["headers"] = headers + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)
