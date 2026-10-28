import hmac
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs

from starlette.responses import JSONResponse

from .config import AUTH_MAX_FAILURES, AUTH_WINDOW_SECONDS, MCP_API_KEY

_failures: dict[str, deque[float]] = defaultdict(deque)
_OPEN_PATHS = {"/health"}
# Bound on tracked client IPs, so spoofed or rotating addresses can't grow memory without limit.
_MAX_TRACKED_IPS = 10_000


def _client_ip(scope) -> str:
    # The app is only reachable through nginx-proxy. It overwrites X-Real-IP with the address it
    # saw, and only appends to X-Forwarded-For: the client controls everything but the last entry.
    headers = dict(scope.get("headers") or [])
    real = headers.get(b"x-real-ip", b"").decode().strip()
    forwarded = headers.get(b"x-forwarded-for", b"").decode().split(",")[-1].strip()
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
    headers = dict(scope.get("headers") or [])
    candidates = []
    auth = headers.get(b"authorization", b"").decode()
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    # Query-param auth is intentional: claude.ai connectors can only take a URL.
    query = parse_qs(scope.get("query_string", b"").decode())
    for name in ("api_key", "api-key"):
        candidates.extend(query.get(name, []))
    # Compare every candidate (no early exit) in constant time.
    ok = False
    for cand in candidates:
        ok |= hmac.compare_digest(cand.encode(), MCP_API_KEY.encode())
    return ok


class ApiKeyMiddleware:
    """Pure ASGI middleware (does not buffer streamed MCP responses)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in _OPEN_PATHS:
            return await self.app(scope, receive, send)

        ip = _client_ip(scope)
        if _blocked(ip):
            resp = JSONResponse({"error": "Too many failed attempts"}, status_code=429, headers={"Retry-After": str(AUTH_WINDOW_SECONDS)})
            return await resp(scope, receive, send)
        if not _valid(scope):
            _record_failure(ip)
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)
