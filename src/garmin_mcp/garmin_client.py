"""Async client for the official Garmin Connect Personal API (read-only)."""
import asyncio
import base64
import json
import math
import re
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
        try:
            resp = await self._http.post(TOKEN_PATH, headers={"Authorization": f"ApiKey {GARMIN_PERSONAL_API_KEY}"})
        except httpx.HTTPError as exc:
            raise GarminError(f"Garmin token endpoint unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise GarminError("Garmin rejected the personal API key (invalid, revoked or expired). Generate a new one.")
        if resp.status_code >= 400:
            raise GarminError(f"Garmin token endpoint failed (HTTP {resp.status_code})")

        token, ttl = None, _DEFAULT_TTL
        try:
            body = resp.json()
        except ValueError:
            # Not JSON: the body is the raw token, unless it is clearly something else (e.g. an HTML page).
            raw = resp.text.strip()
            body = raw if _looks_like_token(raw) else None
        if isinstance(body, dict):
            token = body.get("access_token") or body.get("accessToken") or body.get("token")
            ttl = _parse_ttl(body.get("expires_in", body.get("expiresIn")))
        elif isinstance(body, str):
            token = body.strip()
        if not isinstance(token, str) or not token:
            raise GarminError("Garmin token endpoint returned no token")
        self._token = token
        self._expires_at = time.monotonic() + max(ttl - _EXPIRY_MARGIN, 10)

    async def _ensure_token(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._token or time.monotonic() >= self._expires_at:
                await self._fetch_token()
            assert self._token
            return self._token

    async def _request(
        self, path: str, params: dict[str, Any] | None, max_bytes: int | None = None
    ) -> tuple[httpx.Response, bytes]:
        """
        GET with token refresh on 401. The body is only read on success, and streamed so `max_bytes`
        is enforced before it is all in memory.
        """
        clean = {k: _fmt(v) for k, v in (params or {}).items() if v is not None}
        resp, content = None, b""
        for attempt in range(2):
            token = await self._ensure_token(force=attempt == 1)
            req = self._http.build_request("GET", path, params=clean, headers={"Authorization": f"Bearer {token}"})
            try:
                resp = await self._http.send(req, stream=True)
                try:
                    if 200 <= resp.status_code < 300 and resp.status_code != 204:
                        content = await _read_capped(resp, max_bytes)
                finally:
                    await resp.aclose()
            except httpx.HTTPError as exc:
                raise GarminError(f"Garmin API unreachable: {type(exc).__name__}") from exc
            if resp.status_code != 401:
                break
        assert resp is not None
        if resp.status_code == 204:
            return resp, b""
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
        if not 200 <= resp.status_code < 300:
            # Redirects are not followed: report them instead of answering "no data".
            raise GarminError(f"Unexpected Garmin API response (HTTP {resp.status_code})")
        return resp, content

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        _, content = await self._request(path, params)
        if not content:
            return {"data": None, "note": "No data for this request"}
        try:
            return json.loads(content)
        except ValueError as exc:
            raise GarminError("Garmin API returned invalid JSON") from exc

    async def get_file(self, path: str) -> dict[str, Any]:
        resp, content = await self._request(path, None, max_bytes=MAX_FILE_BYTES)
        return {
            "content_type": resp.headers.get("content-type"),
            "size_bytes": len(content),
            "base64": base64.b64encode(content).decode(),
        }

    async def aclose(self) -> None:
        await self._http.aclose()


# Bearer token syntax (RFC 6750 "b64token"): rejects HTML pages, JSON text, whitespace, control chars.
_TOKEN_RE = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


def _looks_like_token(value: str) -> bool:
    return len(value) <= 8192 and _TOKEN_RE.fullmatch(value) is not None


def _parse_ttl(value: Any) -> float:
    """Token lifetime in seconds; anything missing or unusable falls back to the default."""
    if isinstance(value, bool):
        return _DEFAULT_TTL
    try:
        ttl = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_TTL
    return ttl if math.isfinite(ttl) and ttl >= 0 else _DEFAULT_TTL


async def _read_capped(resp: httpx.Response, max_bytes: int | None) -> bytes:
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if max_bytes is not None and size > max_bytes:
            raise GarminError(f"File too large (limit {max_bytes} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def _fmt(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
