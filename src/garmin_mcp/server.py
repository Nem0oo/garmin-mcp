from datetime import date, timedelta
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import DOMAIN, PORT
from .garmin_client import GarminClient, GarminError
from .security import ApiKeyMiddleware

client = GarminClient()

mcp = FastMCP(
    "garmin-mcp",
    instructions=(
        "Read-only access to the user's Garmin Connect data through the official Garmin Personal API. "
        "Dates are YYYY-MM-DD. Prefer date-range tools over many single-day calls."
    ),
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[DOMAIN, f"{DOMAIN}:*"],
        allowed_origins=[f"https://{DOMAIN}"],
    ),
)

Date = Annotated[str, Field(description="Date in YYYY-MM-DD format", pattern=r"^\d{4}-\d{2}-\d{2}$")]
Limit = Annotated[int, Field(ge=1, le=100)]


def _parse(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GarminError(f"Invalid date '{value}', expected YYYY-MM-DD") from exc


def _range(start: str, end: str, max_days: int) -> None:
    s, e = _parse(start), _parse(end)
    if e < s:
        raise GarminError("end date is before start date")
    if e - s > timedelta(days=max_days - 1):
        raise GarminError(f"Date range too large: maximum {max_days} days")


async def _json(path: str, params: dict[str, Any] | None = None) -> Any:
    return await client.get_json(path, params)


# --- Health -----------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool()
def server_health() -> str:
    """Check that the MCP server is up."""
    return "Server is up and running !"


# --- Activities ---------------------------------------------------------------


@mcp.tool()
async def list_activities(
    start: Annotated[int, Field(ge=0)] = 0,
    limit: Limit = 10,
    start_date: Date | None = None,
    end_date: Date | None = None,
    activity_type: Annotated[str | None, Field(description="Parent type key, e.g. running, cycling")] = None,
    activity_sub_type: Annotated[str | None, Field(description="Subtype key, e.g. trail_running")] = None,
    event_type: Annotated[str | None, Field(description="race, training, recreation, fitness, specialEvent, uncategorized")] = None,
    search: Annotated[str | None, Field(description="Text in activity name/description")] = None,
    min_distance_m: Annotated[int | None, Field(ge=0)] = None,
    max_distance_m: Annotated[int | None, Field(ge=0)] = None,
    min_duration_s: Annotated[int | None, Field(ge=0)] = None,
    max_duration_s: Annotated[int | None, Field(ge=0)] = None,
    favorite: Annotated[bool | None, Field(description="True: favorites only, False: non-favorites only")] = None,
    sort_by: Annotated[str | None, Field(description="e.g. startLocal (default), distance, duration, name")] = None,
    sort_order: Literal["asc", "desc"] = "desc",
) -> Any:
    """Search activities (newest first by default) and return paginated activity summaries."""
    for d in (start_date, end_date):
        if d:
            _parse(d)
    return await _json(
        "/activitylist-service/activities/search/activities",
        {
            "start": start,
            "limit": limit,
            "startDate": start_date,
            "endDate": end_date,
            "activityType": activity_type,
            "activitySubType": activity_sub_type,
            "eventType": event_type,
            "search": search,
            "minDistance": min_distance_m,
            "maxDistance": max_distance_m,
            "minDuration": min_duration_s,
            "maxDuration": max_duration_s,
            "favorite": None if favorite is None else int(favorite),
            "sortBy": sort_by,
            "sortOrder": sort_order,
        },
    )


@mcp.tool()
async def get_activity(activity_id: int) -> Any:
    """Activity summary, metadata, measurements and split summaries for one activity."""
    return await _json(f"/activity-service/activity/{int(activity_id)}")


@mcp.tool()
async def get_activity_workouts(activity_id: int) -> Any:
    """Workouts performed during an activity."""
    return await _json(f"/activity-service/activity/{int(activity_id)}/workouts")


@mcp.tool()
async def get_activity_file(activity_id: int) -> Any:
    """
    Download the original activity file (zip, typically containing the FIT file).
    Returns {content_type, size_bytes, base64}; refused above the configured size limit.
    """
    return await client.get_file(f"/download-service/files/activity/{int(activity_id)}")


@mcp.tool()
async def get_wellness_activity_summaries(from_date: Date, until_date: Date) -> Any:
    """Wellness activity summaries (inclusive date range)."""
    _range(from_date, until_date, 366)
    return await _json(f"/wellnessactivity-service/activity/summary/range/{from_date}/{until_date}")


# --- Daily summaries ------------------------------------------------------------


@mcp.tool()
async def get_daily_summary(calendar_date: Date) -> Any:
    """Day summary: steps, calories, stress, Body Battery and other wellness metrics."""
    _parse(calendar_date)
    return await _json("/usersummary-service/usersummary/daily", {"calendarDate": calendar_date})


@mcp.tool()
async def get_daily_summaries(start_date: Date, end_date: Date) -> Any:
    """Daily summaries over a range (max 1 year)."""
    _range(start_date, end_date, 366)
    return await _json("/usersummary-service/v2/dailysummaries", {"startDate": start_date, "endDate": end_date})


@mcp.tool()
async def get_steps(start_date: Date, end_date: Date) -> Any:
    """Daily steps over a range (max 28 days)."""
    _range(start_date, end_date, 28)
    return await _json(f"/usersummary-service/stats/steps/daily/{start_date}/{end_date}")


@mcp.tool()
async def get_stress(start_date: Date, end_date: Date) -> Any:
    """Daily stress over a range (max 28 days)."""
    _range(start_date, end_date, 28)
    return await _json(f"/usersummary-service/stats/stress/daily/{start_date}/{end_date}")


@mcp.tool()
async def get_hydration(start_date: Date, end_date: Date) -> Any:
    """Daily hydration over a range (max 31 days)."""
    _range(start_date, end_date, 31)
    return await _json(f"/usersummary-service/stats/hydration/daily/{start_date}/{end_date}")


# --- Sleep, HRV, heart rate, body ------------------------------------------------


@mcp.tool()
async def get_sleep(calendar_date: Date) -> Any:
    """Detailed sleep data for one night (identified by its calendar date)."""
    _parse(calendar_date)
    return await _json("/sleep-service/sleep/dailySleep", {"date": calendar_date})


@mcp.tool()
async def get_sleep_summaries(start_date: Date, end_date: Date) -> Any:
    """Sleep summaries over a range (max 31 days)."""
    _range(start_date, end_date, 31)
    return await _json("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": start_date, "endDate": end_date})


@mcp.tool()
async def get_hrv(calendar_date: Date) -> Any:
    """HRV data (readings, status, baseline) for one date."""
    _parse(calendar_date)
    return await _json(f"/hrv-service/hrv/{calendar_date}")


@mcp.tool()
async def get_hrv_summaries(from_date: Date, to_date: Date) -> Any:
    """HRV daily summaries over a range."""
    _range(from_date, to_date, 366)
    return await _json(f"/hrv-service/hrv/daily/{from_date}/{to_date}")


@mcp.tool()
async def get_heart_rate(calendar_date: Date) -> Any:
    """Heart rate data (resting, min/max, time series) for one date."""
    _parse(calendar_date)
    return await _json("/wellness-service/wellness/dailyHeartRate", {"date": calendar_date})


@mcp.tool()
async def get_body_battery(
    start_date: Date,
    end_date: Date,
    chunks: Annotated[int | None, Field(ge=1, le=48, description="Intervals per day; low/high Body Battery returned per interval")] = None,
) -> Any:
    """Body Battery reports over a range (max 31 days)."""
    _range(start_date, end_date, 31)
    return await _json("/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": start_date, "endDate": end_date, "chunks": chunks})


@mcp.tool()
async def get_spo2(calendar_date: Date) -> Any:
    """SpO2 readings and acclimation data for one date."""
    _parse(calendar_date)
    return await _json(f"/wellness-service/wellness/daily/spo2acclimation/{calendar_date}")


@mcp.tool()
async def get_weight(start_date: Date, end_date: Date, include_all: bool = False) -> Any:
    """Weight summaries over a range: daily latest (or all entries), average, adjacent weights."""
    _range(start_date, end_date, 366)
    return await _json(f"/weight-service/weight/range/{start_date}/{end_date}", {"includeAll": include_all})


@mcp.tool()
async def get_weight_entries(start_date: Date, end_date: Date) -> Any:
    """All raw weight entries in a range."""
    _range(start_date, end_date, 366)
    return await _json(f"/weight-service/weight/all/{start_date}/{end_date}")


@mcp.tool()
async def get_weight_goals(start_date: Date, end_date: Date) -> Any:
    """Effective weight goals and goal ranges over a date interval."""
    _range(start_date, end_date, 366)
    return await _json(f"/goal-service/goal/user/effective/weightgoal/{start_date}/{end_date}")


@mcp.tool()
async def get_fitness_age(calendar_date: Date, full_history_scan: bool = True) -> Any:
    """Latest Fitness Age as of a date (if false, only the previous 28 days are considered)."""
    _parse(calendar_date)
    return await _json(f"/fitnessage-service/fitnessage/v2/{calendar_date}", {"fullHistoryScan": full_history_scan})


@mcp.tool()
async def get_heart_rate_zones(generate_defaults: bool = True) -> Any:
    """Configured heart rate zones (defaults generated if none are set and generate_defaults is true)."""
    return await _json("/biometric-service/heartRateZones/v2", {"generateDefaults": generate_defaults})


@mcp.tool()
async def get_user_settings() -> Any:
    """User profile settings (units, formats, preferences)."""
    return await _json("/userprofile-service/userprofile/settings")


# --- Nutrition & cycle tracking ---------------------------------------------------


@mcp.tool()
async def get_nutrition_log(
    calendar_date: Date,
    include_goals: bool = True,
    include_nutrition: bool = True,
    meal_id: int | None = None,
) -> Any:
    """Daily nutrition summary: goals, nutrients consumed and meals."""
    _parse(calendar_date)
    return await _json(
        f"/nutrition-service/food/logs/{calendar_date}",
        {"includeGoals": include_goals, "includeNutrition": include_nutrition, "includeMedia": False, "mealId": meal_id},
    )


@mcp.tool()
async def get_menstrual_cycle_summary(calendar_date: Date) -> Any:
    """Menstrual cycle summary for a date."""
    _parse(calendar_date)
    return await _json(f"/periodichealth-service/menstrualcycle/summary/{calendar_date}")


@mcp.tool()
async def get_menstrual_cycle_day(calendar_date: Date, include_analysis: bool = False) -> Any:
    """Detailed menstrual cycle day view (logged symptoms, flow, mood, cycle summary)."""
    _parse(calendar_date)
    return await _json(
        f"/periodichealth-service/menstrualcycle/dayview/{calendar_date}",
        {"todayDate": date.today().isoformat(), "includeAnalysis": include_analysis},
    )


# --- App ----------------------------------------------------------------------------


def build_app():
    app = mcp.streamable_http_app()
    app.add_middleware(ApiKeyMiddleware)

    return app


app = build_app()


def main() -> None:
    # access_log disabled: the API key can travel in the query string.
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
