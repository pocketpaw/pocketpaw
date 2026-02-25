# Tests for Unified Agent Loop
# Updated for AgentEvent-based architecture (no more dict chunks)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.bus import Channel, InboundMessage


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    return bus


@pytest.fixture
def mock_memory():
    mem = MagicMock()
    mem.add_to_session = AsyncMock()
    mem.get_session_history = AsyncMock(return_value=[])
    mem.get_compacted_history = AsyncMock(return_value=[])
    mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    return mem


@pytest.fixture
def mock_router():
    """Mock AgentRouter that yields AgentEvent objects."""
    router = MagicMock()

    async def mock_run(message, *, system_prompt=None, history=None, session_key=None):
        yield AgentEvent(type="message", content="Hello ")
        yield AgentEvent(type="message", content="world!")
        yield AgentEvent(
            type="tool_use",
            content="Using test_tool...",
            metadata={"name": "test_tool", "input": {}},
        )
        yield AgentEvent(
            type="tool_result",
            content="Tool completed",
            metadata={"name": "test_tool"},
        )
        yield AgentEvent(type="done", content="")

    router.run = mock_run
    router.stop = AsyncMock()
    return router


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_agent_loop_process_message(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
    mock_router,
):
    """Test that AgentLoop processes messages through the router."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory
    mock_router_cls.return_value = mock_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            loop = AgentLoop()

            msg = InboundMessage(
                channel=Channel.CLI,
                sender_id="user1",
                chat_id="chat1",
                content="Hello",
            )

            await loop._process_message(msg)

            mock_memory.add_to_session.assert_called()
            assert mock_bus.publish_outbound.call_count >= 2
            assert mock_bus.publish_system.call_count >= 1


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@pytest.mark.asyncio
async def test_agent_loop_reset_router(
    mock_builder_cls, mock_get_memory, mock_get_bus, mock_bus, mock_memory
):
    """Test that reset_router clears the router instance."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings

        loop = AgentLoop()
        assert loop._router is None
        loop.reset_router()
        assert loop._router is None


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_agent_loop_handles_error(
    mock_router_cls, mock_builder_cls, mock_get_memory, mock_get_bus, mock_bus, mock_memory
):
    """Test that AgentLoop handles errors gracefully."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    error_router = MagicMock()

    async def mock_run_error(message, *, system_prompt=None, history=None, session_key=None):
        yield AgentEvent(type="error", content="Something went wrong")
        yield AgentEvent(type="done", content="")

    error_router.run = mock_run_error
    mock_router_cls.return_value = error_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            loop = AgentLoop()

            msg = InboundMessage(
                channel=Channel.CLI,
                sender_id="user1",
                chat_id="chat1",
                content="Hello",
            )

            await loop._process_message(msg)
            mock_bus.publish_system.assert_called()


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_agent_loop_emits_tool_events(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
    mock_router,
):
    """Test that tool_use and tool_result events are emitted as SystemEvents."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory
    mock_router_cls.return_value = mock_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            loop = AgentLoop()

            msg = InboundMessage(
                channel=Channel.CLI,
                sender_id="user1",
                chat_id="chat1",
                content="Run a tool",
            )

            await loop._process_message(msg)

            system_calls = mock_bus.publish_system.call_args_list
            event_types = [call[0][0].event_type for call in system_calls]

            assert "thinking" in event_types
            assert "tool_start" in event_types
            assert "tool_result" in event_types


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_agent_loop_builds_context_and_passes_to_router(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
):
    """Test that AgentLoop builds system prompt and passes it to router."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    captured_kwargs = {}

    async def capturing_run(message, *, system_prompt=None, history=None, session_key=None):
        captured_kwargs["system_prompt"] = system_prompt
        captured_kwargs["history"] = history
        yield AgentEvent(type="message", content="OK")
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = capturing_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(
        return_value="You are PocketPaw with identity and memory."
    )

    session_history = [
        {"role": "user", "content": "previous question"},
        {"role": "assistant", "content": "previous answer"},
    ]
    mock_memory.get_compacted_history = AsyncMock(return_value=session_history)

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            loop = AgentLoop()

            msg = InboundMessage(
                channel=Channel.CLI,
                sender_id="user1",
                chat_id="chat1",
                content="What did I ask before?",
            )

            await loop._process_message(msg)

            mock_builder_instance.build_system_prompt.assert_called_once()
            mock_memory.get_compacted_history.assert_called_once()
            assert captured_kwargs["system_prompt"] == "You are PocketPaw with identity and memory."
            assert captured_kwargs["history"] == session_history


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@pytest.mark.asyncio
async def test_agent_loop_handles_error_before_router_init(
    mock_builder_cls, mock_get_memory, mock_get_bus, mock_bus, mock_memory
):
    """Test that AgentLoop handles errors before router initialization without UnboundLocalError.

    Regression test for issue #333: If an exception occurs before router is initialized,
    the error handler should not raise UnboundLocalError when trying to call router.stop().
    """
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    # Make memory.add_to_session raise an exception (before router is initialized)
    mock_memory.add_to_session = AsyncMock(
        side_effect=RuntimeError("Simulated memory failure before router init")
    )

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            # Patch health engine to avoid import errors
            with patch("pocketpaw.health.get_health_engine"):
                loop = AgentLoop()

                msg = InboundMessage(
                    channel=Channel.CLI,
                    sender_id="user1",
                    chat_id="chat1",
                    content="This should fail",
                )

                # Patch the redact_output function to avoid import/dependency issues
                with patch("pocketpaw.agents.loop.redact_output", side_effect=lambda x: x):
                    # This should not raise UnboundLocalError (the bug we're testing)
                    await loop._process_message(msg)

                    # Verify error was published to outbound channel
                    assert mock_bus.publish_outbound.call_count >= 1

                    # Find the error message among all outbound messages
                    error_found = False
                    for call in mock_bus.publish_outbound.call_args_list:
                        content = call[0][0].content
                        if content and "an error occurred" in content.lower():
                            assert "simulated memory failure" in content.lower()
                            error_found = True
                            break

                    assert error_found, "Error message should be published to outbound channel"
