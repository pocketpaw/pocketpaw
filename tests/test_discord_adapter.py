# Tests for Discord Channel Adapter (discli-based)

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.bus.adapters.discord_adapter import (
    _BOT_AUTHOR_KEY,
    _CONVERSATION_CHAR_BUDGET,
    _CONVERSATION_HISTORY_SIZE,
    _NO_RESPONSE_MARKER,
    DiscliAdapter,
)
from pocketpaw.bus.events import Channel, InboundMessage, OutboundMessage
from pocketpaw.bus.queue import MessageBus


@pytest.fixture
def adapter():
    return DiscliAdapter(
        token="test-token",
        allowed_guild_ids=[111, 222],
        allowed_user_ids=[999],
    )


@pytest.fixture
def convo_adapter():
    """Adapter with conversation mode enabled on channel 100."""
    return DiscliAdapter(
        token="test-token",
        conversation_channel_ids=[100],
        bot_name="Paw",
    )


@pytest.fixture
def bus():
    return MessageBus()


# ── Basic properties ────────────────────────────────────────────────


def test_channel_property(adapter):
    assert adapter.channel == Channel.DISCORD


def test_status_defaults_to_online():
    a = DiscliAdapter(token="t", status_type="invalid")
    assert a.status_type == "online"


def test_valid_status_types():
    for st in ("online", "idle", "dnd", "invisible"):
        a = DiscliAdapter(token="t", status_type=st)
        assert a.status_type == st


# ── Start / Stop ────────────────────────────────────────────────────


async def test_start_stop(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()

    await adapter.start(bus)
    assert adapter._running is True
    assert adapter._bus is bus

    await adapter.stop()
    assert adapter._running is False


async def test_start_no_token():
    a = DiscliAdapter(token="")
    with pytest.raises(RuntimeError, match="token missing"):
        await a._on_start()


async def test_on_start_does_not_pollute_os_environ():
    """Token must not be written into os.environ (OWASP A02 — process-wide secret exposure)."""
    import os

    secret = "super-secret-discord-token"
    a = DiscliAdapter(token=secret)

    with (
        patch("shutil.which", return_value="/usr/bin/discli"),
        patch.object(a, "_write_slash_config", new=AsyncMock(return_value=None)),
        patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec,
        patch.object(a, "_read_stdout", new=AsyncMock()),
        patch.object(a, "_drain_stderr", new=AsyncMock()),
        patch.object(a, "_eviction_loop", new=AsyncMock()),
    ):
        # Fake bot_id so _on_start does not time-out waiting for a ready event
        async def _set_bot_id(*_args, **_kwargs):
            a._bot_id = "123456789"
            return MagicMock(
                stdin=MagicMock(),
                stdout=MagicMock(),
                stderr=MagicMock(),
            )

        mock_exec.side_effect = _set_bot_id

        with patch.object(a, "_register_discord_mcp"):
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            await a._on_start()

    assert os.environ.get("DISCORD_BOT_TOKEN") != secret, (
        "Bot token must not be written into os.environ"
    )


# ── Auth checks ─────────────────────────────────────────────────────


def test_guild_auth_filtering(adapter):
    # Authorized guild + user
    assert adapter._check_auth("111", "999", None) is True
    # Unauthorized guild
    assert adapter._check_auth("333", "999", None) is False
    # Unauthorized user
    assert adapter._check_auth("111", "888", None) is False


def test_guild_auth_no_restrictions():
    a = DiscliAdapter(token="t")
    assert a._check_auth("999", "1", None) is True


def test_guild_auth_dm_no_guild(adapter):
    """DMs (no guild) pass guild check."""
    assert adapter._check_auth(None, "999", None) is True


def test_check_auth_channel_restriction():
    a = DiscliAdapter(token="t", allowed_channel_ids=[100, 200])
    assert a._check_auth(None, "1", "100") is True
    assert a._check_auth(None, "1", "300") is False
    assert a._check_auth(None, "1", None) is True


# ── _should_respond tests ──────────────────────────────────────────


def test_should_respond_name_mentioned(convo_adapter):
    convo_adapter._add_to_history(100, "alice", "Hey Paw, what do you think?")
    result = convo_adapter._should_respond(100, "Hey Paw, what do you think?")
    assert result == "addressed"


def test_should_respond_name_case_insensitive(convo_adapter):
    convo_adapter._add_to_history(100, "alice", "paw help me")
    assert convo_adapter._should_respond(100, "paw help me") == "addressed"


def test_should_respond_bot_was_previous_speaker(convo_adapter):
    convo_adapter._add_to_history(100, "alice", "hello")
    convo_adapter._add_to_history(100, _BOT_AUTHOR_KEY, "hi there!")
    convo_adapter._add_to_history(100, "alice", "thanks")
    assert convo_adapter._should_respond(100, "thanks") == "engaged"


def test_should_respond_question_with_recent_bot(convo_adapter):
    convo_adapter._add_to_history(100, "alice", "hey")
    convo_adapter._add_to_history(100, _BOT_AUTHOR_KEY, "sup")
    convo_adapter._add_to_history(100, "bob", "nothing much")
    convo_adapter._add_to_history(100, "alice", "anyone know the answer?")
    assert convo_adapter._should_respond(100, "anyone know the answer?") == "engaged"


def test_should_not_respond_to_unrelated_after_bot_spoke(convo_adapter):
    convo_adapter._add_to_history(100, _BOT_AUTHOR_KEY, "here's my take")
    convo_adapter._add_to_history(100, "alice", "interesting")
    convo_adapter._add_to_history(100, "bob", "agreed")
    assert convo_adapter._should_respond(100, "agreed") is None


def test_should_respond_skip_unrelated(convo_adapter):
    convo_adapter._add_to_history(100, "alice", "hello")
    convo_adapter._add_to_history(100, "bob", "hey alice")
    convo_adapter._add_to_history(100, "charlie", "whats up")
    convo_adapter._add_to_history(100, "alice", "not much")
    assert convo_adapter._should_respond(100, "not much") is None


def test_should_respond_empty_history(convo_adapter):
    assert convo_adapter._should_respond(100, "hello") is None


# ── Conversation history tests ──────────────────────────────────────


def test_conversation_history_rolling_window(convo_adapter):
    for i in range(40):
        convo_adapter._add_to_history(100, f"user{i}", f"msg {i}")
    history = convo_adapter._conversation_history[100]
    assert len(history) == _CONVERSATION_HISTORY_SIZE
    assert history[-1]["content"] == "msg 39"


def test_conversation_history_uses_sentinel_for_bot():
    a = DiscliAdapter(token="t", conversation_channel_ids=[1], bot_name="Paw")
    a._add_to_history(1, _BOT_AUTHOR_KEY, "bot reply")
    assert a._conversation_history[1][0]["author"] == _BOT_AUTHOR_KEY


# ── _format_conversation_context tests ──────────────────────────────


def test_format_context_replaces_sentinel_with_name():
    a = DiscliAdapter(token="t", conversation_channel_ids=[1], bot_name="Paw")
    a._add_to_history(1, "alice", "hi")
    a._add_to_history(1, _BOT_AUTHOR_KEY, "hello!")
    ctx = a._format_conversation_context(1, "general", "addressed")
    assert "Paw: hello!" in ctx
    assert _BOT_AUTHOR_KEY not in ctx


def test_format_context_addressed_mode():
    a = DiscliAdapter(token="t", conversation_channel_ids=[1], bot_name="Paw")
    a._add_to_history(1, "alice", "Hey Paw")
    ctx = a._format_conversation_context(1, "general", "addressed")
    assert "Someone is talking to you" in ctx
    assert _NO_RESPONSE_MARKER not in ctx


def test_format_context_engaged_mode():
    a = DiscliAdapter(token="t", conversation_channel_ids=[1], bot_name="Paw")
    a._add_to_history(1, "alice", "hello")
    ctx = a._format_conversation_context(1, "general", "engaged")
    assert _NO_RESPONSE_MARKER in ctx


def test_format_context_respects_char_budget():
    a = DiscliAdapter(token="t", conversation_channel_ids=[1], bot_name="Paw")
    for i in range(30):
        a._add_to_history(1, f"user{i}", "x" * 1000)
    ctx = a._format_conversation_context(1, "general", "engaged")
    lines = [ln for ln in ctx.split("\n") if ": " in ln and not ln.startswith("[")]
    total = sum(len(ln) for ln in lines)
    assert total <= _CONVERSATION_CHAR_BUDGET + 500


# ── _is_no_response tests ──────────────────────────────────────────


def test_is_no_response_exact():
    assert DiscliAdapter._is_no_response("[NO_RESPONSE]") is True
    assert DiscliAdapter._is_no_response("[NO_RESPONSE].") is True
    assert DiscliAdapter._is_no_response("  [NO_RESPONSE]  ") is True


def test_is_no_response_decorated():
    assert DiscliAdapter._is_no_response("`[NO_RESPONSE]`") is True
    assert DiscliAdapter._is_no_response("**[NO_RESPONSE]**") is True


def test_is_no_response_false():
    assert DiscliAdapter._is_no_response("Hello there") is False
    assert DiscliAdapter._is_no_response("I said [NO_RESPONSE] in context") is False


# ── Send via _send_command ──────────────────────────────────────────


async def test_send_normal_message(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="Hello Discord!",
    )
    await adapter.send(msg)

    adapter._send_command.assert_any_call("typing_stop", channel_id="12345")
    adapter._send_command.assert_any_call("send", channel_id="12345", content="Hello Discord!")


async def test_send_with_reply_to(adapter):
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="Reply here",
        reply_to="67890",
    )
    await adapter.send(msg)

    adapter._send_command.assert_any_call(
        "reply", channel_id="12345", message_id="67890", content="Reply here"
    )


async def test_send_interaction_followup(adapter):
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="Slash response",
        metadata={"interaction_token": "tok123"},
    )
    await adapter.send(msg)

    adapter._send_command.assert_any_call(
        "interaction_followup", interaction_token="tok123", content="Slash response"
    )


async def test_send_no_response_stops_typing(adapter):
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="[NO_RESPONSE]",
    )
    await adapter.send(msg)

    adapter._send_command.assert_called_once_with("typing_stop", channel_id="12345")


async def test_send_media_files(adapter):
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="See attached",
        media=["/tmp/file.png"],
    )
    await adapter.send(msg)

    adapter._send_command.assert_any_call(
        "send", channel_id="12345", content="", files=["/tmp/file.png"]
    )


# ── Streaming via _send_command ─────────────────────────────────────


async def test_stream_buffers_short_chunks(adapter):
    """Short chunks are buffered, not sent to Discord immediately."""
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"stream_id": "s1"})

    chunk = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="Hello ",
        is_stream_chunk=True,
    )
    await adapter.send(chunk)

    # Stream should NOT have started yet (content is below buffer threshold)
    assert "12345" not in adapter._active_streams
    assert adapter._stream_buffer["12345"] == "Hello "


async def test_stream_flushes_above_threshold(adapter):
    """Once buffered content exceeds threshold, stream starts."""
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"stream_id": "s1"})

    # Send enough content to exceed the 25-char buffer threshold
    long_content = "This is a response that exceeds the buffer threshold easily"
    chunk = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content=long_content,
        is_stream_chunk=True,
    )
    await adapter.send(chunk)

    adapter._send_command.assert_any_call(
        "stream_start", channel_id="12345", reply_to=None, interaction_token=None
    )
    adapter._send_command.assert_any_call("stream_chunk", stream_id="s1", content=long_content)
    assert adapter._active_streams["12345"] == "s1"


async def test_stream_end_flushes_short_buffer(adapter):
    """Short buffered content is sent as a regular message at stream_end."""
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})

    # Buffer a short chunk
    chunk = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="Short reply",
        is_stream_chunk=True,
    )
    await adapter.send(chunk)
    assert "12345" not in adapter._active_streams

    # End the stream — buffered content should be sent as regular message
    end = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end)

    adapter._send_command.assert_any_call("typing_stop", channel_id="12345")
    adapter._send_command.assert_any_call("send", channel_id="12345", content="Short reply")


async def test_stream_suppresses_no_response(adapter):
    """[NO_RESPONSE] streamed across chunks is buffered and suppressed."""
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})

    # Simulate [NO_RESPONSE] arriving in chunks
    for part in ["[NO_", "RESPONSE]"]:
        chunk = OutboundMessage(
            channel=Channel.DISCORD,
            chat_id="12345",
            content=part,
            is_stream_chunk=True,
        )
        await adapter.send(chunk)

    # Stream should NOT have started
    assert "12345" not in adapter._active_streams

    # End the stream — should suppress and stop typing
    end = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end)

    adapter._send_command.assert_called_once_with("typing_stop", channel_id="12345")


async def test_stream_end(adapter):
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})
    adapter._active_streams["12345"] = "s1"

    end = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end)

    adapter._send_command.assert_any_call("stream_end", stream_id="s1")
    assert "12345" not in adapter._active_streams


async def test_stream_end_with_media(adapter):
    adapter._proc = MagicMock()
    adapter._send_command = AsyncMock(return_value={"ok": True})
    adapter._active_streams["12345"] = "s1"

    end = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="",
        is_stream_end=True,
        media=["/tmp/out.txt"],
    )
    await adapter.send(end)

    adapter._send_command.assert_any_call(
        "send", channel_id="12345", content="", files=["/tmp/out.txt"]
    )


# ── InboundMessage publishing ───────────────────────────────────────


async def test_inbound_message_creation(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    msg = InboundMessage(
        channel=Channel.DISCORD,
        sender_id="999",
        chat_id="12345",
        content="test message",
        metadata={"username": "user#1234"},
    )
    await adapter._publish_inbound(msg)

    assert bus.inbound_pending() == 1
    consumed = await bus.consume_inbound()
    assert consumed.content == "test message"
    assert consumed.channel == Channel.DISCORD


async def test_bus_integration(bus):
    adapter = DiscliAdapter(token="t")
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    adapter.send = AsyncMock()

    await adapter.start(bus)

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="123",
        content="response",
    )
    await bus.publish_outbound(msg)

    adapter.send.assert_called_once_with(msg)

    await adapter.stop()


# ── _handle_message_event tests ─────────────────────────────────────


async def test_handle_message_skips_other_bots(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"

    await adapter._handle_message_event(
        {
            "author_id": "OTHER_BOT",
            "channel_id": "100",
            "is_bot": True,
            "content": "bot msg",
        }
    )

    assert bus.inbound_pending() == 0


async def test_handle_message_tracks_own_bot_in_convo(convo_adapter, bus):
    convo_adapter._on_start = AsyncMock()
    convo_adapter._on_stop = AsyncMock()
    await convo_adapter.start(bus)
    convo_adapter._bot_id = "BOT1"

    await convo_adapter._handle_message_event(
        {
            "author_id": "BOT1",
            "channel_id": "100",
            "is_bot": True,
            "content": "my reply",
        }
    )

    assert 100 in convo_adapter._conversation_history
    assert convo_adapter._conversation_history[100][-1]["author"] == _BOT_AUTHOR_KEY


async def test_handle_message_dm(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"
    adapter._send_command = AsyncMock(return_value={"ok": True})

    await adapter._handle_message_event(
        {
            "author_id": "999",
            "channel_id": "100",
            "is_bot": False,
            "is_dm": True,
            "content": "hello from DM",
        }
    )

    assert bus.inbound_pending() == 1


async def test_handle_message_ignores_non_dm_non_mention(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"

    await adapter._handle_message_event(
        {
            "author_id": "999",
            "channel_id": "100",
            "is_bot": False,
            "is_dm": False,
            "mentions_bot": False,
            "content": "random chat",
        }
    )

    assert bus.inbound_pending() == 0


# ── _handle_slash_event tests ───────────────────────────────────────


async def test_handle_slash_paw(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    await adapter._handle_slash_event(
        {
            "command": "paw",
            "args": {"message": "do something"},
            "channel_id": "100",
            "user_id": "999",
            "guild_id": "111",
            "interaction_token": "tok1",
            "user": "testuser",
        }
    )

    assert bus.inbound_pending() == 1
    msg = await bus.consume_inbound()
    assert msg.content == "do something"
    assert msg.metadata["interaction_token"] == "tok1"


async def test_handle_slash_new(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    await adapter._handle_slash_event(
        {
            "command": "new",
            "args": {},
            "channel_id": "100",
            "user_id": "999",
            "guild_id": "111",
            "interaction_token": "tok2",
        }
    )

    msg = await bus.consume_inbound()
    assert msg.content == "/new"


async def test_handle_slash_unauthorized(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._send_command = AsyncMock(return_value={"ok": True})

    await adapter._handle_slash_event(
        {
            "command": "paw",
            "args": {"message": "hack"},
            "channel_id": "100",
            "user_id": "666",
            "guild_id": "111",
            "interaction_token": "tok3",
        }
    )

    assert bus.inbound_pending() == 0
    adapter._send_command.assert_called_once_with(
        "interaction_followup",
        interaction_token="tok3",
        content="Unauthorized.",
    )


async def test_handle_slash_converse_enable(adapter, bus):
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._send_command = AsyncMock(return_value={"ok": True})

    assert 100 not in adapter.conversation_channel_ids

    await adapter._handle_slash_event(
        {
            "command": "converse",
            "args": {},
            "channel_id": "100",
            "user_id": "999",
            "guild_id": "111",
            "interaction_token": "tok_conv",
            "is_admin": True,
        }
    )

    assert 100 in adapter.conversation_channel_ids
    assert bus.inbound_pending() == 0  # handled locally, not forwarded
    adapter._send_command.assert_called_once()
    call_args = adapter._send_command.call_args
    assert "enabled" in call_args[1]["content"].lower()


async def test_handle_slash_converse_disable(bus):
    a = DiscliAdapter(
        token="test-token",
        allowed_guild_ids=[111],
        allowed_user_ids=[999],
        conversation_channel_ids=[100],
    )
    a._on_start = AsyncMock()
    a._on_stop = AsyncMock()
    await a.start(bus)
    a._send_command = AsyncMock(return_value={"ok": True})

    assert 100 in a.conversation_channel_ids

    await a._handle_slash_event(
        {
            "command": "converse",
            "args": {},
            "channel_id": "100",
            "user_id": "999",
            "guild_id": "111",
            "interaction_token": "tok_conv2",
            "is_admin": True,
        }
    )

    assert 100 not in a.conversation_channel_ids
    assert bus.inbound_pending() == 0
    call_args = a._send_command.call_args
    assert "disabled" in call_args[1]["content"].lower()


# ── _send_command / _read_stdout communication ──────────────────────


async def test_send_command_no_proc(adapter):
    adapter._proc = None
    result = await adapter._send_command("test")
    assert "error" in result


async def test_send_command_timeout(adapter):
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.write = MagicMock()
    mock_proc.stdin.drain = AsyncMock()
    adapter._proc = mock_proc

    # Don't resolve the future so it times out
    with patch.object(asyncio, "wait_for", side_effect=asyncio.TimeoutError):
        result = await adapter._send_command("slow_action")

    assert "error" in result
    assert "timed out" in result["error"].lower()


# ── Slash config ────────────────────────────────────────────────────


async def test_write_slash_config(adapter):
    path = await adapter._write_slash_config()
    assert path is not None

    import os

    assert os.path.exists(path)
    with open(path) as f:
        commands = json.load(f)
    names = [c["name"] for c in commands]
    assert "paw" in names
    assert "new" in names
    assert "kill" in names

    os.unlink(path)


# ── Eviction loop ──────────────────────────────────────────────────


def test_stale_history_eviction(convo_adapter):
    """Manually test that stale channels would be evicted."""
    import time as t

    convo_adapter._add_to_history(100, "alice", "hello")
    convo_adapter._add_to_history(200, "bob", "hi")

    # Simulate staleness by backdating
    convo_adapter._conversation_last_active[100] = t.monotonic() - 7200

    # Eviction logic extracted
    from pocketpaw.bus.adapters.discord_adapter import _IDLE_CHANNEL_TTL

    now = t.monotonic()
    stale = [
        cid
        for cid, last in convo_adapter._conversation_last_active.items()
        if now - last > _IDLE_CHANNEL_TTL
    ]
    assert 100 in stale
    assert 200 not in stale


# ── New v0.7.0 event handler tests ────────────────────────────────


async def test_handle_component_interaction(adapter, bus):
    """component_interaction events are routed to the agent as InboundMessage."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"
    adapter._send_command = AsyncMock(return_value={"ok": True})

    await adapter._handle_component_interaction(
        {
            "custom_id": "ok_btn",
            "channel_id": "100",
            "user_id": "999",
            "user": "testuser",
            "guild_id": "111",
            "interaction_token": "comp_tok1",
            "message_id": "msg123",
        }
    )

    assert bus.inbound_pending() == 1
    msg = await bus.consume_inbound()
    assert "ok_btn" in msg.content
    assert msg.metadata["interaction_token"] == "comp_tok1"
    assert msg.metadata["component_interaction"] is True


async def test_handle_component_interaction_with_values(adapter, bus):
    """Select menu interactions include selected values."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"
    adapter._send_command = AsyncMock(return_value={"ok": True})

    await adapter._handle_component_interaction(
        {
            "custom_id": "color_select",
            "channel_id": "100",
            "user_id": "999",
            "user": "testuser",
            "guild_id": "111",
            "interaction_token": "comp_tok2",
            "values": ["red", "blue"],
        }
    )

    msg = await bus.consume_inbound()
    assert "red" in msg.content
    assert "blue" in msg.content


async def test_handle_component_interaction_unauthorized(adapter, bus):
    """Unauthorized component interactions are rejected."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"
    adapter._send_command = AsyncMock(return_value={"ok": True})

    await adapter._handle_component_interaction(
        {
            "custom_id": "ok_btn",
            "channel_id": "100",
            "user_id": "666",  # not in allowed_user_ids
            "guild_id": "111",
            "interaction_token": "comp_tok3",
        }
    )

    assert bus.inbound_pending() == 0


async def test_handle_modal_submit(adapter, bus):
    """modal_submit events are routed to the agent as InboundMessage."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"
    adapter._send_command = AsyncMock(return_value={"ok": True})

    await adapter._handle_modal_submit(
        {
            "custom_id": "feedback_form",
            "channel_id": "100",
            "user_id": "999",
            "user": "testuser",
            "guild_id": "111",
            "interaction_token": "modal_tok1",
            "fields": {"rating": "5", "comment": "Great bot!"},
        }
    )

    assert bus.inbound_pending() == 1
    msg = await bus.consume_inbound()
    assert "feedback_form" in msg.content
    assert "Great bot!" in msg.content
    assert msg.metadata["interaction_token"] == "modal_tok1"
    assert msg.metadata["modal_submit"] is True


async def test_handle_modal_submit_unauthorized(adapter, bus):
    """Unauthorized modal submissions are rejected."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"
    adapter._send_command = AsyncMock(return_value={"ok": True})

    await adapter._handle_modal_submit(
        {
            "custom_id": "feedback_form",
            "channel_id": "100",
            "user_id": "666",
            "guild_id": "111",
            "interaction_token": "modal_tok2",
            "fields": {"rating": "5"},
        }
    )

    assert bus.inbound_pending() == 0


async def test_handle_voice_state_is_logged_not_routed(adapter, bus):
    """voice_state events are logged but not routed to agent."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)
    adapter._bot_id = "BOT1"

    # Should not raise, should not publish
    adapter._handle_voice_state(
        {
            "user_id": "999",
            "action": "join",
            "channel_id": "vc1",
            "guild_id": "111",
        }
    )

    assert bus.inbound_pending() == 0


async def test_handle_disconnected_sets_status(adapter):
    """disconnected event logs warning."""
    adapter._bot_id = "BOT1"
    adapter._handle_disconnected({"reason": "network"})
    # No crash, status tracked
    assert adapter._disconnected is True


async def test_handle_resumed_clears_status(adapter):
    """resumed event clears disconnected status."""
    adapter._bot_id = "BOT1"
    adapter._disconnected = True
    adapter._handle_resumed({})
    assert adapter._disconnected is False
