---
{
  "title": "Spotify Tools: Search, Now Playing, Playback Control, and Playlist Management",
  "summary": "Four `BaseTool` subclasses — `spotify_search`, `spotify_now_playing`, `spotify_playback`, and `spotify_playlist` — wrap `SpotifyClient` to give agents full Spotify control: catalog search, current track status, transport controls, and playlist operations. All raise `RuntimeError` for auth failures, which surfaces as a clean error rather than a stack trace.",
  "concepts": [
    "SpotifySearchTool",
    "SpotifyNowPlayingTool",
    "SpotifyPlaybackTool",
    "SpotifyPlaylistTool",
    "SpotifyClient",
    "OAuth",
    "playback_control",
    "trust_level_standard",
    "RuntimeError",
    "media_integrations"
  ],
  "categories": [
    "tools",
    "media-integrations",
    "spotify",
    "playback-control"
  ],
  "source_docs": [
    "cd029ac0ed0d2aef"
  ],
  "backlinks": null,
  "word_count": 606,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`spotify.py` (Phase 4 Media Integrations) defines four tools that together cover the Spotify interaction surface an agent needs: discovery, status, control, and curation. Each tool delegates to a shared `SpotifyClient` integration that handles OAuth token management and API calls, keeping the tool layer thin.

## SpotifySearchTool

```python
class SpotifySearchTool(BaseTool):
    async def execute(self, query, type="track", limit=5) -> str:
```

Search covers three entity types: `track`, `album`, and `artist`. Each type produces a differently shaped result — tracks include duration and album name, albums include track count, artists include follower count and genres. The output format is matched to the entity type in a single `if/elif/elif` block rather than three separate tools, keeping the surface area smaller while still producing structured, readable output.

The `limit` defaults to 5 (not 10 like Reddit) because Spotify results are richer per item — each track entry spans three lines with URI, artist, album, and duration, so 10 results would be verbose.

## SpotifyNowPlayingTool

```python
async def execute(self) -> str:
    result = await client.now_playing()
    if not result:
        return "Nothing is currently playing on Spotify."
```

No parameters — this is a pure status query. The empty-result check prevents a `None` dereference: when Spotify reports no active playback, `now_playing()` returns `None` (or an empty dict). The tool converts this to a human-readable "nothing playing" message.

Progress and duration are displayed as `M:SS` format rather than raw milliseconds, which is the format users expect. The conversion (`progress_ms // 1000 → divmod(seconds, 60)`) happens in the tool layer so the LLM never sees raw milliseconds.

## SpotifyPlaybackTool

```python
valid_actions = {"play", "pause", "next", "prev", "volume"}
if action not in valid_actions:
    return self._error(f"Unknown action '{action}'.")
```

The allowlist validation happens before the `SpotifyClient` call. Without it, an invalid action string would reach the API and return a cryptic HTTP error. The volume action clamps `volume_percent` to `[0, 100]` before passing it to the client — `max(0, min(100, volume_percent))` — because Spotify's API returns a 400 for out-of-range values and the error message is not user-friendly.

The `uri` parameter is optional for `play`: calling `play` without a URI resumes the current track, while calling it with a URI starts a specific track. Both code paths funnel through the same `client.playback_control(action, **kwargs)` call, keeping the interface consistent.

## SpotifyPlaylistTool

```python
if action == "add":
    if not playlist_id or not track_uri:
        return self._error("Both playlist_id and track_uri are required for 'add'.")
```

`spotify_playlist` handles two distinct operations behind one tool: listing playlists and adding a track. The parameter validation for `add` is explicit: both `playlist_id` and `track_uri` must be present, and the error message names both missing fields. This matters because an LLM that calls `add` without a `track_uri` gets a precise error it can correct on retry rather than a generic failure.

The default action is `list`, making a bare `spotify_playlist {}` call safe and useful.

## Error Handling: RuntimeError vs. Exception

All four tools catch `RuntimeError` separately from `Exception`:

```python
except RuntimeError as e:
    return self._error(str(e))
except Exception as e:
    return self._error(f"Spotify search failed: {e}")
```

`RuntimeError` is raised by `SpotifyClient` specifically for configuration errors (missing OAuth token, unconfigured client ID). Catching it separately allows the tool to surface a clean "not configured" message, while the broad `Exception` catch handles unexpected API failures. This two-level catch is a documented contract with the integration layer.

## Known Gaps

- OAuth token refresh is handled entirely inside `SpotifyClient` — if refresh fails mid-session, the tool will surface a RuntimeError with no retry logic.
- Queue management (add to queue, view queue) is not implemented.
- No support for podcast episodes, which have a different URI scheme from tracks.
