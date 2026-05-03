# RedditConnector + SpotifyConnector — Phase 1 PR-7 contract tests.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.connectors.adapters.reddit import RedditConnector
from pocketpaw.connectors.adapters.spotify import SpotifyConnector
from pocketpaw.connectors.protocol import (
    ConnectorStatus,
    ExecutionMode,
    TrustLevel,
)


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------


def test_reddit_metadata():
    c = RedditConnector()
    assert c.name == "reddit"
    assert c.display_name == "Reddit"


@pytest.mark.asyncio
async def test_reddit_action_names():
    c = RedditConnector()
    schemas = await c.actions()
    assert sorted(s.name for s in schemas) == [
        "reddit_read",
        "reddit_search",
        "reddit_trending",
    ]
    for s in schemas:
        assert s.execution_mode is ExecutionMode.CLOUD
        assert s.trust_level is TrustLevel.AUTO


@pytest.mark.asyncio
async def test_reddit_no_default_widgets():
    """No workspace-level default — Reddit recipes need a subreddit param."""
    c = RedditConnector()
    assert await c.widgets() == []


@pytest.mark.asyncio
async def test_reddit_search_caps_limit():
    c = RedditConnector()
    with patch(
        "pocketpaw.integrations.reddit.RedditClient.search",
        new=AsyncMock(return_value=[]),
    ) as mock:
        await c.execute("reddit_search", {"query": "x", "limit": 999})
    mock.assert_called_once_with("x", subreddit=None, limit=25)


@pytest.mark.asyncio
async def test_reddit_health_app_only():
    """App-only mode flips connected without a probe."""
    c = RedditConnector()
    await c.connect("default", {})
    h = await c.health()
    assert h.ok is True
    assert h.status is ConnectorStatus.CONNECTED


def test_reddit_registry_wiring():
    from pocketpaw.connectors.registry import _create_native_adapter

    assert isinstance(_create_native_adapter("reddit"), RedditConnector)


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------


def test_spotify_metadata():
    c = SpotifyConnector()
    assert c.name == "spotify"
    assert c.display_name == "Spotify"


@pytest.mark.asyncio
async def test_spotify_action_names():
    c = SpotifyConnector()
    schemas = await c.actions()
    assert sorted(s.name for s in schemas) == [
        "spotify_now_playing",
        "spotify_playback",
        "spotify_playlist",
        "spotify_search",
    ]
    for s in schemas:
        assert s.execution_mode is ExecutionMode.CLOUD


@pytest.mark.asyncio
async def test_spotify_trust_levels():
    c = SpotifyConnector()
    by_name = {s.name: s for s in await c.actions()}
    assert by_name["spotify_search"].trust_level is TrustLevel.AUTO
    assert by_name["spotify_now_playing"].trust_level is TrustLevel.AUTO
    assert by_name["spotify_playlist"].trust_level is TrustLevel.AUTO
    assert by_name["spotify_playback"].trust_level is TrustLevel.CONFIRM


@pytest.mark.asyncio
async def test_spotify_widget_recipes():
    c = SpotifyConnector()
    recipes = await c.widgets()
    assert len(recipes) == 1
    assert recipes[0].title == "Now Playing"
    assert recipes[0].action == "spotify_now_playing"


@pytest.mark.asyncio
async def test_spotify_search_caps_limit():
    c = SpotifyConnector()
    with patch(
        "pocketpaw.integrations.spotify.SpotifyClient.search",
        new=AsyncMock(return_value=[]),
    ) as mock:
        await c.execute("spotify_search", {"query": "x", "limit": 999})
    mock.assert_called_once_with("x", type="track", limit=20)


@pytest.mark.asyncio
async def test_spotify_now_playing_handles_nothing_playing():
    c = SpotifyConnector()
    with patch(
        "pocketpaw.integrations.spotify.SpotifyClient.now_playing",
        new=AsyncMock(return_value=None),
    ):
        result = await c.execute("spotify_now_playing", {})
    assert result.success is True
    assert result.data == {"playing": False}


def test_spotify_registry_wiring():
    from pocketpaw.connectors.registry import _create_native_adapter

    assert isinstance(_create_native_adapter("spotify"), SpotifyConnector)
