"""Async client for the official Garmin Connect Personal API (read-only)."""
import asyncio
import base64
import time
from typing import Any

import httpx

from .config import GARMIN_BASE_URL, GARMIN_PERSONAL_API_KEY, HTTP_TIMEOUT, MAX_FILE_BYTES, TOKEN_PATH

# Used when the token endpoint gives no expiry; refresh well before any plausible lifetime.
_DEFAULT_TTL = 600
_EXPIRY_MARGIN = 30


class GarminError(Exception):
    """Error surfaced to the MCP client. Never contains credentials."""


class GarminClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=GARMIN_BASE_URL, timeout=HTTP_TIMEOUT)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_token(self) -> None:
        resp = await self._http.post(TOKEN_PATH, headers={"Authorization": f"ApiKey {GARMIN_PERSONAL_API_KEY}"})
        if resp.status_code in (401, 403):
            raise GarminError("Garmin rejected the personal API key (invalid, revoked or expired). Generate a new one.")
        if resp.status_code >= 400:
            raise GarminError(f"Garmin token endpoint failed (HTTP {resp.status_code})")

        token, ttl = None, _DEFAULT_TTL
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            token = body.get("access_token") or body.get("accessToken") or body.get("token")
            ttl = body.get("expires_in") or body.get("expiresIn") or ttl
        elif isinstance(body, str):
            token = body
        if not token:
            token = resp.text.strip().strip('"') or None
        if not token:
            raise GarminError("Garmin token endpoint returned no token")
        self._token = token
        self._expires_at = time.monotonic() + max(float(ttl) - _EXPIRY_MARGIN, 10)

    async def _ensure_token(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._token or time.monotonic() >= self._expires_at:
                await self._fetch_token()
            assert self._token
            return self._token

    async def _request(self, path: str, params: dict[str, Any] | None) -> httpx.Response:
        clean = {k: _fmt(v) for k, v in (params or {}).items() if v is not None}
        resp = None
        for attempt in range(2):
            token = await self._ensure_token(force=attempt == 1)
            try:
                resp = await self._http.get(path, params=clean, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise GarminError(f"Garmin API unreachable: {type(exc).__name__}") from exc
            if resp.status_code != 401:
                break
        assert resp is not None
        if resp.status_code == 204:
            return resp
        if resp.status_code == 401:
            raise GarminError("Garmin API authentication failed")
        if resp.status_code == 403:
            raise GarminError("Garmin API access forbidden for this resource")
        if resp.status_code == 404:
            raise GarminError("Not found")
        if resp.status_code == 429:
            raise GarminError("Garmin API rate limit reached, retry later")
        if resp.status_code >= 400:
            raise GarminError(f"Garmin API error (HTTP {resp.status_code})")
        return resp

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._request(path, params)
        if resp.status_code == 204 or not resp.content:
            return {"data": None, "note": "No data for this request"}
        return resp.json()

    async def get_file(self, path: str) -> dict[str, Any]:
        resp = await self._request(path, None)
        if len(resp.content) > MAX_FILE_BYTES:
            raise GarminError(f"File too large ({len(resp.content)} bytes, limit {MAX_FILE_BYTES})")
        return {
            "content_type": resp.headers.get("content-type"),
            "size_bytes": len(resp.content),
            "base64": base64.b64encode(resp.content).decode(),
        }

    async def aclose(self) -> None:
        await self._http.aclose()


def _fmt(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
