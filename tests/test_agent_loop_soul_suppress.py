"""AgentLoop must honor a per-turn flag that suppresses global soul observe.

This is the hook cloud runs use so the default PocketPaw soul does not
evolve from interactions that were actually directed at a specific
workspace agent. OSS paths never set the flag, so default behavior is
unchanged -- verified by the second test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_soul_observe_skipped_when_flag_set(monkeypatch):
    from pocketpaw.agents import loop as loop_mod

    # Build a minimal AgentLoop with a soul manager mock. We only exercise
    # the branch that decides whether to spawn _soul_observe_and_emit.
    al = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    al._soul_manager = MagicMock()
    al._soul_observe_and_emit = AsyncMock()

    message = MagicMock()
    message.content = "hello"
    message.metadata = {"suppress_global_soul_observe": True}
    session_key = "cloud:g:a"

    await al._maybe_observe_soul(message, "full response", session_key, cancelled=False)

    al._soul_observe_and_emit.assert_not_called()


@pytest.mark.asyncio
async def test_soul_observe_runs_when_flag_absent():
    from pocketpaw.agents import loop as loop_mod

    al = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    al._soul_manager = MagicMock()
    al._soul_observe_and_emit = AsyncMock()
    al._background_tasks = set()

    message = MagicMock()
    message.content = "hello"
    message.metadata = {}
    session_key = "websocket:abc"

    await al._maybe_observe_soul(message, "full response", session_key, cancelled=False)
    # helper fires observation as a background task; drain the event loop so
    # the AsyncMock records the await before we assert.
    await asyncio.sleep(0)

    al._soul_observe_and_emit.assert_awaited_once_with("hello", "full response", session_key)
