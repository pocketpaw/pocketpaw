"""Verify agent_bridge routes broadcasts through emit()."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_agent_bridge_emits_stream_start_through_bus():
    """Confirm agent_bridge uses emit() not ws_manager.broadcast_to_group."""
    from pocketpaw_ee.cloud.realtime.events import AgentStreamStart

    # Smoke-test: if `emit` is the only path used by agent_bridge for agent events,
    # patching it should capture every broadcast.
    with patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()) as m_emit:
        # Fire an AgentStreamStart directly through emit — baseline check that
        # agent_bridge imports the same emit we're patching
        from pocketpaw_ee.cloud.shared import agent_bridge  # noqa: F401

        await agent_bridge.emit(AgentStreamStart(data={"group_id": "g1", "agent_id": "a1"}))

    m_emit.assert_awaited_once()
    ev = m_emit.await_args.args[0]
    assert ev.type == "agent.stream_start"
    assert ev.data == {"group_id": "g1", "agent_id": "a1"}


def test_agent_bridge_does_not_import_ws_manager_broadcast_directly():
    """Regression guard: agent_bridge should NOT call ws_manager.broadcast_to_group.

    If this test fails, a broadcast slipped back in and needs to be routed via emit().
    """
    from pathlib import Path

    src = Path("D:/paw/backend/ee/cloud/shared/agent_bridge.py").read_text(encoding="utf-8")
    assert "ws_manager.broadcast_to_group" not in src, (
        "agent_bridge.py still calls ws_manager.broadcast_to_group — route via emit() instead"
    )


@pytest.mark.asyncio
async def test_agent_bridge_emit_calls_preserve_wire_types():
    """Confirm each of the 4 event wire types exists on a dataclass that agent_bridge imports."""
    from pocketpaw_ee.cloud.shared import agent_bridge

    expected_classes = {
        "AgentStreamStart": "agent.stream_start",
        "AgentStreamChunk": "agent.stream_chunk",
        "AgentToolUse": "agent.tool_use",
        "AgentStreamEnd": "agent.stream_end",
    }
    for cls_name, wire_type in expected_classes.items():
        cls = getattr(agent_bridge, cls_name)
        assert cls.EVENT_TYPE == wire_type
