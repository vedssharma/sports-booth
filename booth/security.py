"""
Access control for the dashboard, WebSocket and operational endpoints.

By default the booth listens on loopback only and needs no credentials. When BOOTH_AUTH_TOKEN is
set (required for any non-loopback bind), everything except /healthz needs it, supplied as:
  - `Authorization: Bearer <token>` (scripts, monitoring)
  - a session cookie (browsers). Open `/?token=<token>` once: the token is exchanged for an
    HttpOnly, SameSite=Strict cookie and stripped from the URL by a redirect, so it doesn't
    linger in history or Referer headers. WebSockets can't set headers, so they use the cookie.
Separately, WebSocket handshakes from a browser must come from the page's own origin (or a
configured allow-list); this stops other websites from connecting to a booth on localhost.
"""
import hashlib
import hmac
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection

COOKIE_NAME = "booth_session"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The dashboard is one self-contained page with inline script/style plus two CDN libraries
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
}


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class Auth:
    def __init__(self, token: str | None, allowed_origins: tuple[str, ...] = ()) -> None:
        self.token = token
        self.allowed_origins = tuple(o.lower() for o in allowed_origins)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def session_value(self) -> str:
        """What the cookie holds: a keyed digest, so the cookie is not the raw token."""
        return hmac.new(self.token.encode(), b"booth-session-v1", hashlib.sha256).hexdigest()

    def token_matches(self, candidate: str | None) -> bool:
        return bool(self.token) and candidate is not None and _eq(candidate, self.token)

    def is_authorized(self, conn: HTTPConnection) -> bool:
        """True when no auth is configured, or the connection carries a valid bearer token or cookie."""
        if not self.enabled:
            return True
        header = conn.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and self.token_matches(header[7:].strip()):
            return True
        cookie = conn.cookies.get(COOKIE_NAME)
        return cookie is not None and _eq(cookie, self.session_value())

    def origin_allowed(self, conn: HTTPConnection) -> bool:
        """Browsers always send Origin on WebSocket handshakes; it must match the page's host."""
        origin = conn.headers.get("origin")
        if origin is None:
            return True            # not a browser (scripts, curl): token auth is the control
        if origin.rstrip("/").lower() in self.allowed_origins:
            return True
        host = conn.headers.get("host", "").lower()
        return bool(host) and urlsplit(origin).netloc.lower() == host
