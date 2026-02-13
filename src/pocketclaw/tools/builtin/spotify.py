# Spotify tools — search, now playing, playback control, playlists.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

import logging
from typing import Any

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class SpotifySearchTool(BaseTool):
    """Search Spotify for tracks, albums, or artists."""

    @property
    def name(self) -> str:
        return "spotify_search"

    @property
    def description(self) -> str:
        return "Search Spotify for tracks, albums, or artists."

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "type": {
                    "type": "string",
                    "description": "Search type: 'track', 'album', or 'artist' (default: track)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 5, max 20)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, type: str = "track", limit: int = 5) -> str:
        try:
            from pocketclaw.integrations.spotify import SpotifyClient

            client = SpotifyClient()
            results = await client.search(query, search_type=type, limit=limit)

            if not results:
                return f"No {type}s found for '{query}'."

            lines = [f"Spotify {type} results for '{query}':\n"]
            for i, r in enumerate(results, 1):
                if type == "track":
                    dur = r.get("duration_ms", 0) // 1000
                    mins, secs = divmod(dur, 60)
                    lines.append(
                        f"{i}. **{r['name']}** by {r.get('artists', 'Unknown')}\n"
                        f"   Album: {r.get('album', '')}"
                        f" | {mins}:{secs:02d}\n"
                        f"   URI: {r['uri']}"
                    )
                elif type == "album":
                    lines.append(
                        f"{i}. **{r['name']}** by {r.get('artists', 'Unknown')}\n"
                        f"   Tracks: {r.get('total_tracks', 0)} | URI: {r['uri']}"
                    )
                elif type == "artist":
                    genres = ", ".join(r.get("genres", [])[:3]) or "N/A"
                    lines.append(
                        f"{i}. **{r['name']}** ({r.get('followers', 0):,} followers)\n"
                        f"   Genres: {genres} | URI: {r['uri']}"
                    )
            return "\n".join(lines)

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Spotify search failed: {e}")


class SpotifyNowPlayingTool(BaseTool):
    """Get the currently playing track on Spotify."""

    @property
    def name(self) -> str:
        return "spotify_now_playing"

    @property
    def description(self) -> str:
        return "Show what's currently playing on Spotify."

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self) -> str:
        try:
            from pocketclaw.integrations.spotify import SpotifyClient

            client = SpotifyClient()
            result = await client.now_playing()

            if not result:
                return "Nothing is currently playing on Spotify."

            progress = result.get("progress_ms", 0) // 1000
            duration = result.get("duration_ms", 0) // 1000
            p_min, p_sec = divmod(progress, 60)
            d_min, d_sec = divmod(duration, 60)
            status = "Playing" if result.get("is_playing") else "Paused"

            return (
                f"{status}: **{result['track']}** by {result['artists']}\n"
                f"Album: {result['album']}\n"
                f"Progress: {p_min}:{p_sec:02d} / {d_min}:{d_sec:02d}\n"
                f"URI: {result['uri']}"
            )

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Spotify now playing failed: {e}")


class SpotifyPlaybackTool(BaseTool):
    """Control Spotify playback — play, pause, next, prev, volume."""

    @property
    def name(self) -> str:
        return "spotify_playback"

    @property
    def description(self) -> str:
        return (
            "Control Spotify playback. Actions: play, pause, next, prev, volume. "
            "For 'play' you can optionally provide a track URI. "
            "For 'volume' provide volume_percent (0-100)."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Playback action: play, pause, next, prev, volume",
                },
                "uri": {
                    "type": "string",
                    "description": "Spotify track URI for 'play' action (optional)",
                },
                "volume_percent": {
                    "type": "integer",
                    "description": "Volume percentage (0-100) for 'volume' action",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        uri: str | None = None,
        volume_percent: int | None = None,
    ) -> str:
        valid_actions = {"play", "pause", "next", "prev", "volume"}
        if action not in valid_actions:
            return self._error(
                f"Unknown action '{action}'. Valid: {', '.join(sorted(valid_actions))}"
            )

        try:
            from pocketclaw.integrations.spotify import SpotifyClient

            client = SpotifyClient()
            kwargs: dict[str, Any] = {}
            if uri:
                kwargs["uri"] = uri
            if volume_percent is not None:
                kwargs["volume_percent"] = max(0, min(100, volume_percent))

            result = await client.playback_control(action, **kwargs)
            return result

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Spotify playback failed: {e}")


class SpotifyPlaylistTool(BaseTool):
    """List playlists or add a track to a playlist."""

    @property
    def name(self) -> str:
        return "spotify_playlist"

    @property
    def description(self) -> str:
        return (
            "List your Spotify playlists or add a track to a playlist. "
            "Actions: 'list' (default) or 'add'."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action: 'list' or 'add' (default: list)",
                },
                "playlist_id": {
                    "type": "string",
                    "description": "Playlist ID (required for 'add')",
                },
                "track_uri": {
                    "type": "string",
                    "description": "Track URI (required for 'add')",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        action: str = "list",
        playlist_id: str | None = None,
        track_uri: str | None = None,
    ) -> str:
        try:
            from pocketclaw.integrations.spotify import SpotifyClient

            client = SpotifyClient()

            if action == "add":
                if not playlist_id or not track_uri:
                    return self._error("Both playlist_id and track_uri are required for 'add'.")
                result = await client.add_to_playlist(playlist_id, track_uri)
                return result

            # Default: list playlists
            playlists = await client.get_playlists()
            if not playlists:
                return "No playlists found."

            lines = [f"Your playlists ({len(playlists)}):\n"]
            for p in playlists:
                vis = "Public" if p.get("public") else "Private"
                lines.append(
                    f"- **{p['name']}** ({p.get('tracks', 0)} tracks, {vis})\n  ID: {p['id']}"
                )
            return "\n".join(lines)

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Spotify playlist failed: {e}")
