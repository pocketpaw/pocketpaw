# Tests for Unified Agent Loop
# Updated for AgentEvent-based architecture (no more dict chunks)

import asyncio
import logging
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
async def test_kill_audit_failure_is_logged(
    mock_builder_cls, mock_get_memory, mock_get_bus, mock_bus, mock_memory, caplog
):
    """/kill should continue even if audit logging fails, while surfacing logs."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

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
                content="/kill",
            )

            consume_calls = 0

            async def _consume_once(timeout=1.0):
                nonlocal consume_calls
                consume_calls += 1
                if consume_calls == 1:
                    return msg
                loop._running = False
                return None

            mock_bus.consume_inbound = AsyncMock(side_effect=_consume_once)

            mock_audit_logger = MagicMock()
            mock_audit_logger.log.side_effect = RuntimeError("audit write failed")

            with patch(
                "pocketpaw.security.audit.get_audit_logger",
                return_value=mock_audit_logger,
            ):
                loop._running = True
                with caplog.at_level(logging.ERROR):
                    await loop._loop()

            assert any(
                "Failed to write audit log for /kill action" in rec.message
                for rec in caplog.records
            )
            # Reply + stream_end should still be sent
            assert mock_bus.publish_outbound.call_count >= 2


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
async def test_recent_file_tracker_failures_are_logged(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
    mock_router,
    caplog,
):
    """Tool tracking errors should be visible in logs, not silently swallowed."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory
    mock_router_cls.return_value = mock_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.injection_scan_llm = False
        settings.pii_scan_enabled = False
        settings.pii_scan_memory = False
        settings.welcome_hint_enabled = False
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

            tracker = MagicMock()
            tracker.record_tool_use.side_effect = RuntimeError("tracker failed")

            with patch("pocketpaw.agents.loop.get_recent_files_tracker", return_value=tracker):
                with caplog.at_level(logging.DEBUG):
                    await loop._process_message(msg)

            assert any(
                "Failed to record recent file tracker event for tool" in rec.message
                for rec in caplog.records
            )


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
    mock_builder_instance.bootstrap.get_context = AsyncMock(
        return_value=MagicMock(to_identity_block=lambda: "<identity>Test</identity>")
    )

    session_history = [
        {"role": "user", "content": "previous question"},
        {"role": "assistant", "content": "previous answer"},
    ]
    mock_memory.get_compacted_history = AsyncMock(return_value=session_history)
    mock_memory._store.get_session = AsyncMock(return_value=[])  # No messages for this test

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


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_identity_reinforcement_appended_on_long_conversations(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
):
    """Full identity block is re-injected periodically when message count is multiple of 5."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    captured: dict = {}

    async def capturing_run(message, *, system_prompt=None, history=None, session_key=None):
        captured["system_prompt"] = system_prompt
        yield AgentEvent(type="message", content="OK")
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = capturing_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    # Mock the bootstrap context
    from pocketpaw.bootstrap.protocol import BootstrapContext

    mock_bootstrap = MagicMock()
    mock_bootstrap.get_context = AsyncMock(
        return_value=BootstrapContext(
            name="TestAgent",
            identity="You are a test agent",
            soul="Test soul",
            style="Test style",
            user_profile="Test profile",
        )
    )

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(
        return_value="<identity>You are PocketPaw</identity>"
    )
    mock_builder_instance.bootstrap = mock_bootstrap

    # Mock session with 5 messages to trigger reinforcement
    mock_memory._store.get_session = AsyncMock(
        return_value=[MagicMock(role="user", content=f"msg {i}") for i in range(5)]
    )
    mock_memory.get_compacted_history = AsyncMock(
        return_value=[{"role": "user", "content": f"message {i}"} for i in range(5)]
    )

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
                content="Keep going",
            )
            await loop._process_message(msg)

    # Check that the system prompt contains the re-injected identity block
    system_prompt = captured.get("system_prompt", "")
    assert "<identity>" in system_prompt
    assert "TestAgent" in system_prompt  # From the re-injected identity
    assert system_prompt.count("<identity>") >= 2  # Original + re-injected


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_identity_reinforcement_not_appended_on_short_conversations(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
):
    """Identity re-injection does NOT happen when message count is not multiple of 5."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    captured: dict = {}

    async def capturing_run(message, *, system_prompt=None, history=None, session_key=None):
        captured["system_prompt"] = system_prompt
        yield AgentEvent(type="message", content="OK")
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = capturing_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    # Mock the bootstrap context
    from pocketpaw.bootstrap.protocol import BootstrapContext

    mock_bootstrap = MagicMock()
    mock_bootstrap.get_context = AsyncMock(
        return_value=BootstrapContext(
            name="TestAgent",
            identity="You are a test agent",
            soul="Test soul",
            style="Test style",
            user_profile="Test profile",
        )
    )

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(
        return_value="<identity>You are PocketPaw</identity>"
    )
    mock_builder_instance.bootstrap = mock_bootstrap

    # Short session — 3 messages (not multiple of 5)
    mock_memory._store.get_session = AsyncMock(
        return_value=[MagicMock(role="user", content=f"msg {i}") for i in range(3)]
    )
    mock_memory.get_compacted_history = AsyncMock(
        return_value=[{"role": "user", "content": f"message {i}"} for i in range(3)]
    )

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

    # Check that no additional identity block was appended
    system_prompt = captured.get("system_prompt", "")
    assert system_prompt.count("<identity>") == 1  # Only the original one


# ---------------------------------------------------------------------------
# Session-lock GC tests  (regression for memory-leak bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gc_removes_stale_locks():
    """
    _gc_session_locks must evict locks that have been idle longer than
    _SESSION_LOCK_TTL and are not currently held.
    """
    import time

    from pocketpaw.agents.loop import _SESSION_LOCK_TTL, AgentLoop

    with (
        patch("pocketpaw.agents.loop.get_message_bus"),
        patch("pocketpaw.agents.loop.get_memory_manager"),
        patch("pocketpaw.agents.loop.AgentContextBuilder"),
        patch("pocketpaw.agents.loop.get_settings") as mock_get_settings,
    ):
        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.agent_backend = "claude_agent_sdk"
        mock_get_settings.return_value = settings

        loop = AgentLoop()

        # Manually insert a stale lock: last_used is far in the past
        stale_key = "session:stale"
        loop._session_locks[stale_key] = asyncio.Lock()
        loop._session_lock_last_used[stale_key] = time.monotonic() - (_SESSION_LOCK_TTL + 1)

        # Insert a fresh lock: last_used is now, should NOT be removed
        fresh_key = "session:fresh"
        loop._session_locks[fresh_key] = asyncio.Lock()
        loop._session_lock_last_used[fresh_key] = time.monotonic()

        # Patch asyncio.sleep so the GC body runs once then stops:
        # - first call returns immediately (skips the 5-minute wait)
        # - second call raises CancelledError to exit the while-True loop
        sleep_call_count = 0

        async def fast_sleep(_):
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 2:
                raise asyncio.CancelledError  # stop after one full GC pass

        with patch("asyncio.sleep", side_effect=fast_sleep):
            try:
                await loop._gc_session_locks()
            except asyncio.CancelledError:
                pass

        assert stale_key not in loop._session_locks, "Stale lock should be evicted"
        assert stale_key not in loop._session_lock_last_used, "Stale timestamp should be evicted"
        assert fresh_key in loop._session_locks, "Fresh lock must not be evicted"
        assert fresh_key in loop._session_lock_last_used, "Fresh timestamp must not be evicted"


@pytest.mark.asyncio
async def test_gc_skips_acquired_locks():
    """
    _gc_session_locks must NOT evict a lock that is currently held,
    even if its TTL has expired.
    """
    import time

    from pocketpaw.agents.loop import _SESSION_LOCK_TTL, AgentLoop

    with (
        patch("pocketpaw.agents.loop.get_message_bus"),
        patch("pocketpaw.agents.loop.get_memory_manager"),
        patch("pocketpaw.agents.loop.AgentContextBuilder"),
        patch("pocketpaw.agents.loop.get_settings") as mock_get_settings,
    ):
        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.agent_backend = "claude_agent_sdk"
        mock_get_settings.return_value = settings

        loop = AgentLoop()

        held_key = "session:held"
        lock = asyncio.Lock()
        await lock.acquire()  # lock is now held — must not be evicted
        loop._session_locks[held_key] = lock
        loop._session_lock_last_used[held_key] = time.monotonic() - (_SESSION_LOCK_TTL + 1)

        sleep_call_count = 0

        async def fast_sleep(_):
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 2:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fast_sleep):
            try:
                await loop._gc_session_locks()
            except asyncio.CancelledError:
                pass

        assert held_key in loop._session_locks, "Held lock must never be evicted by GC"
        lock.release()


@pytest.mark.asyncio
async def test_stop_cancels_gc_task():
    """
    stop() must cancel the GC background task so it does not outlive the loop.
    """
    from pocketpaw.agents.loop import AgentLoop

    with (
        patch("pocketpaw.agents.loop.get_message_bus"),
        patch("pocketpaw.agents.loop.get_memory_manager"),
        patch("pocketpaw.agents.loop.AgentContextBuilder"),
        patch("pocketpaw.agents.loop.get_settings") as mock_get_settings,
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
    ):
        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.agent_backend = "claude_agent_sdk"
        mock_get_settings.return_value = settings
        mock_settings_cls.load.return_value = settings

        loop = AgentLoop()

        # Simulate a live GC task by creating one manually
        async def _noop():
            await asyncio.sleep(9999)

        loop._lock_gc_task = asyncio.create_task(_noop())

        await loop.stop()

        assert loop._lock_gc_task is None, "stop() must clear _lock_gc_task"


# ---------------------------------------------------------------------------
# Auto-TTS tests (voice reply feature)
# ---------------------------------------------------------------------------


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_auto_tts_triggered_by_voice_message(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
):
    """Test that voice inbound message triggers auto-TTS and attaches audio."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    # Mock router yields text response without calling text_to_speech tool
    async def mock_run(message, *, system_prompt=None, history=None, session_key=None):
        yield AgentEvent(type="message", content="I heard your voice message!")
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        settings.voice_reply_enabled = True
        settings.injection_scan_enabled = False
        settings.pii_scan_enabled = False
        settings.welcome_hint_enabled = False
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            # Mock synthesize_speech to return a fake audio path
            mock_tts = AsyncMock(return_value="/tmp/tts_12345678.mp3")
            with patch("pocketpaw.tools.builtin.voice.synthesize_speech", mock_tts):
                loop = AgentLoop()

                # Send voice message (has .ogg media attachment)
                msg = InboundMessage(
                    channel=Channel.CLI,
                    sender_id="user1",
                    chat_id="chat1",
                    content="[voice message]",
                    media=["/tmp/voice_input.ogg"],
                )

                await loop._process_message(msg)

                # Verify synthesize_speech was called with the agent's response
                mock_tts.assert_called_once_with("I heard your voice message!")

                # Verify Out boundMessage stream_end includes the generated audio
                outbound_calls = mock_bus.publish_outbound.call_args_list
                stream_end_call = [c for c in outbound_calls if c[0][0].is_stream_end]
                assert len(stream_end_call) == 1
                stream_end_msg = stream_end_call[0][0][0]
                assert "/tmp/tts_12345678.mp3" in stream_end_msg.media


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_auto_tts_skipped_when_agent_already_sent_audio(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
):
    """Test that auto-TTS is skipped when agent already called text_to_speech tool."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    # Mock router yields tool_result with media tag (agent already generated audio)
    async def mock_run(message, *, system_prompt=None, history=None, session_key=None):
        yield AgentEvent(type="message", content="Here's my voice reply")
        yield AgentEvent(
            type="tool_result",
            content="<!-- media:/tmp/agent_tts.mp3 -->",
            metadata={"name": "text_to_speech"},
        )
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        settings.voice_reply_enabled = True
        settings.injection_scan_enabled = False
        settings.pii_scan_enabled = False
        settings.welcome_hint_enabled = False
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            mock_tts = AsyncMock()
            with patch("pocketpaw.tools.builtin.voice.synthesize_speech", mock_tts):
                loop = AgentLoop()

                msg = InboundMessage(
                    channel=Channel.CLI,
                    sender_id="user1",
                    chat_id="chat1",
                    content="[voice]",
                    media=["/tmp/voice.ogg"],
                )

                await loop._process_message(msg)

                # synthesize_speech should NOT be called (agent already provided audio)
                mock_tts.assert_not_called()


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_auto_tts_disabled_by_setting(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
):
    """Test that auto-TTS is skipped when voice_reply_enabled=False."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    async def mock_run(message, *, system_prompt=None, history=None, session_key=None):
        yield AgentEvent(type="message", content="Response")
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        settings.voice_reply_enabled = False  # Disabled
        settings.injection_scan_enabled = False
        settings.pii_scan_enabled = False
        settings.welcome_hint_enabled = False
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            mock_tts = AsyncMock()
            with patch("pocketpaw.tools.builtin.voice.synthesize_speech", mock_tts):
                loop = AgentLoop()

                msg = InboundMessage(
                    channel=Channel.CLI,
                    sender_id="user1",
                    chat_id="chat1",
                    content="[voice]",
                    media=["/tmp/voice.ogg"],
                )

                await loop._process_message(msg)

                # synthesize_speech should NOT be called (feature disabled)
                mock_tts.assert_not_called()


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_auto_tts_handles_synthesis_failure_gracefully(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
):
    """Test that auto-TTS failure doesn't crash the agent loop."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    async def mock_run(message, *, system_prompt=None, history=None, session_key=None):
        yield AgentEvent(type="message", content="Response text")
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with patch("pocketpaw.agents.loop.get_settings") as mock_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        settings.voice_reply_enabled = True
        settings.injection_scan_enabled = False
        settings.pii_scan_enabled = False
        settings.welcome_hint_enabled = False
        mock_settings.return_value = settings

        with patch("pocketpaw.agents.loop.Settings") as mock_settings_cls:
            mock_settings_cls.load.return_value = settings

            # Mock synthesize_speech to raise an exception
            mock_tts = AsyncMock(side_effect=RuntimeError("TTS service unavailable"))
            with patch("pocketpaw.tools.builtin.voice.synthesize_speech", mock_tts):
                loop = AgentLoop()

                msg = InboundMessage(
                    channel=Channel.CLI,
                    sender_id="user1",
                    chat_id="chat1",
                    content="[voice]",
                    media=["/tmp/voice.wav"],
                )

                # Should not raise — failure is logged and swallowed
                await loop._process_message(msg)

                mock_tts.assert_called_once()

                # Verify stream_end was still sent (no audio attached)
                outbound_calls = mock_bus.publish_outbound.call_args_list
                stream_end_call = [c for c in outbound_calls if c[0][0].is_stream_end]
                assert len(stream_end_call) == 1
                # No audio in media list (synthesis failed)
                stream_end_msg = stream_end_call[0][0][0]
                assert len(stream_end_msg.media) == 0


# ---------------------------------------------------------------------------
# PR #658 reviewer suggestions: surface swallowed exceptions
# ---------------------------------------------------------------------------


def _make_loop_with_settings(mock_get_bus, mock_get_memory, mock_builder_cls):
    """Helper to build an AgentLoop with standard mock settings."""
    from pocketpaw.agents.loop import AgentLoop

    with (
        patch("pocketpaw.agents.loop.get_settings") as mock_get_settings,
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
    ):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_get_settings.return_value = settings
        mock_settings_cls.load.return_value = settings
        return AgentLoop()


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_agents_md_discovery_failure_is_silently_logged(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
    mock_router,
    caplog,
):
    """AGENTS.md discovery failure must be caught and logged at DEBUG, not crash the loop."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory
    mock_router_cls.return_value = mock_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with (
        patch("pocketpaw.agents.loop.get_settings") as mock_settings,
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
        patch(
            "pocketpaw.agents_md.AgentsMdLoader.find_and_load",
            side_effect=RuntimeError("disk error"),
        ),
    ):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings
        mock_settings_cls.load.return_value = settings

        loop = AgentLoop()
        msg = InboundMessage(
            channel=Channel.CLI,
            sender_id="user1",
            chat_id="chat1",
            content="Hello",
        )

        with caplog.at_level(logging.DEBUG, logger="pocketpaw.agents.loop"):
            await loop._process_message(msg)

    # The loop must complete (publish stream-end) despite the AGENTS.md error.
    outbound_calls = [str(c) for c in mock_bus.publish_outbound.call_args_list]
    assert any("is_stream_end" in c for c in outbound_calls), (
        "Loop must still publish stream-end even when AGENTS.md discovery raises"
    )
    assert any("AGENTS.md discovery failed" in r.message for r in caplog.records), (
        "AGENTS.md failure must be logged"
    )


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_token_metrics_persist_failure_is_logged_at_debug(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
    caplog,
):
    """A crash inside the usage-tracker record() path must be caught and logged, not re-raised."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    token_router = MagicMock()

    async def mock_run_with_token_usage(
        message, *, system_prompt=None, history=None, session_key=None
    ):
        yield AgentEvent(
            type="token_usage",
            content="",
            metadata={
                "backend": "claude_agent_sdk",
                "model": "claude-3-haiku",
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_input_tokens": 0,
                "total_cost_usd": 0.001,
            },
        )
        yield AgentEvent(type="done", content="")

    token_router.run = mock_run_with_token_usage
    token_router.stop = AsyncMock()
    mock_router_cls.return_value = token_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with (
        patch("pocketpaw.agents.loop.get_settings") as mock_settings,
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
        patch(
            "pocketpaw.usage_tracker.get_usage_tracker",
            side_effect=RuntimeError("tracker unavailable"),
        ),
    ):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings
        mock_settings_cls.load.return_value = settings

        loop = AgentLoop()
        msg = InboundMessage(
            channel=Channel.CLI,
            sender_id="user1",
            chat_id="chat1",
            content="Tokens please",
        )

        with caplog.at_level(logging.DEBUG, logger="pocketpaw.agents.loop"):
            await loop._process_message(msg)

    # Loop must still complete.
    outbound_calls = [str(c) for c in mock_bus.publish_outbound.call_args_list]
    assert any("is_stream_end" in c for c in outbound_calls), (
        "Loop must publish stream-end even when usage tracker raises"
    )
    assert any("token usage metrics" in r.message for r in caplog.records), (
        "Token metrics failure must be logged"
    )


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_health_engine_persist_failure_logged_as_warning(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
    caplog,
):
    """When the health engine itself raises, the failure must be logged at WARNING level."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    # Router that raises an exception, triggering the health engine path.
    boom_router = MagicMock()

    async def mock_run_boom(message, *, system_prompt=None, history=None, session_key=None):
        raise RuntimeError("simulated router crash")
        yield  # make it an async generator

    boom_router.run = mock_run_boom
    boom_router.stop = AsyncMock()
    mock_router_cls.return_value = boom_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with (
        patch("pocketpaw.agents.loop.get_settings") as mock_settings,
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
        patch(
            "pocketpaw.health.get_health_engine",
            side_effect=RuntimeError("health engine down"),
        ),
    ):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings
        mock_settings_cls.load.return_value = settings

        loop = AgentLoop()
        msg = InboundMessage(
            channel=Channel.CLI,
            sender_id="user1",
            chat_id="chat1",
            content="Crash me",
        )

        with caplog.at_level(logging.WARNING, logger="pocketpaw.agents.loop"):
            await loop._process_message(msg)

    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("health engine" in m for m in warning_messages), (
        "Health engine persist failure must be logged at WARNING"
    )


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
@pytest.mark.asyncio
async def test_router_stop_failure_logged_as_warning(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
    mock_bus,
    mock_memory,
    caplog,
):
    """router.stop() failure during error handling must be logged at WARNING, not swallowed."""
    mock_get_bus.return_value = mock_bus
    mock_get_memory.return_value = mock_memory

    flaky_router = MagicMock()

    async def mock_run_boom(message, *, system_prompt=None, history=None, session_key=None):
        raise RuntimeError("router processing error")
        yield  # make it an async generator

    flaky_router.run = mock_run_boom
    flaky_router.stop = AsyncMock(side_effect=OSError("stop failed"))
    mock_router_cls.return_value = flaky_router

    mock_builder_instance = mock_builder_cls.return_value
    mock_builder_instance.build_system_prompt = AsyncMock(return_value="System Prompt")

    with (
        patch("pocketpaw.agents.loop.get_settings") as mock_settings,
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
    ):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings
        mock_settings_cls.load.return_value = settings

        loop = AgentLoop()
        msg = InboundMessage(
            channel=Channel.CLI,
            sender_id="user1",
            chat_id="chat1",
            content="Crash me",
        )

        with caplog.at_level(logging.WARNING, logger="pocketpaw.agents.loop"):
            await loop._process_message(msg)

    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("router" in m.lower() or "stop" in m.lower() for m in warning_messages), (
        "router.stop() failure must be logged at WARNING level"
    )
