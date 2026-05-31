# SpotifyConnector — native adapter wrapping the existing SpotifyClient.
# Created: 2026-05-03 — Phase 1 PR-7. 4 actions mirror tools/builtin/spotify.py.

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


class SpotifyConnector:
    @property
    def name(self) -> str:
        return "spotify"

    @property
    def display_name(self) -> str:
        return "Spotify"

    @property
    def type(self) -> str:
        return "communication"

    @property
    def icon(self) -> str:
        return "music"

    def __init__(self) -> None:
        self._connected = False

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            from pocketpaw.clients.spotify import SpotifyClient

            client = SpotifyClient()
            await client._get_token()  # noqa: SLF001
            self._connected = True
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message="Spotify connected",
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=str(exc),
            )

    async def disconnect(self, pocket_id: str) -> bool:
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="spotify_search",
                description="Search Spotify for tracks, albums, or artists.",
                method="GET",
                parameters={
                    "query": {"type": "string", "description": "Search query"},
                    "type": {
                        "type": "string",
                        "description": "'track' | 'album' | 'artist' (default 'track')",
                        "default": "track",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 5, capped at 20)",
                        "default": 5,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="spotify_now_playing",
                description="Show what's currently playing.",
                method="GET",
                parameters={},
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="spotify_playback",
                description="Control playback: play | pause | next | previous | volume.",
                method="POST",
                parameters={
                    "action": {
                        "type": "string",
                        "description": "play | pause | next | previous | volume",
                    },
                    "volume_percent": {
                        "type": "integer",
                        "description": "Required when action=volume (0-100)",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="spotify_playlist",
                description="List your playlists.",
                method="GET",
                parameters={
                    "limit": {
                        "type": "integer",
                        "description": "Max playlists (default 20, capped at 50)",
                        "default": 20,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        from pocketpaw.clients.spotify import SpotifyClient

        try:
            client = SpotifyClient()

            if action == "spotify_search":
                results = await client.search(
                    params["query"],
                    type=params.get("type", "track"),
                    limit=min(int(params.get("limit", 5)), 20),
                )
                return ActionResult(success=True, data=results, records_affected=len(results))
            if action == "spotify_now_playing":
                data = await client.now_playing()
                return ActionResult(
                    success=True,
                    data=data or {"playing": False},
                    records_affected=1 if data else 0,
                )
            if action == "spotify_playback":
                kwargs = {k: v for k, v in params.items() if k != "action"}
                data = await client.playback_control(params["action"], **kwargs)
                return ActionResult(success=True, data={"message": data}, records_affected=1)
            if action == "spotify_playlist":
                results = await client.get_playlists(
                    limit=min(int(params.get("limit", 20)), 50),
                )
                return ActionResult(success=True, data=results, records_affected=len(results))
            return ActionResult(success=False, error=f"Unknown action: {action}")
        except RuntimeError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ActionResult(success=False, error=f"Spotify {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "spotify_tracks", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        return [
            WidgetRecipe(
                title="Now Playing",
                display_type="stats",
                action="spotify_now_playing",
                params={},
                default_size="col-1 row-1",
                description="What's currently playing on Spotify",
            ),
        ]

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        try:
            from pocketpaw.clients.spotify import SpotifyClient

            client = SpotifyClient()
            await client._get_token()  # noqa: SLF001
            return ConnectorHealth(
                ok=True,
                status=ConnectorStatus.CONNECTED,
                message="Spotify reachable",
                checked_at_ms=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectorHealth(
                ok=False,
                status=ConnectorStatus.ERROR,
                message=str(exc),
                checked_at_ms=int(time.time() * 1000),
            )
