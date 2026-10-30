# tests/cloud/test_pocket_mutation_sse.py
# Created: 2026-05-21 — diagnostic for the "canvas doesn't update, I have
# to refresh" bug. Verifies that a successful agent-mode edit pushes a
# ``pocket_mutation`` SSE event onto the active sink — the event the
# paw-enterprise canvas re-renders from.
"""Does an agent-mode edit emit the pocket_mutation SSE event?"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


async def _make_flex_pocket() -> Any:
    from pocketpaw_ee.cloud.models.pocket import Pocket

    spec = {
        "version": "1.0",
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "props": {"direction": "column"},
            "children": [
                {"id": "n_head0000", "type": "heading", "props": {"text": "Hi"}},
            ],
        },
    }
    doc = Pocket(workspace="w1", name="SSE", owner="u1", visibility="workspace", rippleSpec=spec)
    await doc.insert()
    return doc


@pytest.fixture
def agent_identity():
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        detach_agent_identity,
    )

    tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    try:
        yield
    finally:
        detach_agent_identity(tokens)


@pytest.mark.asyncio
async def test_agent_mode_edit_pushes_pocket_mutation(mongo_db, agent_identity):
    """A successful agent-mode add_node edit must push a ``pocket_mutation``
    event onto the active SSE sink — that is what the canvas re-renders
    from. If this fails, the canvas never updates and the user must
    refresh (the live-update bug)."""
    from pocketpaw_ee.agent.pocket_specialist.adapters import EditAgentModeAdapter
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )

    from pocketpaw.config import Settings

    doc = await _make_flex_pocket()
    pocket_id = str(doc.id)

    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        out = await EditAgentModeAdapter().edit(
            PocketSpecialistEditInput(
                pocket_id=pocket_id,
                intent="add a stat tile",
                ops=[
                    {
                        "op": "add_node",
                        "args": {
                            "parent_id": "n_root0000",
                            "spec": {"type": "stat", "props": {"value": "42"}},
                        },
                    }
                ],
            ),
            workspace_id="w1",
            user_id="u1",
            settings=Settings(),
        )
    finally:
        detach_sse_event_sink(token)

    assert out.ok is True, f"edit failed: {out.error} / {out.warnings}"

    events: list[tuple[str, dict]] = []
    while not sink.empty():
        events.append(sink.get_nowait())
    names = [n for n, _ in events]
    assert "pocket_mutation" in names, (
        "agent-mode edit applied an op but pushed NO pocket_mutation SSE "
        f"event — the canvas cannot update live. Events seen: {names}"
    )
