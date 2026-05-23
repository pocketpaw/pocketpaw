# GoogleMeetConnector — native adapter wrapping GoogleMeetClient.
# Created: 2026-05-19 — phase 1.6 of the meetings integration. Follows
# the ZoomConnector pattern (adapters/zoom.py). Action surface mirrors
# Zoom's where semantically possible so the agent can reason about
# meetings provider-agnostically.
#
# Quirks vs Zoom:
#   * Meet has no native cancel endpoint — meeting_cancel marks the
#     row cancelled locally; the join URL stays live. Documented.
#   * Meet has no native list-meetings; we list conferenceRecords
#     (past) instead, since the agent already cross-queries our
#     Meeting collection for upcoming meetings via the meetings
#     meta-connector (Phase 1.7).
#   * Transcript download → no separate file URL — entries come back
#     as paginated JSON from the API, which the adapter serializes
#     into a VTT-ish blob for parity with the Zoom path.

from __future__ import annotations

import logging
import time
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
from pocketpaw_ee.cloud.meetings.providers.recall.clients.google_meet import (
    GoogleMeetAPIError,
    GoogleMeetClient,
)

logger = logging.getLogger(__name__)


class GoogleMeetConnector:
    """Native Google Meet connector implementing ConnectorProtocol.

    Single-account: constructed from the deployment's Google OAuth
    credentials (``GOOGLE_MEET_CLIENT_ID`` / ``GOOGLE_MEET_CLIENT_SECRET``
    / ``GOOGLE_MEET_REFRESH_TOKEN``). Access tokens are minted from the
    long-lived refresh token on demand.
    """

    @property
    def name(self) -> str:
        return "google_meet"

    @property
    def display_name(self) -> str:
        return "Google Meet"

    @property
    def type(self) -> str:
        return "communication"

    @property
    def icon(self) -> str:
        return "video"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        client: GoogleMeetClient | None = None,
    ) -> None:
        self._client = client or GoogleMeetClient(client_id, client_secret, refresh_token)

    # -----------------------------------------------------------------------
    # Protocol surface
    # -----------------------------------------------------------------------

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            await self._client._get_token()  # noqa: SLF001
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message="Google Meet connected",
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=str(exc),
            )

    async def disconnect(self, pocket_id: str) -> bool:
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="meeting_create",
                description=(
                    "Create a Google Meet meeting space. Returns the meeting URI "
                    "(join URL) and the space resource name."
                ),
                method="POST",
                parameters={
                    "topic": {"type": "string", "description": "Meeting title (stored locally)"},
                    "start_time": {
                        "type": "string",
                        "description": (
                            "ISO 8601 UTC start time. Meet doesn't pre-schedule via "
                            "REST — the time is recorded locally for our 'list meetings' "
                            "view; the actual conference starts when someone joins."
                        ),
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Meeting duration (advisory only — Meet enforces nothing)",
                        "default": 30,
                    },
                    "access_type": {
                        "type": "string",
                        "description": (
                            "OPEN (default) | TRUSTED | RESTRICTED. OPEN lets "
                            "anyone with the link join without knocking — required "
                            "for the recording bot to join unattended, since an "
                            "anonymous bot otherwise waits in the lobby for a host "
                            "to admit it, and the shared service account has no "
                            "human behind it to do so."
                        ),
                        "default": "OPEN",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="meeting_list",
                description=(
                    "List past conference records for the host. Use the meetings "
                    "meta-connector for upcoming meetings (Meet has no native list)."
                ),
                method="GET",
                parameters={
                    "page_size": {
                        "type": "integer",
                        "description": "Page size (default 25, max 100)",
                        "default": 25,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="meeting_get",
                description="Get a conference record by resource name.",
                method="GET",
                parameters={
                    "meeting_id": {
                        "type": "string",
                        "description": "Conference record name (conferenceRecords/{id})",
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="meeting_cancel",
                description=(
                    "Mark a meeting cancelled. Meet has no native cancel — this "
                    "ends any active conference but the join URL stays live."
                ),
                method="POST",
                parameters={
                    "meeting_id": {
                        "type": "string",
                        "description": "Space name (spaces/{id})",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="transcript_get",
                description=(
                    "Download the transcript for one conference record as text. "
                    "Returns empty string if no transcript exists. Note: Meet "
                    "deletes transcript entries 30 days after the conference."
                ),
                method="GET",
                parameters={
                    "meeting_id": {
                        "type": "string",
                        "description": "Conference record name (conferenceRecords/{id})",
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        try:
            if action == "meeting_create":
                # OPEN by default: an anonymous Recall recording bot waits in
                # the Meet lobby until a *host* admits it, and our shared
                # service account has no human behind it to click "admit".
                # OPEN = anyone with the link joins without knocking, so the
                # bot (and attendees) get in unattended.
                space = await self._client.create_space(
                    access_type=params.get("access_type", "OPEN")
                )
                # Normalize the response into Zoom-ish shape for the
                # service-layer mapper (it reads ``id``, ``join_url``,
                # ``space_name`` from data).
                normalized = {
                    "id": space.get("name", ""),  # spaces/{id}
                    "name": space.get("name", ""),
                    "space_name": space.get("name", ""),
                    "join_url": space.get("meetingUri", ""),
                    "meetingUri": space.get("meetingUri", ""),
                    "meetingCode": space.get("meetingCode", ""),
                }
                return ActionResult(success=True, data=normalized, records_affected=1)

            if action == "meeting_list":
                data = await self._client.list_conference_records(
                    page_size=int(params.get("page_size", 25))
                )
                records = data.get("conferenceRecords", [])
                return ActionResult(success=True, data=records, records_affected=len(records))

            if action == "meeting_get":
                data = await self._client.get_conference_record(params["meeting_id"])
                return ActionResult(success=True, data=data, records_affected=1)

            if action == "meeting_cancel":
                # Meet has no real "cancel" — best effort is to end the
                # active conference if one is running. Always succeeds
                # locally so the service can mark the row cancelled.
                space_name = params["meeting_id"]
                try:
                    await self._client.end_active_conference(space_name)
                except GoogleMeetAPIError as exc:
                    # 404 = no active conference (expected); anything
                    # else is real.
                    if exc.status_code != 404 and exc.status_code != 400:
                        raise
                return ActionResult(success=True, records_affected=1)

            if action == "transcript_get":
                # ``meeting_id`` is whatever we stored as
                # ``provider_meeting_id`` at create time. For Meet that's
                # the *space* resource name (``spaces/abc``). Transcripts
                # don't live on spaces — they live on the conference
                # records the space spawned when people joined. So:
                #   1. Resolve space → conferenceRecords.
                #   2. For each record, walk its transcript sessions.
                #   3. For each session, page through entries.
                # We accept conferenceRecord names directly too, so the
                # meetings meta-connector can hand us either form.
                #
                # Diagnostic INFO logs at each step — when transcripts
                # come back empty, the log line tells you whether nobody
                # joined, transcription wasn't enabled, or no words were
                # captured. Saves running the debug script.
                meeting_id = params["meeting_id"]
                if meeting_id.startswith("conferenceRecords/"):
                    record_names = [meeting_id]
                else:
                    space_name = (
                        meeting_id if meeting_id.startswith("spaces/") else f"spaces/{meeting_id}"
                    )
                    listing = await self._client.list_conference_records(
                        filter_=f'space.name="{space_name}"'
                    )
                    record_names = [
                        r["name"] for r in listing.get("conferenceRecords", []) if "name" in r
                    ]
                    if not record_names:
                        logger.info(
                            "Meet transcript empty: no conferenceRecords for %s "
                            "(nobody joined yet, or record still propagating)",
                            space_name,
                        )
                        return ActionResult(success=True, data="", records_affected=0)
                    logger.info(
                        "Meet transcript: space %s → %d conferenceRecord(s)",
                        space_name,
                        len(record_names),
                    )

                lines = ["WEBVTT"]
                got_any_entries = False
                empty_transcript_records: list[str] = []
                for record_name in record_names:
                    transcripts = (await self._client.list_transcripts(record_name)).get(
                        "transcripts", []
                    )
                    if not transcripts:
                        empty_transcript_records.append(record_name)
                        continue
                    for t in transcripts:
                        entry_count = 0
                        next_token = None
                        while True:
                            page = await self._client.list_transcript_entries(
                                t["name"], page_token=next_token
                            )
                            for entry in page.get("transcriptEntries", []):
                                got_any_entries = True
                                entry_count += 1
                                speaker = entry.get("participant", "speaker")
                                start = entry.get("startTime", "")
                                end = entry.get("endTime", "")
                                text = entry.get("text", "")
                                if start or end:
                                    lines.append(f"\n{start} --> {end}")
                                lines.append(f"<v {speaker}>{text}")
                            next_token = page.get("nextPageToken")
                            if not next_token:
                                break
                        logger.info("Meet transcript: %s → %d entries", t["name"], entry_count)
                if empty_transcript_records:
                    logger.info(
                        "Meet transcript: %d conferenceRecord(s) had NO transcripts "
                        "(was 'Take transcript' enabled? requires Business Standard+): %s",
                        len(empty_transcript_records),
                        empty_transcript_records,
                    )
                if not got_any_entries:
                    return ActionResult(success=True, data="", records_affected=0)
                return ActionResult(success=True, data="\n".join(lines), records_affected=1)

            return ActionResult(success=False, error=f"Unknown action: {action}")
        except GoogleMeetAPIError as exc:
            return ActionResult(success=False, error=f"Meet {action}: {exc}")
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing required param: {exc.args[0]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Google Meet action %s failed", action)
            return ActionResult(success=False, error=f"Meet {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "meetings", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        # Meet has no upcoming-list, so we contribute zero widgets here
        # — the meetings meta-connector (Phase 1.7) handles the
        # "Upcoming Meetings" feed across providers.
        return []

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        try:
            await self._client._get_token()  # noqa: SLF001
            return ConnectorHealth(
                ok=True,
                status=ConnectorStatus.CONNECTED,
                message="Google Meet reachable",
                checked_at_ms=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectorHealth(
                ok=False,
                status=ConnectorStatus.ERROR,
                message=str(exc),
                checked_at_ms=int(time.time() * 1000),
            )
