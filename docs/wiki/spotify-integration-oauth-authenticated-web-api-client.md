---
{
  "title": "Spotify Integration: OAuth-Authenticated Web API Client",
  "summary": "The Spotify client provides PocketPaw agents with authenticated access to the Spotify Web API for playback control, track search, and playlist management. It delegates token lifecycle management to the shared `OAuthManager` and `TokenStore` infrastructure, raising a clear error if the user has not completed the OAuth flow.",
  "concepts": [
    "SpotifyClient",
    "OAuth 2.0",
    "OAuthManager",
    "TokenStore",
    "playback control",
    "Spotify Web API",
    "bearer tokens",
    "httpx",
    "Phase 4 Media Integrations",
    "frozenset success codes"
  ],
  "categories": [
    "integrations",
    "media",
    "authentication"
  ],
  "source_docs": [
    "f078f706da5cec20"
  ],
  "backlinks": null,
  "word_count": 347,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/integrations/spotify.py` is part of Phase 4 Media Integrations. It wraps the Spotify Web API (`https://api.spotify.com/v1`) behind a thin async client that handles authentication transparently for the calling agent.

## Authentication Architecture

Unlike the Reddit client, Spotify requires OAuth 2.0 bearer tokens. Rather than managing tokens directly, `SpotifyClient` delegates entirely to the shared `OAuthManager` (which handles token refresh) and `TokenStore` (which persists tokens to disk at `~/.pocketpaw/oauth/spotify.json`).

```python
class SpotifyClient:
    def __init__(self):
        self._oauth = OAuthManager(TokenStore())

    async def _get_token(self) -> str:
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
```

The error message deliberately includes the UI path so it surfaces actionably to the end user, not just in logs.

## Playback Control: Success Code Set

The `playback_control` method handles actions like play, pause, and skip. Spotify uses 200, 202, and 204 depending on whether the action was immediate or queued, so the client checks against a frozenset of success codes:

```python
_SPOTIFY_SUCCESS_CODES: frozenset[int] = frozenset({200, 202, 204})
```

Without this, a 204 response (no content) would be mistakenly treated as an error.

## Search and Playlist Management

The client exposes:

- `search(query, search_type, limit)` — finds tracks, albums, or artists
- `add_to_playlist(playlist_id, track_uri)` — appends a track URI to a playlist

Both methods call `_get_token()` on every invocation. Since `OAuthManager.get_valid_token()` automatically refreshes expired tokens, there is no token-caching concern at the client level.

## Error Propagation

All HTTP errors are raised directly via `httpx`. The client does not implement retry logic. Failed requests propagate to the tool layer where the agent runtime handles error messaging.

## Known Gaps

- **No playback state query**: The client can control playback but cannot read current playback state (what is playing, progress, device info). A `get_playback_state()` method is absent.
- **No OAuth initiation**: The client assumes tokens already exist in the store. The OAuth authorization flow is handled elsewhere (the settings UI), creating an implicit dependency not visible in this file.