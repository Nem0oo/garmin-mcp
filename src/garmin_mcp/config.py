import os


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} environment variable is required")
    return value


# Key protecting the MCP endpoint itself (what claude.ai / clients send).
MCP_API_KEY = _required("MCP_API_KEY")
if len(MCP_API_KEY) < 32:
    raise RuntimeError("MCP_API_KEY must be at least 32 characters")

# Personal API key generated on the Garmin Connect web account. Exchanged for a short-lived access token.
GARMIN_PERSONAL_API_KEY = _required("GARMIN_PERSONAL_API_KEY")

# Public hostname served by nginx-proxy (Host header validation, DNS rebinding protection).
DOMAIN = _required("DOMAIN")

GARMIN_BASE_URL = os.environ.get("GARMIN_BASE_URL", "https://personal.connectapi.garmin.com").rstrip("/")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

PORT = int(os.environ.get("PORT", "8001"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
# Cap on the binary activity file returned through MCP (bytes, before base64).
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(5 * 1024 * 1024)))
