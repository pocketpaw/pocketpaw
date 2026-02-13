# Tests for Spotify integration (Sprint 27)

from unittest.mock import AsyncMock, patch


class TestSpotifyToolSchemas:
    """Test Spotify tool properties and schemas."""

    def test_search_tool(self):
        from pocketclaw.tools.builtin.spotify import SpotifySearchTool

        tool = SpotifySearchTool()
        assert tool.name == "spotify_search"
        assert tool.trust_level == "standard"
        assert "query" in tool.parameters["properties"]

    def test_now_playing_tool(self):
        from pocketclaw.tools.builtin.spotify import SpotifyNowPlayingTool

        tool = SpotifyNowPlayingTool()
        assert tool.name == "spotify_now_playing"
        assert tool.trust_level == "standard"

    def test_playback_tool(self):
        from pocketclaw.tools.builtin.spotify import SpotifyPlaybackTool

        tool = SpotifyPlaybackTool()
        assert tool.name == "spotify_playback"
        assert "action" in tool.parameters["properties"]
        assert "action" in tool.parameters["required"]

    def test_playlist_tool(self):
        from pocketclaw.tools.builtin.spotify import SpotifyPlaylistTool

        tool = SpotifyPlaylistTool()
        assert tool.name == "spotify_playlist"
        assert "action" in tool.parameters["properties"]


async def test_spotify_search_no_auth():
    from pocketclaw.tools.builtin.spotify import SpotifySearchTool

    tool = SpotifySearchTool()
    with patch(
        "pocketclaw.integrations.spotify.SpotifyClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(query="bohemian rhapsody")
    assert result.startswith("Error:")
    assert "authenticated" in result.lower()


async def test_spotify_now_playing_no_auth():
    from pocketclaw.tools.builtin.spotify import SpotifyNowPlayingTool

    tool = SpotifyNowPlayingTool()
    with patch(
        "pocketclaw.integrations.spotify.SpotifyClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute()
    assert result.startswith("Error:")


async def test_spotify_playback_invalid_action():
    from pocketclaw.tools.builtin.spotify import SpotifyPlaybackTool

    tool = SpotifyPlaybackTool()
    result = await tool.execute(action="dance")
    assert result.startswith("Error:")
    assert "Unknown action" in result


async def test_spotify_playback_no_auth():
    from pocketclaw.tools.builtin.spotify import SpotifyPlaybackTool

    tool = SpotifyPlaybackTool()
    with patch(
        "pocketclaw.integrations.spotify.SpotifyClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(action="play")
    assert result.startswith("Error:")


async def test_spotify_playlist_add_missing_args():
    from pocketclaw.tools.builtin.spotify import SpotifyPlaylistTool

    tool = SpotifyPlaylistTool()
    # Mock _get_token so we get past auth, test arg validation
    with patch(
        "pocketclaw.integrations.spotify.SpotifyClient._get_token",
        new_callable=AsyncMock,
        return_value="fake",
    ):
        result = await tool.execute(action="add")
    assert result.startswith("Error:")
    assert "required" in result.lower()


async def test_spotify_search_success():
    from pocketclaw.tools.builtin.spotify import SpotifySearchTool

    tool = SpotifySearchTool()

    mock_results = [
        {
            "name": "Bohemian Rhapsody",
            "id": "track1",
            "uri": "spotify:track:track1",
            "type": "track",
            "artists": "Queen",
            "album": "A Night at the Opera",
            "duration_ms": 354000,
        }
    ]

    with patch(
        "pocketclaw.integrations.spotify.SpotifyClient._get_token",
        new_callable=AsyncMock,
        return_value="fake",
    ):
        with patch(
            "pocketclaw.integrations.spotify.SpotifyClient.search",
            new_callable=AsyncMock,
            return_value=mock_results,
        ):
            result = await tool.execute(query="bohemian rhapsody")

    assert "Bohemian Rhapsody" in result
    assert "Queen" in result


async def test_spotify_now_playing_nothing():
    from pocketclaw.tools.builtin.spotify import SpotifyNowPlayingTool

    tool = SpotifyNowPlayingTool()

    with patch(
        "pocketclaw.integrations.spotify.SpotifyClient._get_token",
        new_callable=AsyncMock,
        return_value="fake",
    ):
        with patch(
            "pocketclaw.integrations.spotify.SpotifyClient.now_playing",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await tool.execute()

    assert "Nothing" in result


async def test_spotify_playlist_list_success():
    from pocketclaw.tools.builtin.spotify import SpotifyPlaylistTool

    tool = SpotifyPlaylistTool()

    mock_playlists = [
        {
            "name": "Chill Vibes",
            "id": "pl1",
            "uri": "spotify:playlist:pl1",
            "tracks": 42,
            "public": True,
        }
    ]

    with patch(
        "pocketclaw.integrations.spotify.SpotifyClient._get_token",
        new_callable=AsyncMock,
        return_value="fake",
    ):
        with patch(
            "pocketclaw.integrations.spotify.SpotifyClient.get_playlists",
            new_callable=AsyncMock,
            return_value=mock_playlists,
        ):
            result = await tool.execute(action="list")

    assert "Chill Vibes" in result
    assert "42 tracks" in result
