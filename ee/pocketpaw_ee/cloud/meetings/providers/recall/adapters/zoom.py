# ZoomConnector — native adapter wrapping ZoomClient.
# Created: 2026-05-19 — phase 1.4 of the meetings integration. Follows
# the GoogleCalendarConnector pattern (adapters/gcalendar.py). Single
# account: constructed from one deployment-wide Zoom Marketplace app
# (S2S OAuth credentials read from env).
#
# Action surface lines up with the design's MeetingResponse shape:
#   meeting_create / meeting_list / meeting_get / meeting_cancel
#   recording_list / transcript_get          (Phase 2 — listed for tool surface
#                                              parity; transcript persistence
#                                              wired by listeners in Phase 2.)
#
# Wiring into the cloud-side meetings/service.py lands in Phase 1.5
# via a MeetingsPersistencePort so this adapter stays free of Mongo /
# FastAPI imports.

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorHealth,
    ConnectorScope,
    ConnectorStatus,
    ExecutionMode,
    SyncResult,
    TrustLevel,
    WidgetRecipe,
)
from pocketpaw_ee.cloud.meetings.providers.recall.clients.zoom import ZoomAPIError, ZoomClient

logger = logging.getLogger(__name__)


class ZoomConnector:
    """Native Zoom connector implementing ConnectorProtocol.

    Single-account: constructed from the deployment's Zoom Server-to-Server
    OAuth credentials (``ZOOM_ACCOUNT_ID`` / ``ZOOM_CLIENT_ID`` /
    ``ZOOM_CLIENT_SECRET``). The factory in
    ``pocketpaw_ee/cloud/meetings/service.py`` reads those env vars and
    constructs one instance per request.
    """

    @property
    def name(self) -> str:
        return "zoom"

    @property
    def display_name(self) -> str:
        return "Zoom"

    @property
    def type(self) -> str:
        return "communication"

    @property
    def icon(self) -> str:
        return "video"

    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        *,
        client: ZoomClient | None = None,
    ) -> None:
        self._client = client or ZoomClient(account_id, client_id, client_secret)

    # -----------------------------------------------------------------------
    # Protocol surface
    # -----------------------------------------------------------------------

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        """Validate the configured S2S credentials; reports current state.

        Server-to-Server OAuth has no browser flow — the client mints a
        token directly from the env-configured app credentials. This
        method exists for ConnectorProtocol compatibility.
        """
        try:
            await self._client._get_token()  # noqa: SLF001
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message="Zoom connected",
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=str(exc),
            )

    async def disconnect(self, pocket_id: str) -> bool:
        # Single-account S2S: credentials are env-configured, nothing to
        # tear down beyond the in-memory client + its cached token.
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="meeting_create",
                description=(
                    "Schedule or instant-launch a Zoom meeting. Returns the "
                    "meeting ID, join URL, and host URL."
                ),
                method="POST",
                parameters={
                    "topic": {"type": "string", "description": "Meeting title"},
                    "start_time": {
                        "type": "string",
                        "description": ("ISO 8601 UTC start time. Omit for an instant meeting."),
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Meeting duration in minutes (default 30)",
                        "default": 30,
                    },
                    "agenda": {"type": "string", "description": "Agenda / description"},
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="meeting_list",
                description="List the host's upcoming or past meetings.",
                method="GET",
                parameters={
                    "meeting_type": {
                        "type": "string",
                        "description": "scheduled | live | upcoming",
                        "default": "upcoming",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Page size (default 30, max 300)",
                        "default": 30,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="meeting_get",
                description="Read full details for one meeting by ID.",
                method="GET",
                parameters={
                    "meeting_id": {"type": "string", "description": "Zoom meeting ID"},
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="meeting_cancel",
                description="Cancel a scheduled Zoom meeting.",
                method="DELETE",
                parameters={
                    "meeting_id": {"type": "string", "description": "Zoom meeting ID"},
                    "notify_hosts": {
                        "type": "boolean",
                        "description": "Send cancellation notice (default true)",
                        "default": True,
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="recording_list",
                description=(
                    "List cloud recordings + transcripts for a finished meeting. "
                    "Available ~2× the meeting duration after end."
                ),
                method="GET",
                parameters={
                    "meeting_id": {"type": "string", "description": "Zoom meeting ID"},
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="transcript_get",
                description=(
                    "Download the transcript for a finished meeting as VTT text. "
                    "Returns empty string if no transcript file exists."
                ),
                method="GET",
                parameters={
                    "meeting_id": {"type": "string", "description": "Zoom meeting ID"},
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        try:
            if action == "meeting_create":
                start_time = _parse_iso_utc(params.get("start_time"))
                data = await self._client.create_meeting(
                    topic=params["topic"],
                    start_time=start_time,
                    duration_minutes=int(params.get("duration_minutes", 30)),
                    agenda=params.get("agenda", ""),
                    # Unattended-friendly defaults: the Recall recording bot
                    # (and attendees) must get in with no host present.
                    #   waiting_room=False           — no lobby to be admitted from
                    #   join_before_host=True        — host account is never online
                    #   approval_type=2              — no registration gate
                    #   meeting_authentication=False — bot isn't a signed-in Zoom user
                    settings={
                        "waiting_room": False,
                        "join_before_host": True,
                        "approval_type": 2,
                        "meeting_authentication": False,
                    },
                )
                return ActionResult(success=True, data=data, records_affected=1)

            if action == "meeting_list":
                data = await self._client.list_meetings(
                    meeting_type=params.get("meeting_type", "upcoming"),
                    page_size=int(params.get("page_size", 30)),
                )
                meetings = data.get("meetings", [])
                return ActionResult(success=True, data=meetings, records_affected=len(meetings))

            if action == "meeting_get":
                data = await self._client.get_meeting(params["meeting_id"])
                return ActionResult(success=True, data=data, records_affected=1)

            if action == "meeting_cancel":
                await self._client.cancel_meeting(
                    params["meeting_id"],
                    notify_hosts=bool(params.get("notify_hosts", True)),
                )
                return ActionResult(success=True, records_affected=1)

            if action == "recording_list":
                data = await self._client.list_recordings(params["meeting_id"])
                files = data.get("recording_files", [])
                return ActionResult(success=True, data=files, records_affected=len(files))

            if action == "transcript_get":
                listing = await self._client.list_recordings(params["meeting_id"])
                files = listing.get("recording_files", [])
                transcript_url = next(
                    (f["download_url"] for f in files if f.get("file_type") == "TRANSCRIPT"),
                    None,
                )
                if not transcript_url:
                    return ActionResult(success=True, data="", records_affected=0)
                text = await self._client.download_transcript(transcript_url)
                return ActionResult(success=True, data=text, records_affected=1)

            return ActionResult(success=False, error=f"Unknown action: {action}")
        except ZoomAPIError as exc:
            return ActionResult(success=False, error=f"Zoom {action}: {exc}")
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing required param: {exc.args[0]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Zoom action %s failed", action)
            return ActionResult(success=False, error=f"Zoom {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        # Meetings don't sync into pocket.db — the meetings module
        # owns persistence via Phase 2's webhook + polling pipeline.
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "meetings", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        return [
            WidgetRecipe(
                title="Upcoming Zoom Meetings",
                display_type="feed",
                action="meeting_list",
                params={"meeting_type": "upcoming", "page_size": 10},
                default_size="col-1 row-2",
                description="Next 10 scheduled Zoom meetings",
            ),
        ]

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        try:
            await self._client._get_token()  # noqa: SLF001
            return ConnectorHealth(
                ok=True,
                status=ConnectorStatus.CONNECTED,
                message="Zoom reachable",
                checked_at_ms=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectorHealth(
                ok=False,
                status=ConnectorStatus.ERROR,
                message=str(exc),
                checked_at_ms=int(time.time() * 1000),
            )


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a UTC datetime, or return None."""
    if not value:
        return None
    # Tolerate both ``...Z`` and ``+00:00`` shapes from agent inputs.
    cleaned = value.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)
