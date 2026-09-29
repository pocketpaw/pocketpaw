# tests/cloud/surface/test_chat_handler.py — Chat surface handler.
#
# Two guarantees:
#   1. Happy path — the preamble carries ``<chat-snapshot sessions="N" />``
#      with the sessions service's server-side count. This is the hint the
#      agent reads to answer "how many threads do I have?".
#   2. Failure path — the preamble falls back to
#      ``(session count unavailable)`` when the sessions service raises,
#      so a transient DB hiccup never breaks the chat send.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import chat as chat_handler

pytestmark = pytest.mark.asyncio


async def test_chat_handler_emits_session_count() -> None:
    """Sessions service counting N rows -> ``<chat-snapshot sessions="N" />``."""
    count = AsyncMock(return_value=3)
    with patch("pocketpaw_ee.cloud.sessions.service.count_for_user", new=count):
        preamble = (await chat_handler.build_preamble("w1", "u1", SurfaceMeta())).text

    assert '<surface kind="chat"' in preamble
    assert '<chat-snapshot sessions="3" />' in preamble
    # The unavailable-fallback tag must NOT appear on the happy path —
    # if both shipped the agent would see contradictory hints.
    assert "session count unavailable" not in preamble
    # Counted server-side for the chat surface, never listed and len()'d.
    count.assert_awaited_once_with("w1", "u1", surface="chat")


async def test_chat_handler_falls_back_when_lister_raises() -> None:
    """Any exception from the sessions service yields the unavailable fallback."""
    with patch(
        "pocketpaw_ee.cloud.sessions.service.count_for_user",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        preamble = (await chat_handler.build_preamble("w1", "u1", SurfaceMeta())).text

    assert '<surface kind="chat"' in preamble
    assert "<chat-snapshot>(session count unavailable)</chat-snapshot>" in preamble
