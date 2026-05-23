# Zoom Client — HTTP client for the Zoom REST API using Server-to-Server OAuth.
# Created: 2026-05-19 — meetings integration. Uses the Zoom S2S OAuth grant
# (account_credentials); constructed from the deployment's single Zoom app
# credentials (account_id + client_id + client_secret) read from env.
#
# Surface:
#   create_meeting       — POST   /users/me/meetings  (or /users/{userId}/meetings)
#   list_meetings        — GET    /users/me/meetings
#   get_meeting          — GET    /meetings/{meetingId}
#   cancel_meeting       — DELETE /meetings/{meetingId}
#   list_recordings      — GET    /meetings/{meetingId}/recordings
#   download_transcript  — GET    {download_url}  (auth: bearer token)
#
# Out of scope (deferred to Phase 3): RTMS WebSocket, Meeting SDK bot path.

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ZOOM_BASE = "https://api.zoom.us/v2"
_REQUEST_TIMEOUT = 30


class ZoomAPIError(RuntimeError):
    """Raised when the Zoom API returns a non-2xx response.

    Carries the HTTP status code and the raw body so callers can map
    expired-token (401) or rate-limit (429) cases distinctly. Other
    error categories surface as a generic ``ZoomAPIError`` with the
    Zoom-supplied ``message`` from the response envelope.
    """

    def __init__(self, status_code: int, message: str, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.body = body or {}
        super().__init__(f"Zoom API {status_code}: {message}")


class ZoomClient:
    """Zoom REST client.

    Constructed from the deployment's single Zoom S2S OAuth app
    (``account_id`` + ``client_id`` + ``client_secret``). Mints a
    short-lived access token via the ``account_credentials`` grant on
    demand and caches it in-memory until expiry.
    """

    def __init__(self, account_id: str, client_id: str, client_secret: str) -> None:
        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str = ""
        self._token_expiry: float = 0.0  # time.monotonic() deadline

    async def _get_token(self) -> str:
        """Return a valid access token, minting one via the S2S grant if needed.

        Server-to-Server OAuth ``account_credentials`` grant — tokens are
        short-lived (~1h), cached in-memory and re-minted on expiry. No
        browser flow, no stored refresh token.
        """
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                "https://zoom.us/oauth/token",
                data={"grant_type": "account_credentials", "account_id": self._account_id},
                headers={"Authorization": f"Basic {basic}"},
            )
        if resp.status_code != 200:
            raise ZoomAPIError(resp.status_code, f"S2S token request failed: {resp.text[:200]}")
        payload = resp.json()
        self._token = payload["access_token"]
        # Refresh ~60s early to avoid edge-of-expiry 401s.
        self._token_expiry = time.monotonic() + max(0, int(payload.get("expires_in", 3600)) - 60)
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Single REST call with bearer auth + structured error mapping.

        Returns the parsed JSON body on 2xx (empty ``{}`` for 204).
        Raises ``ZoomAPIError`` with the Zoom envelope on non-2xx.
        """
        token = await self._get_token()
        url = path if path.startswith("http") else f"{_ZOOM_BASE}{path}"

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=json,
                params=params,
            )

        if resp.status_code == 204:
            return {}
        if 200 <= resp.status_code < 300:
            return resp.json() if resp.content else {}

        # Zoom error envelope: {"code": int, "message": str}
        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            pass
        message = body.get("message") or resp.text or "unknown error"
        raise ZoomAPIError(resp.status_code, message, body=body)

    # -----------------------------------------------------------------------
    # Meeting lifecycle
    # -----------------------------------------------------------------------

    async def create_meeting(
        self,
        *,
        topic: str,
        start_time: datetime | None = None,
        duration_minutes: int = 30,
        user_id: str = "me",
        agenda: str = "",
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a scheduled or instant Zoom meeting.

        ``user_id="me"`` schedules under the S2S app's account-default
        user. Pass an email or Zoom userId to schedule on another user's
        calendar within the same account.

        Type semantics (Zoom-defined):
            1 = instant, 2 = scheduled, 3 = recurring (no fixed time),
            8 = recurring (fixed time). We use 1 when ``start_time`` is
            null (start-now meeting) and 2 otherwise.
        """
        meeting_type = 2 if start_time is not None else 1
        body: dict[str, Any] = {
            "topic": topic,
            "type": meeting_type,
            "duration": duration_minutes,
            "agenda": agenda,
        }
        if start_time is not None:
            # Zoom expects RFC 3339 with the trailing "Z" for UTC.
            body["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if settings:
            body["settings"] = settings

        return await self._request("POST", f"/users/{user_id}/meetings", json=body)

    async def list_meetings(
        self,
        *,
        user_id: str = "me",
        meeting_type: str = "scheduled",
        page_size: int = 30,
        page_number: int = 1,
    ) -> dict[str, Any]:
        """List the user's meetings.

        ``meeting_type`` accepts ``"scheduled" | "live" | "upcoming"``
        and a few historical variants documented at Zoom's `/users/{userId}/meetings`.
        """
        return await self._request(
            "GET",
            f"/users/{user_id}/meetings",
            params={
                "type": meeting_type,
                "page_size": min(max(page_size, 1), 300),
                "page_number": max(page_number, 1),
            },
        )

    async def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        """Read a meeting's full record by ID."""
        return await self._request("GET", f"/meetings/{meeting_id}")

    async def cancel_meeting(self, meeting_id: str, *, notify_hosts: bool = True) -> None:
        """Cancel a Zoom meeting. Zoom returns 204 on success."""
        await self._request(
            "DELETE",
            f"/meetings/{meeting_id}",
            params={"schedule_for_reminder": "true" if notify_hosts else "false"},
        )

    # -----------------------------------------------------------------------
    # Recordings + transcripts (Phase 2)
    # -----------------------------------------------------------------------

    async def list_recordings(self, meeting_id: str) -> dict[str, Any]:
        """List cloud recordings + transcript files for a meeting.

        Response shape (relevant subset):
            { "recording_files": [
                { "id": str, "file_type": "MP4" | "TRANSCRIPT" | ..., "download_url": str }
              ] }
        """
        return await self._request("GET", f"/meetings/{meeting_id}/recordings")

    async def download_transcript(self, download_url: str) -> str:
        """Download a transcript file (typically VTT).

        Zoom requires a bearer token even for ``download_url``, so this
        does NOT take a separate URL but reuses the standard auth path.
        Returns the raw transcript text.
        """
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
            )
        if not 200 <= resp.status_code < 300:
            raise ZoomAPIError(resp.status_code, "transcript download failed", body={})
        return resp.text
