# Google Meet Client — REST v2 client using OAuth 2.0 (3-leg + refresh).
# Created: 2026-05-19 — phase 1.6 of the meetings integration. Mirrors
# the Zoom client pattern (clients/zoom.py) but uses Google's standard
# OAuth (refresh_token grant) instead of S2S, and is constructed per
# workspace.
#
# Surface:
#   create_space            — POST /v2/spaces            (creates a new meeting "room")
#   get_space               — GET  /v2/spaces/{name}
#   end_active_conference   — POST /v2/spaces/{name}:endActiveConference
#   list_conference_records — GET  /v2/conferenceRecords
#   get_conference_record   — GET  /v2/conferenceRecords/{name}
#   list_transcripts        — GET  /v2/conferenceRecords/{record}/transcripts
#   list_transcript_entries — GET  /v2/conferenceRecords/{record}/transcripts/{t}/entries
#
# Cancel: Meet has no native cancel endpoint. The adapter marks the row
# cancelled locally and the join URL stays live (documented limitation
# in docs/plans/2026-05-19-meetings-integration-design.md §Section 4).

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MEET_BASE = "https://meet.googleapis.com/v2"
_REQUEST_TIMEOUT = 30


class GoogleMeetAPIError(RuntimeError):
    """Raised when the Meet API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.body = body or {}
        super().__init__(f"Google Meet API {status_code}: {message}")


class GoogleMeetClient:
    """Google Meet REST client.

    Constructed from the deployment's single Google OAuth client
    (``client_id`` + ``client_secret``) and a long-lived ``refresh_token``.
    Mints short-lived access tokens via the refresh-token grant on demand
    and caches them in-memory until expiry.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token: str = ""
        self._token_expiry: float = 0.0  # time.monotonic() deadline

    async def _get_token(self) -> str:
        """Return a valid access token via the refresh-token grant.

        The long-lived refresh token comes from env; access tokens are
        short-lived and cached in-memory until ~60s before expiry.
        """
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        if resp.status_code != 200:
            raise GoogleMeetAPIError(resp.status_code, f"token refresh failed: {resp.text[:200]}")
        payload = resp.json()
        self._token = payload["access_token"]
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
        token = await self._get_token()
        url = path if path.startswith("http") else f"{_MEET_BASE}{path}"
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=json,
                params=params,
            )
        if 200 <= resp.status_code < 300:
            return resp.json() if resp.content else {}

        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            pass
        # Google error envelope: {"error": {"code", "message", "status"}}
        err = body.get("error") if isinstance(body, dict) else None
        message = (err or {}).get("message") if isinstance(err, dict) else None
        message = message or resp.text or "unknown error"
        raise GoogleMeetAPIError(resp.status_code, message, body=body)

    # -----------------------------------------------------------------------
    # Spaces — Meet's persistent "room" resource. Creating one returns a
    # ``meetingUri`` join URL; the actual conference instance is born when
    # someone joins.
    # -----------------------------------------------------------------------

    async def create_space(
        self,
        *,
        access_type: str = "TRUSTED",
        entry_point_access: str = "ALL",
    ) -> dict[str, Any]:
        """Create a new meeting space.

        Meet does NOT support pre-scheduling a meeting with a start time
        through the REST API — that's a calendar concern (Google
        Calendar API). The space's ``meetingUri`` is the join URL.

        ``access_type`` accepts ``OPEN | TRUSTED | RESTRICTED`` (default
        ``TRUSTED`` = anyone in the host's domain can join without
        knocking). ``entry_point_access`` controls phone/SIP entry —
        usually ``ALL``.
        """
        body = {
            "config": {
                "accessType": access_type,
                "entryPointAccess": entry_point_access,
            }
        }
        return await self._request("POST", "/spaces", json=body)

    async def get_space(self, space_name: str) -> dict[str, Any]:
        """Get a space by its resource name (``spaces/{id}`` or bare id)."""
        path = f"/{space_name}" if space_name.startswith("spaces/") else f"/spaces/{space_name}"
        return await self._request("GET", path)

    async def end_active_conference(self, space_name: str) -> None:
        """Terminate any currently-active conference inside this space."""
        path = (
            f"/{space_name}:endActiveConference"
            if space_name.startswith("spaces/")
            else f"/spaces/{space_name}:endActiveConference"
        )
        await self._request("POST", path, json={})

    # -----------------------------------------------------------------------
    # Conference records — the historical record of each conference instance.
    # -----------------------------------------------------------------------

    async def list_conference_records(
        self,
        *,
        filter_: str = "",
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """List conference records for the calling user / space.

        Meet supports a filter syntax like
        ``"space.name=\"spaces/abc\""`` to scope to a single space; pass
        an empty string for the user-wide list.
        """
        params: dict[str, Any] = {"pageSize": min(max(page_size, 1), 100)}
        if filter_:
            params["filter"] = filter_
        if page_token:
            params["pageToken"] = page_token
        return await self._request("GET", "/conferenceRecords", params=params)

    async def get_conference_record(self, record_name: str) -> dict[str, Any]:
        """Get a conference record by full resource name (``conferenceRecords/{id}``)."""
        path = (
            f"/{record_name}"
            if record_name.startswith("conferenceRecords/")
            else (f"/conferenceRecords/{record_name}")
        )
        return await self._request("GET", path)

    # -----------------------------------------------------------------------
    # Transcripts (Phase 2)
    # -----------------------------------------------------------------------

    async def list_transcripts(self, record_name: str) -> dict[str, Any]:
        """List transcript sessions for one conference record."""
        prefix = (
            record_name
            if record_name.startswith("conferenceRecords/")
            else (f"conferenceRecords/{record_name}")
        )
        return await self._request("GET", f"/{prefix}/transcripts")

    async def list_transcript_entries(
        self,
        transcript_name: str,
        *,
        page_size: int = 1000,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """List speaker-level entries for one transcript session.

        Meet deletes these 30 days after the conference ends. The
        polling fallback in ee/cloud/cycles enforces this window.
        """
        params: dict[str, Any] = {"pageSize": min(max(page_size, 1), 1000)}
        if page_token:
            params["pageToken"] = page_token
        prefix = (
            transcript_name
            if transcript_name.startswith("conferenceRecords/")
            else f"conferenceRecords/{transcript_name}"
        )
        return await self._request("GET", f"/{prefix}/entries", params=params)
