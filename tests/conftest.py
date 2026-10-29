import os

# config.py reads these at import time, so they must be set before any garmin_mcp import.
# Assigned (not setdefault) so values exported in the developer's shell can't change the tests.
os.environ.update(
    MCP_API_KEY="k" * 32,
    GARMIN_PERSONAL_API_KEY="garmin-personal-key",
    DOMAIN="mcp.example.com",
    MAX_FILE_BYTES="1000",
    AUTH_MAX_FAILURES="10",
    AUTH_WINDOW_SECONDS="300",
    HTTP_TIMEOUT="30",
    GARMIN_BASE_URL="https://garmin.test",
)

import pytest  # noqa: E402

from garmin_mcp import security  # noqa: E402

API_KEY = os.environ["MCP_API_KEY"]
DOMAIN = os.environ["DOMAIN"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    security._failures.clear()
    yield
    security._failures.clear()
