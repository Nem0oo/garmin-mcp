import hmac
import time
from collections import defaultdict, deque

from starlette.responses import JSONResponse

from .config import AUTH_MAX_FAILURES, AUTH_WINDOW_SECONDS, MCP_API_KEY

_failures: dict[str, deque[float]] = defaultdict(deque)
_OPEN_PATHS = {"/health"}
# Bound on tracked client IPs, so spoofed or rotating addresses can't grow memory without limit.
_MAX_TRACKED_IPS = 10_000


def _headers(scope) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers") or []}


def _client_ip(scope) -> str:
    # The app is only reachable through nginx-proxy. It overwrites X-Real-IP with the address it
    # saw, and only appends to X-Forwarded-For: the client controls everything but the last entry.
    headers = _headers(scope)
    # Header bytes are latin-1 per the HTTP/ASGI specs; decoding as UTF-8 could raise on arbitrary input.
    real = headers.get(b"x-real-ip", b"").decode("latin-1").strip()
    forwarded = headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",")[-1].strip()
    return real or forwarded or (scope.get("client") or ("unknown", 0))[0]


def _record_failure(ip: str) -> None:
    if ip not in _failures and len(_failures) >= _MAX_TRACKED_IPS:
        _failures.pop(next(iter(_failures)))  # dicts keep insertion order: evict the oldest
    _failures[ip].append(time.monotonic())


def _blocked(ip: str) -> bool:
    window = _failures[ip]
    cutoff = time.monotonic() - AUTH_WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    if not window:
        _failures.pop(ip, None)
    return len(window) >= AUTH_MAX_FAILURES


def _valid(scope) -> bool:
    # Only the Authorization header is accepted: a key in the URL ends up in proxy logs and history.
    # Compared as raw bytes in constant time, so any key (even non-ASCII, sent as UTF-8) matches exactly.
    auth = _headers(scope).get(b"authorization", b"")
    if auth[:7].lower() != b"bearer ":
        return False
    return hmac.compare_digest(auth[7:].strip(), MCP_API_KEY.encode())


class ApiKeyMiddleware:
    """Pure ASGI middleware (does not buffer streamed MCP responses)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan" or (scope["type"] == "http" and scope["path"] in _OPEN_PATHS):
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            # No websocket endpoint here: refuse anything else instead of passing it through unauthenticated.
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            return

        ip = _client_ip(scope)
        if _blocked(ip):
            resp = JSONResponse({"error": "Too many failed attempts"}, status_code=429, headers={"Retry-After": str(AUTH_WINDOW_SECONDS)})
            return await resp(scope, receive, send)
        if not _valid(scope):
            _record_failure(ip)
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)
