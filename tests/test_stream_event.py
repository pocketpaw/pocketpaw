# Tests for StreamEvent token-by-token streaming integration
# Created: 2026-02-06

from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers — lightweight fakes for SDK types
# ---------------------------------------------------------------------------


class FakeStreamEvent:
    """Mimics claude_agent_sdk.StreamEvent with an .event dict."""

    def __init__(self, event: dict):
        self.event = event
        self._block_type = None


class FakeAssistantMessage:
    def __init__(self, content=None):
        self.content = content or []


class FakeTextBlock:
    def __init__(self, text: str):
        self.text = text


class FakeToolUseBlock:
    def __init__(self, name: str, input: dict | None = None):
        self.name = name
        self.input = input or {}


class FakeResultMessage:
    def __init__(self, result="", is_error=False):
        self.result = result
        self.is_error = is_error


def _make_sdk(settings=None):
    """Create a ClaudeAgentSDK with mocked SDK imports."""
    from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

    s = settings or MagicMock(
        tool_profile="full",
        tools_allow=[],
        tools_deny=[],
        bypass_permissions=True,
    )
    with patch.object(ClaudeAgentSDK, "_initialize"):
        sdk = ClaudeAgentSDK(s)

    # Wire up fake types
    sdk._sdk_available = True
    sdk._StreamEvent = FakeStreamEvent
    sdk._AssistantMessage = FakeAssistantMessage
    sdk._TextBlock = FakeTextBlock
    sdk._ToolUseBlock = FakeToolUseBlock
    sdk._ResultMessage = FakeResultMessage
    sdk._UserMessage = type("UserMessage", (), {})
    sdk._SystemMessage = type("SystemMessage", (), {})
    sdk._HookMatcher = lambda matcher, hooks: MagicMock()
    sdk._ClaudeAgentOptions = lambda **kw: MagicMock()
    return sdk


async def _collect(sdk, message="hi"):
    """Collect all AgentEvents from chat()."""
    events = []
    async for ev in sdk.chat(message):
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamEventHandling:
    """Tests for StreamEvent processing in claude_sdk.py."""

    async def test_text_delta_yields_message(self):
        """StreamEvent with text_delta yields AgentEvent(type='message')."""
        sdk = _make_sdk()

        async def fake_query(**kw):
            yield FakeStreamEvent({"type": "content_block_delta", "delta": {"text": "Hello"}})
            yield FakeStreamEvent({"type": "content_block_delta", "delta": {"text": " world"}})
            # Follow with an AssistantMessage (text should be skipped)
            yield FakeAssistantMessage([FakeTextBlock("Hello world")])

        sdk._query = fake_query

        events = await _collect(sdk)
        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 2
        assert messages[0].content == "Hello"
        assert messages[1].content == " world"

    async def test_thinking_delta_yields_thinking(self):
        """StreamEvent with thinking_delta yields AgentEvent(type='thinking')."""
        sdk = _make_sdk()

        async def fake_query(**kw):
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"thinking": "Let me reason..."}}
            )
            yield FakeAssistantMessage([])

        sdk._query = fake_query

        events = await _collect(sdk)
        thinking = [e for e in events if e.type == "thinking"]
        assert len(thinking) == 1
        assert thinking[0].content == "Let me reason..."

    async def test_thinking_done_on_block_stop(self):
        """content_block_stop for thinking block yields AgentEvent(type='thinking_done')."""
        sdk = _make_sdk()

        async def fake_query(**kw):
            ev = FakeStreamEvent({"type": "content_block_stop", "index": 0})
            ev._block_type = "thinking"
            yield ev
            yield FakeAssistantMessage([])

        sdk._query = fake_query

        events = await _collect(sdk)
        done = [e for e in events if e.type == "thinking_done"]
        assert len(done) == 1

    async def test_tool_use_start_yields_tool_use(self):
        """content_block_start with tool_use yields AgentEvent(type='tool_use')."""
        sdk = _make_sdk()

        async def fake_query(**kw):
            yield FakeStreamEvent(
                {
                    "type": "content_block_start",
                    "content_block": {"type": "tool_use", "name": "Bash"},
                }
            )
            yield FakeAssistantMessage([FakeToolUseBlock("Bash", {"command": "ls"})])

        sdk._query = fake_query

        events = await _collect(sdk)
        tool_events = [e for e in events if e.type == "tool_use"]
        # Should only get ONE tool_use (from StreamEvent), not a duplicate from AssistantMessage
        assert len(tool_events) == 1
        assert tool_events[0].metadata["name"] == "Bash"

    async def test_no_duplicate_text(self):
        """When StreamEvent deltas sent, AssistantMessage text is skipped."""
        sdk = _make_sdk()

        async def fake_query(**kw):
            yield FakeStreamEvent({"type": "content_block_delta", "delta": {"text": "Hi"}})
            yield FakeAssistantMessage([FakeTextBlock("Hi")])

        sdk._query = fake_query

        events = await _collect(sdk)
        messages = [e for e in events if e.type == "message"]
        # Only the StreamEvent delta, not the AssistantMessage duplicate
        assert len(messages) == 1
        assert messages[0].content == "Hi"

    async def test_no_duplicate_tool_use(self):
        """When StreamEvent announced tool, AssistantMessage tool_use is skipped."""
        sdk = _make_sdk()

        async def fake_query(**kw):
            yield FakeStreamEvent(
                {
                    "type": "content_block_start",
                    "content_block": {"type": "tool_use", "name": "Read"},
                }
            )
            yield FakeAssistantMessage([FakeToolUseBlock("Read", {"file": "foo.py"})])

        sdk._query = fake_query

        events = await _collect(sdk)
        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1

    async def test_fallback_without_stream_event(self):
        """With _StreamEvent = None, AssistantMessage text yields normally."""
        sdk = _make_sdk()
        sdk._StreamEvent = None  # Disable StreamEvent support

        async def fake_query(**kw):
            yield FakeAssistantMessage([FakeTextBlock("Fallback text")])

        sdk._query = fake_query

        events = await _collect(sdk)
        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 1
        assert messages[0].content == "Fallback text"

    async def test_multi_turn_state_reset(self):
        """_streamed_via_events resets between AssistantMessages."""
        sdk = _make_sdk()

        async def fake_query(**kw):
            # Turn 1: stream via events
            yield FakeStreamEvent({"type": "content_block_delta", "delta": {"text": "Turn1"}})
            yield FakeAssistantMessage([FakeTextBlock("Turn1")])
            # Turn 2: no stream events → AssistantMessage text should yield
            yield FakeAssistantMessage([FakeTextBlock("Turn2")])

        sdk._query = fake_query

        events = await _collect(sdk)
        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 2
        assert messages[0].content == "Turn1"
        assert messages[1].content == "Turn2"


class TestLoopThinkingIntegration:
    """Tests for thinking event handling in AgentLoop."""

    async def test_loop_thinking_publishes_system_event(self):
        """Loop publishes thinking as SystemEvent, not OutboundMessage."""
        from pocketclaw.bus import Channel, InboundMessage

        with (
            patch("pocketclaw.agents.loop.get_settings") as mock_settings,
            patch("pocketclaw.agents.loop.get_message_bus") as mock_bus_fn,
            patch("pocketclaw.agents.loop.get_memory_manager") as mock_mem_fn,
            patch("pocketclaw.agents.loop.AgentContextBuilder") as mock_builder_cls,
        ):
            mock_settings.return_value = MagicMock(
                agent_backend="claude_agent_sdk",
                max_concurrent_conversations=5,
            )
            bus = MagicMock()
            bus.publish_system = AsyncMock()
            bus.publish_outbound = AsyncMock()
            mock_bus_fn.return_value = bus
            mem = MagicMock()
            mem.add_to_session = AsyncMock()
            mem.get_session_history = AsyncMock(return_value=[])
            mem.get_compacted_history = AsyncMock(return_value=[])
            mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
            mock_mem_fn.return_value = mem
            mock_builder_cls.return_value.build_system_prompt = AsyncMock(
                return_value="System Prompt"
            )

            from pocketclaw.agents.loop import AgentLoop

            loop = AgentLoop()

            # Mock router to yield thinking + done
            router = MagicMock()

            async def fake_run(msg, *, system_prompt=None, history=None):
                yield {"type": "thinking", "content": "Deep thought", "metadata": {}}
                yield {"type": "thinking_done", "content": "", "metadata": {}}
                yield {"type": "done", "content": "", "metadata": {}}

            router.run = fake_run
            loop._router = router

            msg = InboundMessage(
                channel=Channel.WEBSOCKET,
                sender_id="user1",
                chat_id="test",
                content="hello",
            )
            await loop._process_message(msg)

            # Check that publish_system was called with thinking events
            system_calls = bus.publish_system.call_args_list
            event_types = [c.args[0].event_type for c in system_calls]
            assert "thinking" in event_types
            assert "thinking_done" in event_types

            # Check that thinking content was NOT sent as OutboundMessage
            outbound_calls = bus.publish_outbound.call_args_list
            for call in outbound_calls:
                msg_obj = call.args[0]
                assert "Deep thought" not in (msg_obj.content or "")

    async def test_loop_thinking_not_in_memory(self):
        """Thinking content is excluded from full_response stored in memory."""
        from pocketclaw.bus import Channel, InboundMessage

        with (
            patch("pocketclaw.agents.loop.get_settings") as mock_settings,
            patch("pocketclaw.agents.loop.get_message_bus") as mock_bus_fn,
            patch("pocketclaw.agents.loop.get_memory_manager") as mock_mem_fn,
            patch("pocketclaw.agents.loop.AgentContextBuilder") as mock_builder_cls,
        ):
            mock_settings.return_value = MagicMock(
                agent_backend="claude_agent_sdk",
                max_concurrent_conversations=5,
            )
            bus = MagicMock()
            bus.publish_system = AsyncMock()
            bus.publish_outbound = AsyncMock()
            mock_bus_fn.return_value = bus
            mem = MagicMock()
            mem.add_to_session = AsyncMock()
            mem.get_session_history = AsyncMock(return_value=[])
            mem.get_compacted_history = AsyncMock(return_value=[])
            mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
            mock_mem_fn.return_value = mem
            mock_builder_cls.return_value.build_system_prompt = AsyncMock(
                return_value="System Prompt"
            )

            from pocketclaw.agents.loop import AgentLoop

            loop = AgentLoop()

            router = MagicMock()

            async def fake_run(msg, *, system_prompt=None, history=None):
                yield {"type": "thinking", "content": "secret reasoning", "metadata": {}}
                yield {"type": "message", "content": "Hello!", "metadata": {}}
                yield {"type": "done", "content": "", "metadata": {}}

            router.run = fake_run
            loop._router = router

            msg = InboundMessage(
                channel=Channel.WEBSOCKET,
                sender_id="user1",
                chat_id="test",
                content="hi",
            )
            await loop._process_message(msg)

            # Memory should store "Hello!" but NOT "secret reasoning"
            assistant_calls = [
                c for c in mem.add_to_session.call_args_list if c.kwargs.get("role") == "assistant"
            ]
            assert len(assistant_calls) == 1
            stored = assistant_calls[0].kwargs["content"]
            assert "Hello!" in stored
            assert "secret reasoning" not in stored
