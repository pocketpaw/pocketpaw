"""Tests for concurrency controls: session locks, global semaphore, async clients."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.bus import Channel, InboundMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inbound(chat_id: str, content: str = "hi") -> InboundMessage:
    return InboundMessage(
        channel=Channel.WEBSOCKET,
        sender_id=chat_id,
        chat_id=chat_id,
        content=content,
        metadata={},
    )


def _make_slow_router(delay: float = 0.1):
    """Return a mock router whose run() sleeps for *delay* seconds."""
    router = MagicMock()

    async def mock_run(message, *, system_prompt=None, history=None):
        await asyncio.sleep(delay)
        yield {"type": "message", "content": "ok", "metadata": {}}
        yield {"type": "done", "content": ""}

    router.run = mock_run
    router.stop = AsyncMock()
    return router


# ---------------------------------------------------------------------------
# 1. AgentLoop — session lock serialises same-session messages
# ---------------------------------------------------------------------------


@patch("pocketclaw.agents.loop.get_injection_scanner")
@patch("pocketclaw.agents.loop.get_message_bus")
@patch("pocketclaw.agents.loop.get_memory_manager")
@patch("pocketclaw.agents.loop.AgentContextBuilder")
@patch("pocketclaw.agents.loop.get_settings")
async def test_session_lock_serialises_same_session(
    mock_get_settings,
    mock_ctx_cls,
    mock_get_mem,
    mock_get_bus,
    mock_get_scanner,
):
    """Two messages with the same session_key must not overlap."""
    settings = MagicMock()
    settings.injection_scan_enabled = False
    settings.memory_backend = "file"
    settings.file_auto_learn = False
    settings.mem0_auto_learn = False
    settings.compaction_recent_window = 10
    settings.compaction_char_budget = 8000
    settings.compaction_summary_chars = 150
    settings.compaction_llm_summarize = False
    settings.max_concurrent_conversations = 5
    mock_get_settings.return_value = settings

    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    mock_get_bus.return_value = bus

    mem = MagicMock()
    mem.add_to_session = AsyncMock()
    mem.get_compacted_history = AsyncMock(return_value=[])
    mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    mock_get_mem.return_value = mem

    ctx = MagicMock()
    ctx.build_system_prompt = AsyncMock(return_value="system")
    mock_ctx_cls.return_value = ctx

    scanner = MagicMock()
    mock_get_scanner.return_value = scanner

    from pocketclaw.agents.loop import AgentLoop

    loop = AgentLoop()

    order = []
    delay = 0.05

    async def slow_run(message, *, system_prompt=None, history=None):
        order.append(f"start:{message}")
        await asyncio.sleep(delay)
        order.append(f"end:{message}")
        yield {"type": "message", "content": "ok", "metadata": {}}
        yield {"type": "done", "content": ""}

    router = MagicMock()
    router.run = slow_run
    loop._router = router

    msg1 = _make_inbound("user1", "first")
    msg2 = _make_inbound("user1", "second")

    # Fire both concurrently — same session key
    await asyncio.gather(
        loop._process_message(msg1),
        loop._process_message(msg2),
    )

    # With the session lock the second must not start until the first finishes.
    # "start:first" < "end:first" < "start:second" < "end:second"
    assert order.index("end:first") < order.index("start:second")


# ---------------------------------------------------------------------------
# 2. AgentLoop — cross-session parallelism
# ---------------------------------------------------------------------------


@patch("pocketclaw.agents.loop.get_injection_scanner")
@patch("pocketclaw.agents.loop.get_message_bus")
@patch("pocketclaw.agents.loop.get_memory_manager")
@patch("pocketclaw.agents.loop.AgentContextBuilder")
@patch("pocketclaw.agents.loop.get_settings")
async def test_cross_session_runs_in_parallel(
    mock_get_settings,
    mock_ctx_cls,
    mock_get_mem,
    mock_get_bus,
    mock_get_scanner,
):
    """Messages for different sessions should overlap in time."""
    settings = MagicMock()
    settings.injection_scan_enabled = False
    settings.memory_backend = "file"
    settings.file_auto_learn = False
    settings.mem0_auto_learn = False
    settings.compaction_recent_window = 10
    settings.compaction_char_budget = 8000
    settings.compaction_summary_chars = 150
    settings.compaction_llm_summarize = False
    settings.max_concurrent_conversations = 5
    mock_get_settings.return_value = settings

    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    mock_get_bus.return_value = bus

    mem = MagicMock()
    mem.add_to_session = AsyncMock()
    mem.get_compacted_history = AsyncMock(return_value=[])
    mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    mock_get_mem.return_value = mem

    ctx = MagicMock()
    ctx.build_system_prompt = AsyncMock(return_value="system")
    mock_ctx_cls.return_value = ctx

    scanner = MagicMock()
    mock_get_scanner.return_value = scanner

    from pocketclaw.agents.loop import AgentLoop

    loop = AgentLoop()

    order = []

    async def slow_run(message, *, system_prompt=None, history=None):
        order.append(f"start:{message}")
        await asyncio.sleep(0.05)
        order.append(f"end:{message}")
        yield {"type": "message", "content": "ok", "metadata": {}}
        yield {"type": "done", "content": ""}

    router = MagicMock()
    router.run = slow_run
    loop._router = router

    # Different session keys → should run in parallel
    msg_a = _make_inbound("userA", "alpha")
    msg_b = _make_inbound("userB", "beta")

    await asyncio.gather(
        loop._process_message(msg_a),
        loop._process_message(msg_b),
    )

    # Both should start before either ends (parallel)
    assert order.index("start:alpha") < order.index("end:alpha")
    assert order.index("start:beta") < order.index("end:beta")
    # At least one starts before the other ends
    starts = [i for i, v in enumerate(order) if v.startswith("start:")]
    ends = [i for i, v in enumerate(order) if v.startswith("end:")]
    assert starts[1] < ends[0], "Expected parallel execution but got serial"


# ---------------------------------------------------------------------------
# 3. AgentLoop — global semaphore caps concurrency
# ---------------------------------------------------------------------------


@patch("pocketclaw.agents.loop.get_injection_scanner")
@patch("pocketclaw.agents.loop.get_message_bus")
@patch("pocketclaw.agents.loop.get_memory_manager")
@patch("pocketclaw.agents.loop.AgentContextBuilder")
@patch("pocketclaw.agents.loop.get_settings")
async def test_global_semaphore_caps_concurrency(
    mock_get_settings,
    mock_ctx_cls,
    mock_get_mem,
    mock_get_bus,
    mock_get_scanner,
):
    """With max_concurrent_conversations=1, even cross-session must serialise."""
    settings = MagicMock()
    settings.injection_scan_enabled = False
    settings.memory_backend = "file"
    settings.file_auto_learn = False
    settings.mem0_auto_learn = False
    settings.compaction_recent_window = 10
    settings.compaction_char_budget = 8000
    settings.compaction_summary_chars = 150
    settings.compaction_llm_summarize = False
    settings.max_concurrent_conversations = 1  # Force serial
    mock_get_settings.return_value = settings

    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    mock_get_bus.return_value = bus

    mem = MagicMock()
    mem.add_to_session = AsyncMock()
    mem.get_compacted_history = AsyncMock(return_value=[])
    mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    mock_get_mem.return_value = mem

    ctx = MagicMock()
    ctx.build_system_prompt = AsyncMock(return_value="system")
    mock_ctx_cls.return_value = ctx

    scanner = MagicMock()
    mock_get_scanner.return_value = scanner

    from pocketclaw.agents.loop import AgentLoop

    loop = AgentLoop()

    order = []

    async def slow_run(message, *, system_prompt=None, history=None):
        order.append(f"start:{message}")
        await asyncio.sleep(0.05)
        order.append(f"end:{message}")
        yield {"type": "message", "content": "ok", "metadata": {}}
        yield {"type": "done", "content": ""}

    router = MagicMock()
    router.run = slow_run
    loop._router = router

    msg_a = _make_inbound("userA", "alpha")
    msg_b = _make_inbound("userB", "beta")

    await asyncio.gather(
        loop._process_message(msg_a),
        loop._process_message(msg_b),
    )

    # With semaphore(1), first must fully finish before second starts
    first_end = min(order.index("end:alpha"), order.index("end:beta"))
    second_start = max(order.index("start:alpha"), order.index("start:beta"))
    assert first_end < second_start, "Semaphore(1) should serialise cross-session"


# ---------------------------------------------------------------------------
# 4. PocketPaw Native — messages.create is awaited (AsyncAnthropic)
# ---------------------------------------------------------------------------


async def test_pocketpaw_native_uses_async_anthropic():
    """Verify PocketPawOrchestrator imports and uses AsyncAnthropic."""
    settings = MagicMock()
    settings.anthropic_api_key = "sk-test"
    settings.anthropic_model = "claude-sonnet-4-5-20250929"
    settings.tool_profile = "full"
    settings.tools_allow = []
    settings.tools_deny = []
    settings.file_jail_path = Path.home()
    settings.smart_routing_enabled = False

    with patch("pocketclaw.agents.pocketpaw_native.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Mock the messages.create to return an async-compatible response
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello!"
        mock_response.content = [text_block]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        orch = PocketPawOrchestrator(settings)

        # Confirm AsyncAnthropic was used
        mock_cls.assert_called_once_with(api_key="sk-test")

        # Run a chat and confirm messages.create was awaited
        events = []
        async for event in orch.chat("hello"):
            events.append(event)

        mock_client.messages.create.assert_awaited_once()
        assert any(e.type == "message" for e in events)


# ---------------------------------------------------------------------------
# 5. Open Interpreter — semaphore prevents concurrent access
# ---------------------------------------------------------------------------


async def test_oi_semaphore_prevents_concurrent_access():
    """OpenInterpreterAgent._semaphore should prevent overlapping run() calls."""
    settings = MagicMock()
    settings.llm_provider = "anthropic"
    settings.anthropic_api_key = "sk-test"
    settings.anthropic_model = "claude-sonnet-4-5-20250929"

    with patch("pocketclaw.agents.open_interpreter.interpreter", create=True):
        from pocketclaw.agents.open_interpreter import OpenInterpreterAgent

        agent = OpenInterpreterAgent(settings)

        # Verify semaphore exists and has value 1
        assert isinstance(agent._semaphore, asyncio.Semaphore)
        assert agent._semaphore._value == 1


# ---------------------------------------------------------------------------
# 6. FileMemoryStore — session write lock prevents corruption
# ---------------------------------------------------------------------------


async def test_file_memory_store_session_lock(tmp_path):
    """Concurrent _save_session_entry calls should not corrupt session JSON."""
    from pocketclaw.memory.file_store import FileMemoryStore
    from pocketclaw.memory.protocol import MemoryEntry, MemoryType

    store = FileMemoryStore(base_path=tmp_path)

    # Verify lock dict exists
    assert isinstance(store._session_write_locks, dict)

    # Create 10 entries concurrently for the same session
    session_key = "test_session"

    async def save_entry(i: int):
        entry = MemoryEntry(
            id=f"entry-{i}",
            type=MemoryType.SESSION,
            content=f"message {i}",
            role="user",
            session_key=session_key,
        )
        await store._save_session_entry(entry)

    await asyncio.gather(*[save_entry(i) for i in range(10)])

    # Verify session file is valid JSON with all 10 entries
    session_file = store._get_session_file(session_key)
    data = json.loads(session_file.read_text())
    assert len(data) == 10
    contents = {item["content"] for item in data}
    assert contents == {f"message {i}" for i in range(10)}


# ---------------------------------------------------------------------------
# 7. Config — max_concurrent_conversations field
# ---------------------------------------------------------------------------


def test_config_max_concurrent_conversations_default():
    """Settings should have max_concurrent_conversations with default 5."""
    from pocketclaw.config import Settings

    s = Settings()
    assert s.max_concurrent_conversations == 5


def test_config_max_concurrent_conversations_save():
    """max_concurrent_conversations should appear in save() output."""
    from pocketclaw.config import Settings

    s = Settings(max_concurrent_conversations=10)

    # Capture the JSON that would be written
    with patch("pocketclaw.config.get_config_path") as mock_path:
        mock_file = MagicMock()
        mock_path.return_value = mock_file
        mock_file.exists.return_value = False

        written = {}

        def capture_write(text):
            written.update(json.loads(text))

        mock_file.write_text = capture_write
        s.save()

    assert written["max_concurrent_conversations"] == 10
