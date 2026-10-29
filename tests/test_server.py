import json

import pytest
from starlette.testclient import TestClient

from garmin_mcp import server
from garmin_mcp.garmin_client import GarminError

from .conftest import API_KEY, DOMAIN


@pytest.fixture
def calls(monkeypatch):
    """Capture what the tools send to Garmin instead of calling it."""
    recorded = []

    async def fake_get_json(path, params=None):
        recorded.append((path, params))
        return {"path": path}

    async def fake_get_file(path):
        recorded.append((path, None))
        return {"base64": ""}

    monkeypatch.setattr(server.client, "get_json", fake_get_json)
    monkeypatch.setattr(server.client, "get_file", fake_get_file)
    return recorded


# --- Date helpers --------------------------------------------------------------------


def test_range_accepts_inclusive_maximum():
    server._range("2026-01-01", "2026-01-28", 28)
    server._range("2026-01-01", "2026-01-01", 1)


@pytest.mark.parametrize(
    "start,end,match",
    [
        ("2026-01-01", "2026-01-29", "maximum 28 days"),
        ("2026-01-02", "2026-01-01", "before start"),
        ("2026-02-30", "2026-03-01", "Invalid date"),
        ("2026-13-01", "2026-13-02", "Invalid date"),
    ],
)
def test_range_rejects(start, end, match):
    with pytest.raises(GarminError, match=match):
        server._range(start, end, 28)


# --- Tools (called through FastMCP, so argument validation runs) ----------------------


async def call(name, **args):
    return await server.mcp.call_tool(name, args)


@pytest.mark.anyio
async def test_all_tools_are_registered():
    tools = {t.name for t in await server.mcp.list_tools()}
    assert len(tools) == 27
    assert {"list_activities", "get_activity_file", "get_sleep", "get_body_battery"} <= tools


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,args,path,params",
    [
        ("get_activity", {"activity_id": 42}, "/activity-service/activity/42", None),
        ("get_activity_workouts", {"activity_id": 42}, "/activity-service/activity/42/workouts", None),
        ("get_activity_file", {"activity_id": 42}, "/download-service/files/activity/42", None),
        ("get_daily_summary", {"calendar_date": "2026-10-01"}, "/usersummary-service/usersummary/daily", {"calendarDate": "2026-10-01"}),
        ("get_steps", {"start_date": "2026-10-01", "end_date": "2026-10-07"}, "/usersummary-service/stats/steps/daily/2026-10-01/2026-10-07", None),
        ("get_hrv", {"calendar_date": "2026-10-01"}, "/hrv-service/hrv/2026-10-01", None),
        ("get_hrv_summaries", {"from_date": "2026-10-01", "to_date": "2026-10-07"}, "/hrv-service/hrv/daily/2026-10-01/2026-10-07", None),
        ("get_sleep", {"calendar_date": "2026-10-01"}, "/sleep-service/sleep/dailySleep", {"date": "2026-10-01"}),
        (
            "get_body_battery",
            {"start_date": "2026-10-01", "end_date": "2026-10-02", "chunks": 4},
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": "2026-10-01", "endDate": "2026-10-02", "chunks": 4},
        ),
        ("get_weight", {"start_date": "2026-01-01", "end_date": "2026-10-01"}, "/weight-service/weight/range/2026-01-01/2026-10-01", {"includeAll": False}),
        ("get_heart_rate_zones", {}, "/biometric-service/heartRateZones/v2", {"generateDefaults": True}),
        ("get_user_settings", {}, "/userprofile-service/userprofile/settings", None),
    ],
)
async def test_tool_requests(calls, tool, args, path, params):
    await call(tool, **args)
    assert calls == [(path, params)]


@pytest.mark.anyio
async def test_list_activities_maps_parameters(calls):
    await call("list_activities", limit=5, start_date="2026-10-01", activity_type="running", favorite=True, min_distance_m=1000)
    path, params = calls[0]
    assert path == "/activitylist-service/activities/search/activities"
    assert params["limit"] == 5
    assert params["startDate"] == "2026-10-01"
    assert params["activityType"] == "running"
    assert params["favorite"] == 1  # the API expects 1/0, not true/false
    assert params["minDistance"] == 1000
    assert params["sortOrder"] == "desc"
    assert params["endDate"] is None  # dropped later by the client


@pytest.mark.anyio
async def test_list_activities_favorite_false(calls):
    await call("list_activities", favorite=False)
    assert calls[0][1]["favorite"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,args",
    [
        ("get_sleep", {"calendar_date": "../../admin"}),
        ("get_hrv", {"calendar_date": "2026-10-01/../../x"}),
        ("get_steps", {"start_date": "2026-10-01", "end_date": "2026-10-29"}),  # 29 days
        ("get_activity", {"activity_id": "12/../../x"}),
        ("list_activities", {"limit": 101}),
        ("list_activities", {"limit": 0}),
        ("list_activities", {"sort_order": "random"}),
        ("get_body_battery", {"start_date": "2026-10-01", "end_date": "2026-10-02", "chunks": 49}),
    ],
)
async def test_invalid_arguments_never_reach_garmin(calls, tool, args):
    with pytest.raises(Exception):
        await call(tool, **args)
    assert calls == []


# --- HTTP app (auth + MCP transport + Host validation) ---------------------------------

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture(scope="module")
def http():
    # The MCP session manager can only be started once per process, so the app's lifespan is shared.
    with TestClient(server.app, base_url=f"https://{DOMAIN}") as c:
        yield c


def _rpc_result(response):
    data = next(line[5:] for line in response.text.splitlines() if line.startswith("data:"))
    return json.loads(data)["result"]


def test_health(http):
    assert http.get("/health").json() == {"status": "ok"}


def test_mcp_requires_key(http):
    assert http.post("/mcp", headers=MCP_HEADERS, json=LIST_TOOLS).status_code == 401


def test_mcp_rejects_key_in_url(http):
    assert http.post(f"/mcp?api_key={API_KEY}", headers=MCP_HEADERS, json=LIST_TOOLS).status_code == 401


def test_mcp_lists_tools_with_key(http):
    r = http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {API_KEY}"}, json=LIST_TOOLS)
    assert r.status_code == 200
    assert len(_rpc_result(r)["tools"]) == 27


def test_mcp_tool_error_is_reported_without_crash(http, monkeypatch):
    async def failing(path, params=None):
        raise GarminError("Garmin API rate limit reached, retry later")

    monkeypatch.setattr(server.client, "get_json", failing)
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_user_settings", "arguments": {}}}
    r = http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {API_KEY}"}, json=body)
    result = _rpc_result(r)
    assert result["isError"] is True
    assert "rate limit" in result["content"][0]["text"]


@pytest.mark.parametrize("headers", [{"Host": "evil.com"}, {"Origin": "https://evil.com"}])
def test_dns_rebinding_protection(http, headers):
    r = http.post("/mcp", headers={**MCP_HEADERS, **headers, "Authorization": f"Bearer {API_KEY}"}, json=LIST_TOOLS)
    assert r.status_code in (403, 421)
