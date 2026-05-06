"""Tests for the ``_publish_pocket_event`` hook in ``agents/loop.py``.

The hook scans every tool_result body for two pocket-event shapes:
  1. Legacy local mutation instruction: ``{"pocket_event": "...", ...}``
  2. Cloud CLI response: ``{"ok": true, "pocket": {...}}``

When it sees either, it publishes a ``pocket_mutation`` (or
``pocket_created``) SystemEvent on the message bus so the chat SSE
bridge fans it out to the active session — that's the channel
paw-enterprise reads to refresh the pocket canvas live.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from pocketpaw.agents.loop import _publish_pocket_event


def _bus_mock() -> AsyncMock:
    """A minimal MessageBus stub that records every publish_system call."""
    bus = AsyncMock()
    bus.publish_system = AsyncMock()
    return bus


@pytest.mark.asyncio
async def test_cloud_add_widget_response_emits_pocket_mutation():
    bus = _bus_mock()
    payload = {
        "ok": True,
        "pocket": {
            "_id": "p-abc",
            "name": "My Pocket",
            "widgets": [{"_id": "w-1", "name": "Revenue"}],
        },
        "pocket_id": "p-abc",
    }
    content = json.dumps(payload)

    await _publish_pocket_event(bus, content, session_key="ws:room:1")

    bus.publish_system.assert_awaited_once()
    call = bus.publish_system.await_args.args[0]
    assert call.event_type == "pocket_mutation"
    mutation = call.data["mutation"]
    assert mutation["action"] == "replace"
    assert mutation["pocket_id"] == "p-abc"
    assert mutation["pocket"] == payload["pocket"]
    assert call.data["session_key"] == "ws:room:1"


@pytest.mark.asyncio
async def test_cloud_response_falls_back_to_pocket_id_inside_pocket():
    """If the top-level ``pocket_id`` isn't present we mine it out of the
    embedded pocket document so older CLI shapes still work."""
    bus = _bus_mock()
    payload = {"ok": True, "pocket": {"_id": "p-xyz", "name": "X"}}
    content = json.dumps(payload)

    await _publish_pocket_event(bus, content, session_key="s")

    call = bus.publish_system.await_args.args[0]
    assert call.data["mutation"]["pocket_id"] == "p-xyz"


@pytest.mark.asyncio
async def test_cloud_get_pocket_does_not_emit_when_no_change():
    """Read-only output still lands as ``{ok: true, pocket: ...}``. We
    DO emit because the chat session may have stale state — the replace
    is idempotent and cheap. (Asserts current behaviour; revisit if it
    causes UI thrash.)"""
    bus = _bus_mock()
    content = json.dumps({"ok": True, "pocket": {"_id": "p1", "name": "X"}})
    await _publish_pocket_event(bus, content, session_key="s")
    bus.publish_system.assert_awaited_once()


@pytest.mark.asyncio
async def test_cloud_error_response_does_not_emit():
    bus = _bus_mock()
    content = json.dumps({"ok": False, "error": "AssertionError: ..."})
    await _publish_pocket_event(bus, content, session_key="s")
    bus.publish_system.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_tool_output_skipped_cheaply():
    bus = _bus_mock()
    await _publish_pocket_event(bus, "Just some random text", session_key="s")
    await _publish_pocket_event(bus, "{\"unrelated\": \"data\"}", session_key="s")
    bus.publish_system.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_pocket_event_mutation_still_works():
    """Don't regress the existing local-mode shape."""
    bus = _bus_mock()
    content = json.dumps(
        {
            "pocket_event": "mutation",
            "mutation": {"action": "add_widget", "pocket_id": "p1", "widget": {}},
        }
    )
    await _publish_pocket_event(bus, content, session_key="s")
    call = bus.publish_system.await_args.args[0]
    assert call.event_type == "pocket_mutation"
    assert call.data["mutation"]["action"] == "add_widget"


@pytest.mark.asyncio
async def test_handles_mixed_text_around_json():
    """The hook brace-matches the first JSON object, so leading/trailing
    text doesn't break it. Subprocess agents often print prose around
    the JSON line."""
    bus = _bus_mock()
    content = (
        "Calling cloud_add_widget...\n"
        + json.dumps({"ok": True, "pocket": {"_id": "pid"}})
        + "\n\nDone."
    )
    await _publish_pocket_event(bus, content, session_key="s")
    bus.publish_system.assert_awaited_once()
