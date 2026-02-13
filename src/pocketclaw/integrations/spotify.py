# Spotify Client — HTTP client for Spotify Web API using OAuth tokens.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

from __future__ import annotations

import logging
from typing import Any

import httpx

from pocketclaw.config import get_settings
from pocketclaw.integrations.oauth import OAuthManager
from pocketclaw.integrations.token_store import TokenStore

logger = logging.getLogger(__name__)

_SPOTIFY_BASE = "https://api.spotify.com/v1"


class SpotifyClient:
    """HTTP client for Spotify Web API.

    Uses OAuth bearer tokens from the token store.
    """

    def __init__(self):
        self._oauth = OAuthManager(TokenStore())

    async def _get_token(self) -> str:
        """Get a valid OAuth access token for Spotify."""
        settings = get_settings()
        token = await self._oauth.get_valid_token(
            service="spotify",
            client_id=settings.spotify_client_id or "",
            client_secret=settings.spotify_client_secret or "",
            provider="spotify",
        )
        if not token:
            raise RuntimeError(
                "Spotify not authenticated. Complete OAuth flow first "
                "(Settings > Spotify > Authorize)."
            )
        return token

    async def search(
        self, query: str, search_type: str = "track", limit: int = 5
    ) -> list[dict[str, Any]]:
        """Search Spotify for tracks, albums, or artists.

        Args:
            query: Search query.
            search_type: Type of search — 'track', 'album', or 'artist'.
            limit: Maximum results.

        Returns:
            List of result dicts.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_SPOTIFY_BASE}/search",
                params={"q": query, "type": search_type, "limit": min(limit, 20)},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        items_key = f"{search_type}s"
        for item in data.get(items_key, {}).get("items", []):
            entry: dict[str, Any] = {
                "name": item.get("name", ""),
                "id": item.get("id", ""),
                "uri": item.get("uri", ""),
                "type": search_type,
            }
            if search_type == "track":
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                album = item.get("album", {}).get("name", "")
                entry["artists"] = artists
                entry["album"] = album
                entry["duration_ms"] = item.get("duration_ms", 0)
            elif search_type == "album":
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                entry["artists"] = artists
                entry["total_tracks"] = item.get("total_tracks", 0)
            elif search_type == "artist":
                entry["genres"] = item.get("genres", [])
                entry["followers"] = item.get("followers", {}).get("total", 0)
            results.append(entry)

        return results

    async def now_playing(self) -> dict[str, Any] | None:
        """Get the currently playing track.

        Returns:
            Dict with track info, or None if nothing is playing.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_SPOTIFY_BASE}/me/player/currently-playing",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 204:
                return None
            resp.raise_for_status()
            data = resp.json()

        if not data.get("item"):
            return None

        item = data["item"]
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        return {
            "track": item.get("name", ""),
            "artists": artists,
            "album": item.get("album", {}).get("name", ""),
            "uri": item.get("uri", ""),
            "is_playing": data.get("is_playing", False),
            "progress_ms": data.get("progress_ms", 0),
            "duration_ms": item.get("duration_ms", 0),
        }

    async def playback_control(self, action: str, **kwargs: Any) -> str:
        """Control playback.

        Args:
            action: One of 'play', 'pause', 'next', 'prev', 'volume'.
            **kwargs: Extra args (e.g. volume_percent for 'volume', uri for 'play').

        Returns:
            Status message.
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=10) as client:
            if action == "play":
                body: dict[str, Any] = {}
                uri = kwargs.get("uri")
                if uri:
                    body["uris"] = [uri]
                resp = await client.put(
                    f"{_SPOTIFY_BASE}/me/player/play",
                    headers=headers,
                    json=body if body else None,
                )
            elif action == "pause":
                resp = await client.put(f"{_SPOTIFY_BASE}/me/player/pause", headers=headers)
            elif action == "next":
                resp = await client.post(f"{_SPOTIFY_BASE}/me/player/next", headers=headers)
            elif action == "prev":
                resp = await client.post(f"{_SPOTIFY_BASE}/me/player/previous", headers=headers)
            elif action == "volume":
                volume = kwargs.get("volume_percent", 50)
                resp = await client.put(
                    f"{_SPOTIFY_BASE}/me/player/volume",
                    params={"volume_percent": volume},
                    headers=headers,
                )
            else:
                return f"Unknown action: {action}"

            if resp.status_code in (200, 202, 204):
                return f"Playback: {action} OK"
            resp.raise_for_status()

        return f"Playback: {action} OK"

    async def get_playlists(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get user's playlists.

        Returns:
            List of playlist dicts.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_SPOTIFY_BASE}/me/playlists",
                params={"limit": min(limit, 50)},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            {
                "name": p.get("name", ""),
                "id": p.get("id", ""),
                "uri": p.get("uri", ""),
                "tracks": p.get("tracks", {}).get("total", 0),
                "public": p.get("public", False),
            }
            for p in data.get("items", [])
        ]

    async def add_to_playlist(self, playlist_id: str, track_uri: str) -> str:
        """Add a track to a playlist.

        Args:
            playlist_id: Spotify playlist ID.
            track_uri: Spotify track URI (spotify:track:...).

        Returns:
            Status message.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_SPOTIFY_BASE}/playlists/{playlist_id}/tracks",
                json={"uris": [track_uri]},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()

        return "Track added to playlist."
