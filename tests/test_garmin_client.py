import json

import httpx
import pytest

from garmin_mcp import garmin_client
from garmin_mcp.config import TOKEN_PATH
from garmin_mcp.garmin_client import GarminClient, GarminError

pytestmark = pytest.mark.anyio


class FakeGarmin:
    """httpx transport standing in for Garmin: token endpoint + scripted API responses."""

    def __init__(self, token_response=None, api_responses=None):
        self.token_response = token_response or (lambda n: httpx.Response(200, json={"access_token": f"tok{n}", "expires_in": 3600}))
        self.api_responses = list(api_responses or [httpx.Response(200, json={"ok": True})])
        self.token_calls = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            self.token_calls += 1
            assert request.method == "POST"
            assert request.headers["authorization"] == "ApiKey garmin-personal-key"
            return self.token_response(self.token_calls)
        self.requests.append(request)
        return self.api_responses.pop(0) if len(self.api_responses) > 1 else self.api_responses[0]


def make_client(fake: FakeGarmin) -> GarminClient:
    c = GarminClient()
    c._http = httpx.AsyncClient(base_url="https://garmin.test", transport=httpx.MockTransport(fake))
    return c


# --- Token -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(200, json={"access_token": "a"}), "a"),
        (httpx.Response(200, json={"accessToken": "b", "expiresIn": 100}), "b"),
        (httpx.Response(200, json={"token": "c"}), "c"),
        (httpx.Response(200, json="d"), "d"),
        (httpx.Response(200, text="  raw-token \n"), "raw-token"),
    ],
)
async def test_token_formats(response, expected):
    fake = FakeGarmin(token_response=lambda n: response)
    c = make_client(fake)
    await c.get_json("/x")
    assert fake.requests[0].headers["authorization"] == f"Bearer {expected}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"error": "nope"}),  # must not use the JSON text as a token
        httpx.Response(200, json={"access_token": 123}),
        httpx.Response(200, text=""),
        httpx.Response(200, json=None),
        httpx.Response(200, text="<html><body>Maintenance</body></html>"),
        httpx.Response(200, text="tok\x00en"),
    ],
)
async def test_token_missing_is_an_error(response):
    c = make_client(FakeGarmin(token_response=lambda n: response))
    with pytest.raises(GarminError, match="no token"):
        await c.get_json("/x")


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_personal_key(status):
    c = make_client(FakeGarmin(token_response=lambda n: httpx.Response(status)))
    with pytest.raises(GarminError, match="rejected the personal API key"):
        await c.get_json("/x")


async def test_token_endpoint_error_hides_secret():
    c = make_client(FakeGarmin(token_response=lambda n: httpx.Response(500, text="garmin-personal-key")))
    with pytest.raises(GarminError) as exc:
        await c.get_json("/x")
    assert "HTTP 500" in str(exc.value)
    assert "garmin-personal-key" not in str(exc.value)


@pytest.mark.parametrize(
    "value,expected",
    [
        (3600, 3600.0),
        ("120", 120.0),
        (0, 0.0),  # 0 is a real value, not "missing"
        (None, garmin_client._DEFAULT_TTL),
        ("soon", garmin_client._DEFAULT_TTL),
        ("NaN", garmin_client._DEFAULT_TTL),
        ("inf", garmin_client._DEFAULT_TTL),
        (-5, garmin_client._DEFAULT_TTL),
        (True, garmin_client._DEFAULT_TTL),
    ],
)
def test_parse_ttl(value, expected):
    assert garmin_client._parse_ttl(value) == expected


async def test_zero_ttl_refreshes_quickly(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(garmin_client.time, "monotonic", lambda: now[0])
    fake = FakeGarmin(token_response=lambda n: httpx.Response(200, json={"access_token": f"t{n}", "expires_in": 0}))
    c = make_client(fake)
    await c.get_json("/x")
    now[0] += 11  # floor of 10 s, not the 600 s default
    await c.get_json("/x")
    assert fake.token_calls == 2


async def test_token_endpoint_unreachable():
    def boom(request):
        raise httpx.ConnectError("connection refused to https://garmin.test")

    c = GarminClient()
    c._http = httpx.AsyncClient(base_url="https://garmin.test", transport=httpx.MockTransport(boom))
    with pytest.raises(GarminError, match="token endpoint unreachable: ConnectError") as exc:
        await c.get_json("/x")
    assert "garmin.test" not in str(exc.value)


async def test_token_is_cached_then_refreshed_on_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(garmin_client.time, "monotonic", lambda: now[0])
    fake = FakeGarmin(token_response=lambda n: httpx.Response(200, json={"access_token": f"t{n}", "expires_in": 100}))
    c = make_client(fake)
    await c.get_json("/x")
    await c.get_json("/x")
    assert fake.token_calls == 1
    now[0] += 100 - garmin_client._EXPIRY_MARGIN  # refreshed ahead of the real expiry
    await c.get_json("/x")
    assert fake.token_calls == 2
    assert fake.requests[-1].headers["authorization"] == "Bearer t2"


async def test_401_forces_one_token_refresh_and_retry():
    fake = FakeGarmin(api_responses=[httpx.Response(401), httpx.Response(200, json={"ok": 1})])
    c = make_client(fake)
    assert await c.get_json("/x") == {"ok": 1}
    assert fake.token_calls == 2
    assert [r.headers["authorization"] for r in fake.requests] == ["Bearer tok1", "Bearer tok2"]


async def test_persistent_401_gives_up():
    fake = FakeGarmin(api_responses=[httpx.Response(401)])
    with pytest.raises(GarminError, match="authentication failed"):
        await make_client(fake).get_json("/x")
    assert len(fake.requests) == 2


# --- Responses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,message",
    [(403, "forbidden"), (404, "Not found"), (429, "rate limit"), (500, "HTTP 500"), (502, "HTTP 502")],
)
async def test_error_mapping(status, message):
    c = make_client(FakeGarmin(api_responses=[httpx.Response(status, text="details")]))
    with pytest.raises(GarminError, match=message):
        await c.get_json("/x")


@pytest.mark.parametrize("status", [301, 302, 304, 307])
async def test_redirect_is_an_error_not_empty_data(status):
    response = httpx.Response(status, headers={"location": "https://elsewhere.test/"}, text="moved")
    c = make_client(FakeGarmin(api_responses=[response]))
    with pytest.raises(GarminError, match=f"Unexpected Garmin API response \\(HTTP {status}\\)"):
        await c.get_json("/x")
    with pytest.raises(GarminError, match="Unexpected"):
        await c.get_file("/f")


@pytest.mark.parametrize("response", [httpx.Response(204), httpx.Response(200, content=b"")])
async def test_empty_response(response):
    c = make_client(FakeGarmin(api_responses=[response]))
    assert await c.get_json("/x") == {"data": None, "note": "No data for this request"}


async def test_invalid_json():
    c = make_client(FakeGarmin(api_responses=[httpx.Response(200, text="<html>")]))
    with pytest.raises(GarminError, match="invalid JSON"):
        await c.get_json("/x")


async def test_network_error():
    def boom(request):
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "a"})
        raise httpx.ConnectTimeout("timeout")

    c = GarminClient()
    c._http = httpx.AsyncClient(base_url="https://garmin.test", transport=httpx.MockTransport(boom))
    with pytest.raises(GarminError, match="unreachable: ConnectTimeout"):
        await c.get_json("/x")


async def test_params_are_cleaned():
    fake = FakeGarmin()
    await make_client(fake).get_json("/x", {"a": None, "b": True, "c": False, "d": 3, "e": "s"})
    assert dict(fake.requests[0].url.params) == {"b": "true", "c": "false", "d": "3", "e": "s"}


# --- Files ------------------------------------------------------------------------------


async def test_get_file():
    fake = FakeGarmin(api_responses=[httpx.Response(200, content=b"PK\x03\x04", headers={"content-type": "application/zip"})])
    result = await make_client(fake).get_file("/f")
    assert result == {"content_type": "application/zip", "size_bytes": 4, "base64": "UEsDBA=="}


async def test_get_file_too_large_stops_reading_early():
    # MAX_FILE_BYTES is 1000 in tests (conftest). The body is a stream: reading must stop at the cap.
    pulled = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(100):
                pulled.append(i)
                yield b"x" * 100

    fake = FakeGarmin(api_responses=[httpx.Response(200, stream=Body())])
    with pytest.raises(GarminError, match="too large"):
        await make_client(fake).get_file("/f")
    assert len(pulled) == 11  # 1100 bytes > 1000, then stop: the other 89 chunks are never read


@pytest.mark.parametrize("status,message", [(404, "Not found"), (500, "HTTP 500")])
async def test_get_file_large_error_body_reports_status(status, message):
    fake = FakeGarmin(api_responses=[httpx.Response(status, content=b"<html>" * 1000)])
    with pytest.raises(GarminError, match=message):
        await make_client(fake).get_file("/f")


async def test_get_file_large_401_body_still_retries():
    fake = FakeGarmin(api_responses=[httpx.Response(401, content=b"x" * 5000), httpx.Response(200, content=b"ok")])
    result = await make_client(fake).get_file("/f")
    assert result["size_bytes"] == 2
    assert fake.token_calls == 2


async def test_get_file_size_limit_does_not_apply_to_json():
    payload = {"x": "y" * 2000}
    fake = FakeGarmin(api_responses=[httpx.Response(200, content=json.dumps(payload).encode())])
    assert await make_client(fake).get_json("/x") == payload
