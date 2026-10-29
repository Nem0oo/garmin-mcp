import hmac

from starlette.responses import JSONResponse

from .config import MCP_API_KEY

_OPEN_PATHS = {"/health"}


def _headers(scope) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers") or []}


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

        if not _valid(scope):
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)
