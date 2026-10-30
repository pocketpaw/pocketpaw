# MeetingsAggregatorConnector — read-only cross-provider meetings adapter.
# Created: 2026-05-19 — phase 1.7 of the meetings integration.
#
# Purpose: give the agent a provider-agnostic surface for the questions
# it actually wants to ask — "what did we discuss with Acme last week?"
# — without forcing it to pick a provider (zoom / google_meet). Backed
# by the meetings module's Mongo state rather than provider APIs, so
# this adapter:
#
#   * has NO credentials (auto-enabled when ≥1 provider is configured)
#   * is read-only (no create/cancel here — those need provider context)
#   * lives alongside the provider adapters so the existing
#     ``connector_tools_for(c)`` pipeline picks it up at startup
#
# Wiring into the cloud-side meetings service uses the same
# ``MeetingsPersistencePort`` pattern as the provider adapters — the
# query callbacks are injected at construction so this file stays free
# of Mongo / FastAPI imports.

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
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

logger = logging.getLogger(__name__)


# Callbacks the cloud-side factory binds to MeetingsService.* functions.
SearchCallback = Callable[..., Awaitable[list[dict[str, Any]]]]
ListRecentCallback = Callable[..., Awaitable[list[dict[str, Any]]]]
GetTranscriptCallback = Callable[..., Awaitable[dict[str, Any]]]


class MeetingsAggregatorConnector:
    """Cross-provider read-only aggregator for meetings.

    Constructed per-workspace with the workspace_id baked in. The
    callbacks are bound at construction so the adapter never needs to
    know about Mongo, Beanie, or FastAPI.
    """

    @property
    def name(self) -> str:
        return "meetings"

    @property
    def display_name(self) -> str:
        return "Meetings"

    @property
    def type(self) -> str:
        return "communication"

    @property
    def icon(self) -> str:
        return "calendar-clock"

    def __init__(
        self,
        workspace_id: str,
        *,
        search_fn: SearchCallback,
        list_recent_fn: ListRecentCallback,
        get_transcript_fn: GetTranscriptCallback,
    ) -> None:
        self._workspace_id = workspace_id
        self._search = search_fn
        self._list_recent = list_recent_fn
        self._get_transcript = get_transcript_fn

    # -----------------------------------------------------------------------
    # Protocol surface
    # -----------------------------------------------------------------------

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        # No-op connect — the aggregator has no credentials. It's
        # "connected" as soon as at least one provider adapter is
        # configured; the cloud-side factory enforces that.
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message="Meetings aggregator ready",
        )

    async def disconnect(self, pocket_id: str) -> bool:
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="search",
                description=(
                    "Search meetings across all configured providers. Matches "
                    "title, organizer, and participant names/emails. Pass a date "
                    "range to scope the search."
                ),
                method="GET",
                parameters={
                    "query": {"type": "string", "description": "Search text"},
                    "since": {
                        "type": "string",
                        "description": "ISO 8601 lower bound on scheduled_start",
                    },
                    "until": {
                        "type": "string",
                        "description": "ISO 8601 upper bound on scheduled_start",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 100)",
                        "default": 20,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="list_recent",
                description=(
                    "List the most recent meetings across all providers, "
                    "newest first. Includes upcoming + past."
                ),
                method="GET",
                parameters={
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 100)",
                        "default": 10,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="get_transcript_by_id",
                description=(
                    "Return transcript metadata for any meeting by its ID, "
                    "regardless of provider. Use ``meeting_id`` from a prior "
                    "search / list_recent result."
                ),
                method="GET",
                parameters={
                    "meeting_id": {
                        "type": "string",
                        "description": "Our internal meeting ID (not the provider's)",
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        try:
            if action == "search":
                since = _parse_iso_opt(params.get("since"))
                until = _parse_iso_opt(params.get("until"))
                rows = await self._search(
                    self._workspace_id,
                    query=params.get("query", ""),
                    since=since,
                    until=until,
                    limit=int(params.get("limit", 20)),
                )
                return ActionResult(success=True, data=rows, records_affected=len(rows))

            if action == "list_recent":
                rows = await self._list_recent(
                    self._workspace_id, limit=int(params.get("limit", 10))
                )
                return ActionResult(success=True, data=rows, records_affected=len(rows))

            if action == "get_transcript_by_id":
                data = await self._get_transcript(self._workspace_id, params["meeting_id"])
                return ActionResult(success=True, data=data, records_affected=1)

            return ActionResult(success=False, error=f"Unknown action: {action}")
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing required param: {exc.args[0]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Meetings aggregator action %s failed", action)
            return ActionResult(success=False, error=f"Meetings {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "meetings", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        return [
            WidgetRecipe(
                title="Recent Meetings",
                display_type="feed",
                action="list_recent",
                params={"limit": 10},
                default_size="col-1 row-2",
                description="Newest meetings across Zoom + Google Meet",
            ),
        ]

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        # The aggregator's "health" reduces to whether the meetings
        # module can be reached — a no-op for in-process calls.
        return ConnectorHealth(
            ok=True,
            status=ConnectorStatus.CONNECTED,
            message="Meetings aggregator ready",
            checked_at_ms=int(time.time() * 1000),
        )


def _parse_iso_opt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
