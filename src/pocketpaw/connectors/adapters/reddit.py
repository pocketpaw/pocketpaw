# RedditConnector — native adapter wrapping the existing RedditClient.
# Created: 2026-05-03 — Phase 1 PR-7. Same shape as the Google
# Workspace adapters; 3 actions mirror tools/builtin/reddit.py.

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

logger = logging.getLogger(__name__)


class RedditConnector:
    """Native Reddit connector implementing ConnectorProtocol."""

    @property
    def name(self) -> str:
        return "reddit"

    @property
    def display_name(self) -> str:
        return "Reddit"

    @property
    def type(self) -> str:
        return "knowledge"

    @property
    def icon(self) -> str:
        return "rss"

    def __init__(self) -> None:
        self._connected = False

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        # Reddit can run anonymously app-only; flip connected without an auth probe.
        self._connected = True
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message="Reddit connected (app-only)",
        )

    async def disconnect(self, pocket_id: str) -> bool:
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="reddit_search",
                description="Search Reddit posts. Pass a query and optional subreddit.",
                method="GET",
                parameters={
                    "query": {"type": "string", "description": "Search query"},
                    "subreddit": {
                        "type": "string",
                        "description": "Restrict to a subreddit (optional)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, capped at 25)",
                        "default": 10,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="reddit_read",
                description="Read a Reddit post + top comments. Accepts URL or post ID.",
                method="GET",
                parameters={
                    "url": {"type": "string", "description": "Reddit URL or post ID"},
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="reddit_trending",
                description="Top posts in a subreddit, sorted by hot or top.",
                method="GET",
                parameters={
                    "subreddit": {
                        "type": "string",
                        "description": "Subreddit name (e.g., 'programming')",
                    },
                    "sort": {
                        "type": "string",
                        "description": "'hot' | 'top' (default 'hot')",
                        "default": "hot",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max posts (default 10, capped at 25)",
                        "default": 10,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        from pocketpaw.integrations.reddit import RedditClient

        try:
            client = RedditClient()
            limit_cap = lambda v: min(int(v), 25)  # noqa: E731 — local cap helper

            if action == "reddit_search":
                results = await client.search(
                    params["query"],
                    subreddit=params.get("subreddit"),
                    limit=limit_cap(params.get("limit", 10)),
                )
                return ActionResult(success=True, data=results, records_affected=len(results))
            if action == "reddit_read":
                data = await client.get_post(params["url"])
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "reddit_trending":
                results = await client.get_subreddit_top(
                    params["subreddit"],
                    sort=params.get("sort", "hot"),
                    limit=limit_cap(params.get("limit", 10)),
                )
                return ActionResult(success=True, data=results, records_affected=len(results))
            return ActionResult(success=False, error=f"Unknown action: {action}")
        except RuntimeError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ActionResult(success=False, error=f"Reddit {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "reddit_posts", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        # No reasonable workspace-level default — Reddit recipes are
        # subreddit-specific. Users add via "Ask agent" with a target
        # subreddit. Return empty so the picker doesn't suggest a
        # generic feed that would 404 without a subreddit param.
        return []

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        return ConnectorHealth(
            ok=self._connected,
            status=ConnectorStatus.CONNECTED if self._connected else ConnectorStatus.DISCONNECTED,
            message="reddit (app-only)" if self._connected else "not connected",
            checked_at_ms=int(time.time() * 1000),
        )
