"""Trace propagation tests for AgentLoop integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.bus import Channel, InboundMessage


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_loop_emits_trace_lifecycle_and_normalized_token_usage(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
):
    mock_bus = MagicMock()
    mock_bus.publish_outbound = AsyncMock()
    mock_bus.publish_system = AsyncMock()

    mock_memory = MagicMock()
    mock_memory.add_to_session = AsyncMock()
    mock_memory.get_session_history = AsyncMock(return_value=[])
    mock_memory.get_compacted_history = AsyncMock(return_value=[])
    mock_memory.resolve_session_key = AsyncMock(side_effect=lambda value: value)

    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    router = MagicMock()

    async def run_with_usage(message, *, system_prompt=None, history=None, session_key=None):
        _ = message, system_prompt, history, session_key
        yield AgentEvent(type="message", content="hello")
        yield AgentEvent(
            type="token_usage",
            content="",
            metadata={
                "backend": "claude_agent_sdk",
                "model": "claude-3-haiku",
                "input_tokens": 12,
                "output_tokens": 8,
                "cached_input_tokens": 2,
                "total_cost_usd": 0.004,
            },
        )
        yield AgentEvent(type="done", content="")

    router.run = run_with_usage
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    builder = mock_builder_cls.return_value
    builder.build_system_prompt = AsyncMock(return_value="System prompt")
    builder.bootstrap.get_context = AsyncMock(return_value=MagicMock(to_identity_block=lambda: ""))

    class Tracker:
        def __init__(self) -> None:
            self.total = 0.0

        def get_summary(self, since=None):
            _ = since
            return {"total_cost_usd": self.total}

        def record(self, *, total_cost_usd=None, **kwargs):
            _ = kwargs
            self.total += float(total_cost_usd or 0.0)

    tracker = Tracker()

    with (
        patch("pocketpaw.agents.loop.get_settings") as mock_get_settings,
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
        patch("pocketpaw.agents.loop.usage_tracker_module.get_usage_tracker", return_value=tracker),
    ):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.injection_scan_llm = False
        settings.pii_scan_enabled = False
        settings.pii_scan_memory = False
        settings.welcome_hint_enabled = False
        settings.file_jail_path = "."
        settings.compaction_recent_window = 20
        settings.compaction_char_budget = 30000
        settings.compaction_summary_chars = 1000
        settings.compaction_llm_summarize = False
        settings.tool_profile = "full"
        settings.voice_reply_enabled = False
        settings.memory_backend = "file"
        settings.file_auto_learn = False
        settings.mem0_auto_learn = False
        settings.budget_monthly_usd = 100.0
        settings.budget_warning_threshold = 0.8
        settings.budget_auto_pause = True
        settings.budget_reset_day = 1
        settings.budget_paused = False
        settings.budget_override_usd = None
        settings.budget_override_reason = ""
        settings.budget_override_expires_at = None
        mock_get_settings.return_value = settings
        mock_settings_cls.load.return_value = settings

        loop = AgentLoop()
        msg = InboundMessage(
            channel=Channel.CLI,
            sender_id="user1",
            chat_id="chat1",
            content="trace me",
        )

        await loop._process_message(msg)

    system_events = [call.args[0] for call in mock_bus.publish_system.call_args_list]
    trace_start = [event for event in system_events if event.event_type == "trace_start"]
    trace_end = [event for event in system_events if event.event_type == "trace_end"]
    token_usage = [event for event in system_events if event.event_type == "token_usage"]

    assert len(trace_start) == 1
    assert len(trace_end) == 1
    assert len(token_usage) == 1

    trace_id = trace_start[0].data["trace_id"]
    assert trace_end[0].data["trace_id"] == trace_id
    assert token_usage[0].data["trace_id"] == trace_id
    assert token_usage[0].data["input"] == 12
    assert token_usage[0].data["output"] == 8

    outbound_messages = [call.args[0] for call in mock_bus.publish_outbound.call_args_list]
    stream_chunks = [m for m in outbound_messages if m.is_stream_chunk]
    stream_end = [m for m in outbound_messages if m.is_stream_end]

    assert stream_chunks
    assert stream_end
    assert stream_chunks[0].metadata["trace_id"] == trace_id
    assert stream_end[0].metadata["trace_id"] == trace_id
