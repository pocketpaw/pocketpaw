# tests/cloud/surface/test_chat_handler.py — Chat surface handler.
#
# Created: 2026-05-24 — Two guarantees:
#   1. Happy path — the preamble carries ``<chat-snapshot sessions="N" />``
#      when the sessions service returns a real listing. This is the
#      hint the agent reads to answer "how many threads do I have?".
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
    """Sessions service returning N rows -> ``<chat-snapshot sessions="N" />``."""
    fake_rows = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    with patch(
        "pocketpaw_ee.cloud.sessions.service.list_for_user",
        new=AsyncMock(return_value=fake_rows),
    ):
        preamble = await chat_handler.build_preamble("w1", "u1", SurfaceMeta())

    assert '<surface kind="chat"' in preamble
    assert '<chat-snapshot sessions="3" />' in preamble
    # The unavailable-fallback tag must NOT appear on the happy path —
    # if both shipped the agent would see contradictory hints.
    assert "session count unavailable" not in preamble


async def test_chat_handler_falls_back_when_lister_raises() -> None:
    """Any exception from the sessions service yields the unavailable fallback."""
    with patch(
        "pocketpaw_ee.cloud.sessions.service.list_for_user",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        preamble = await chat_handler.build_preamble("w1", "u1", SurfaceMeta())

    assert '<surface kind="chat"' in preamble
    assert "<chat-snapshot>(session count unavailable)</chat-snapshot>" in preamble
