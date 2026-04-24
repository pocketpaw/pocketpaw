---
{
  "title": "Spotify Tool Tests: Schema Validation, Auth Guards, and Playback Control",
  "summary": "Tests for PocketPaw's Spotify integration tools covering schema correctness, authentication failure handling, playback action validation, and successful API interactions. Ensures the tools surface clear errors when credentials are absent and route API calls correctly when authenticated.",
  "concepts": [
    "SpotifySearchTool",
    "SpotifyPlaybackTool",
    "SpotifyNowPlayingTool",
    "SpotifyPlaylistTool",
    "trust_level",
    "auth guards",
    "tool schema",
    "JSON Schema",
    "playback control"
  ],
  "categories": [
    "testing",
    "built-in tools",
    "third-party integrations",
    "test"
  ],
  "source_docs": [
    "7701748b26b6fcbc"
  ],
  "backlinks": null,
  "word_count": 452,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw includes built-in tools for Spotify integration, allowing an AI companion to search tracks, report now-playing status, control playback, and manage playlists. This test file validates the tool contracts at two levels: schema correctness (what the tool advertises) and runtime behaviour (what it actually does).

## Tool Schema Tests

`TestSpotifyToolSchemas` validates four tools — `SpotifySearchTool`, `SpotifyNowPlayingTool`, `SpotifyPlaybackTool`, and `SpotifyPlaylistTool` — against their expected `name`, `trust_level`, and `parameters` schemas. Schema tests matter because PocketPaw exposes these to the LLM as JSON Schema; if a required parameter is missing or a tool name is wrong, the LLM cannot invoke it correctly.

All four tools declare `trust_level = "standard"`, which means the agent can invoke them without elevated permission prompts. This is appropriate for read/write media operations that don't touch sensitive user data.

## Authentication Guards

Four `no_auth` tests confirm that attempting to use any Spotify tool without valid credentials returns a clear error rather than an unhandled exception. This matters in two real scenarios:

- The user has not connected their Spotify account.
- The OAuth token has expired and the refresh flow failed.

Without explicit auth guards, the tools would propagate an `AttributeError` or `NoneType` error from the Spotify SDK, which would appear to the user as an agent bug rather than a configuration issue.

## Playback Action Validation

`test_spotify_playback_invalid_action` confirms that passing an unsupported `action` string to the playback tool returns an error rather than silently doing nothing or crashing. Spotify's playback API supports a fixed set of actions; an open-ended string parameter without validation would produce confusing API errors.

## Missing Arguments

`test_spotify_playlist_add_missing_args` verifies that the playlist add tool rejects calls that omit required arguments. This guards against LLM hallucination — the model might attempt to invoke the tool with an incomplete argument set, and the tool must reject that cleanly rather than calling the Spotify API with a malformed request.

## Success Cases

Three success tests cover:

- Search returns matching tracks.
- Now-playing returns `None` gracefully when nothing is playing (not an error state).
- Playlist list returns the user's playlists.

The "nothing playing" test is an important edge case: a tool that raises when Spotify reports no active playback would break any companion workflow that checks music status as part of ambient context gathering.

## Known Gaps

The test file covers four tools but does not include explicit tests for OAuth token refresh flows or rate limit handling, both of which are common failure modes for third-party API integrations in long-running agent sessions.

```python
# Schema test pattern used for all four Spotify tools
def test_search_tool(self):
    from pocketpaw.tools.builtin.spotify import SpotifySearchTool
    tool = SpotifySearchTool()
    assert tool.name == "spotify_search"
    assert tool.trust_level == "standard"
    assert "query" in tool.parameters["properties"]
```
