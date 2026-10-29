import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from garmin_mcp import security
from garmin_mcp.config import AUTH_MAX_FAILURES, AUTH_WINDOW_SECONDS

from .conftest import API_KEY


def _scope(headers=None, query=b"", path="/mcp", client=("10.0.0.1", 1234)):
    return {
        "type": "http",
        "path": path,
        "headers": [(k.lower(), v) for k, v in (headers or {}).items()],
        "query_string": query,
        "client": client,
    }


@pytest.fixture
def client():
    async def ok(_):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"]), Route("/health", ok)])
    app.add_middleware(security.ApiKeyMiddleware)
    return TestClient(app)


# --- _client_ip ---------------------------------------------------------------


def test_client_ip_prefers_x_real_ip():
    scope = _scope({b"x-real-ip": b"1.1.1.1", b"x-forwarded-for": b"2.2.2.2"})
    assert security._client_ip(scope) == "1.1.1.1"


def test_client_ip_uses_last_forwarded_entry():
    # Earlier entries are client-controlled; only the one appended by the proxy counts.
    scope = _scope({b"x-forwarded-for": b"6.6.6.6, 3.3.3.3"})
    assert security._client_ip(scope) == "3.3.3.3"


def test_client_ip_falls_back_to_socket_peer():
    assert security._client_ip(_scope()) == "10.0.0.1"
    assert security._client_ip({**_scope(), "client": None}) == "unknown"


# --- _valid ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers,query",
    [
        ({b"authorization": f"Bearer {API_KEY}".encode()}, b""),
        ({b"authorization": f"bearer   {API_KEY} ".encode()}, b""),
        # Unrelated query parameters don't matter.
        ({b"authorization": f"Bearer {API_KEY}".encode()}, b"api_key=wrong"),
    ],
)
def test_valid_accepts_correct_key(headers, query):
    assert security._valid(_scope(headers, query))


@pytest.mark.parametrize(
    "headers,query",
    [
        ({}, b""),
        ({b"authorization": b"Bearer wrong"}, b""),
        ({b"authorization": API_KEY.encode()}, b""),  # missing "Bearer "
        ({b"authorization": f"Basic {API_KEY}".encode()}, b""),
        ({}, f"key={API_KEY}".encode()),
        ({}, f"api_key={API_KEY[:-1]}".encode()),
        ({}, f"api_key={API_KEY}x".encode()),
        # The key is only accepted in the Authorization header, never in the URL.
        ({}, f"api_key={API_KEY}".encode()),
        ({}, f"api-key={API_KEY}".encode()),
        ({b"authorization": b"Bearer wrong"}, f"api_key={API_KEY}".encode()),
    ],
)
def test_valid_rejects_bad_key(headers, query):
    assert not security._valid(_scope(headers, query))


def test_non_ascii_key_matches_as_utf8(monkeypatch):
    key = "clé-" + "x" * 30
    monkeypatch.setattr(security, "MCP_API_KEY", key)
    assert security._valid(_scope({b"authorization": f"Bearer {key}".encode()}))
    assert not security._valid(_scope({b"authorization": f"Bearer {key}".encode("latin-1")}))


def test_non_utf8_header_does_not_crash():
    assert not security._valid(_scope({b"authorization": b"Bearer \xe9\xff"}, b"api_key=\xe9"))
    assert security._client_ip(_scope({b"x-real-ip": b"\xff"})) == "\xff"


# --- Middleware -------------------------------------------------------------------


def test_health_is_open(client):
    assert client.get("/health").status_code == 200


def test_missing_key_is_rejected(client):
    r = client.get("/mcp")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert API_KEY not in r.text


def test_good_key_passes(client):
    assert client.get("/mcp", headers={"Authorization": f"Bearer {API_KEY}"}).text == "ok"


@pytest.mark.parametrize("param", ["api_key", "api-key"])
def test_key_in_url_is_rejected_and_counted(client, param):
    r = client.get(f"/mcp?{param}={API_KEY}", headers={"X-Real-IP": "5.5.5.5"})
    assert r.status_code == 401
    assert len(security._failures["5.5.5.5"]) == 1


def test_ip_is_blocked_after_too_many_failures(client):
    ip = {"X-Real-IP": "9.9.9.9"}
    for _ in range(AUTH_MAX_FAILURES):
        assert client.get("/mcp", headers=ip).status_code == 401
    r = client.get("/mcp", headers={**ip, "Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == str(AUTH_WINDOW_SECONDS)
    # Other clients are unaffected.
    assert client.get("/mcp", headers={"X-Real-IP": "8.8.8.8", "Authorization": f"Bearer {API_KEY}"}).status_code == 200


def test_spoofed_forwarded_for_does_not_bypass_block(client):
    for i in range(AUTH_MAX_FAILURES):
        client.get("/mcp", headers={"X-Real-IP": "9.9.9.9", "X-Forwarded-For": f"1.2.3.{i}"})
    r = client.get("/mcp", headers={"X-Real-IP": "9.9.9.9", "X-Forwarded-For": "4.4.4.4"})
    assert r.status_code == 429


def test_block_expires_after_window(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: now[0])
    for _ in range(AUTH_MAX_FAILURES):
        client.get("/mcp", headers={"X-Real-IP": "9.9.9.9"})
    assert client.get("/mcp", headers={"X-Real-IP": "9.9.9.9"}).status_code == 429
    now[0] += AUTH_WINDOW_SECONDS + 1
    r = client.get("/mcp", headers={"X-Real-IP": "9.9.9.9", "Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 200
    assert "9.9.9.9" not in security._failures


def test_successful_requests_do_not_track_ip(client):
    client.get("/mcp", headers={"X-Real-IP": "7.7.7.7", "Authorization": f"Bearer {API_KEY}"})
    assert "7.7.7.7" not in security._failures


def test_tracked_ips_are_bounded(monkeypatch):
    monkeypatch.setattr(security, "_MAX_TRACKED_IPS", 3)
    for ip in ("a", "b", "c", "d"):
        security._record_failure(ip)
    assert list(security._failures) == ["b", "c", "d"]


@pytest.mark.anyio
async def test_websocket_is_refused():
    called, sent = [], []

    async def inner(scope, receive, send):
        called.append(scope)

    async def send(message):
        sent.append(message)

    await security.ApiKeyMiddleware(inner)({"type": "websocket", "path": "/mcp", "headers": []}, None, send)
    assert not called
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.anyio
async def test_lifespan_passes_through():
    called = []

    async def inner(scope, receive, send):
        called.append(scope["type"])

    await security.ApiKeyMiddleware(inner)({"type": "lifespan"}, None, None)
    assert called == ["lifespan"]
