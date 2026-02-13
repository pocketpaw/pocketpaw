"""Tests for cross-channel command handler and session aliases."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.bus.events import Channel, InboundMessage, OutboundMessage
from pocketclaw.memory.file_store import FileMemoryStore

# =========================================================================
# Helpers
# =========================================================================


def _make_msg(content: str, channel=Channel.DISCORD, chat_id="12345") -> InboundMessage:
    return InboundMessage(
        channel=channel,
        sender_id="user1",
        chat_id=chat_id,
        content=content,
    )


# =========================================================================
# is_command parsing
# =========================================================================


class TestIsCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    def test_recognises_new(self):
        assert self.handler.is_command("/new")

    def test_recognises_sessions(self):
        assert self.handler.is_command("/sessions")

    def test_recognises_resume(self):
        assert self.handler.is_command("/resume")

    def test_recognises_resume_with_arg(self):
        assert self.handler.is_command("/resume 3")

    def test_recognises_help(self):
        assert self.handler.is_command("/help")

    def test_rejects_unknown_command(self):
        assert not self.handler.is_command("/unknown")

    def test_rejects_plain_text(self):
        assert not self.handler.is_command("hello world")

    def test_rejects_empty(self):
        assert not self.handler.is_command("")

    def test_handles_bot_suffix(self):
        assert self.handler.is_command("/new@PocketPawBot")

    def test_handles_bot_suffix_with_args(self):
        assert self.handler.is_command("/resume@PocketPawBot 3")

    def test_case_insensitive(self):
        assert self.handler.is_command("/NEW")
        assert self.handler.is_command("/Sessions")
        assert self.handler.is_command("/RESUME 1")

    def test_leading_whitespace(self):
        assert self.handler.is_command("  /new")


# =========================================================================
# Session Aliases (FileMemoryStore)
# =========================================================================


class TestSessionAliases:
    def setup_method(self):
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.store = FileMemoryStore(base_path=Path(self.tmpdir))

    async def test_resolve_returns_original_when_no_alias(self):
        result = await self.store.resolve_session_alias("discord:123")
        assert result == "discord:123"

    async def test_set_and_resolve(self):
        await self.store.set_session_alias("discord:123", "discord:123:abc")
        result = await self.store.resolve_session_alias("discord:123")
        assert result == "discord:123:abc"

    async def test_overwrite_alias(self):
        await self.store.set_session_alias("discord:123", "discord:123:abc")
        await self.store.set_session_alias("discord:123", "discord:123:def")
        result = await self.store.resolve_session_alias("discord:123")
        assert result == "discord:123:def"

    async def test_remove_alias(self):
        await self.store.set_session_alias("discord:123", "discord:123:abc")
        removed = await self.store.remove_session_alias("discord:123")
        assert removed is True
        result = await self.store.resolve_session_alias("discord:123")
        assert result == "discord:123"

    async def test_remove_nonexistent(self):
        removed = await self.store.remove_session_alias("discord:999")
        assert removed is False

    async def test_aliases_persist_to_disk(self):
        await self.store.set_session_alias("discord:123", "discord:123:abc")
        # Read the file directly
        data = json.loads(self.store._aliases_path.read_text())
        assert data["discord:123"] == "discord:123:abc"

    async def test_get_session_keys_includes_alias_targets(self):
        await self.store.set_session_alias("discord:123", "discord:123:abc")
        keys = await self.store.get_session_keys_for_chat("discord:123")
        assert "discord:123:abc" in keys

    async def test_concurrent_alias_writes(self):
        """Multiple concurrent alias writes don't corrupt the file."""

        async def _write(i):
            await self.store.set_session_alias(f"key:{i}", f"target:{i}")

        await asyncio.gather(*[_write(i) for i in range(10)])

        aliases = self.store._load_aliases()
        assert len(aliases) == 10
        for i in range(10):
            assert aliases[f"key:{i}"] == f"target:{i}"


# =========================================================================
# /new command
# =========================================================================


class TestNewCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_new_creates_alias(self, mock_get_mm):
        mm = MagicMock()
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        msg = _make_msg("/new")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "new conversation" in response.content.lower()
        mm.set_session_alias.assert_called_once()
        call_args = mm.set_session_alias.call_args
        assert call_args[0][0] == "discord:12345"
        assert call_args[0][1].startswith("discord:12345:")

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_new_with_bot_suffix(self, mock_get_mm):
        mm = MagicMock()
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        msg = _make_msg("/new@PocketPawBot")
        response = await self.handler.handle(msg)

        assert response is not None
        mm.set_session_alias.assert_called_once()


# =========================================================================
# /sessions command
# =========================================================================


class TestSessionsCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_sessions_empty(self, mock_get_mm):
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        msg = _make_msg("/sessions")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "no sessions" in response.content.lower()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_sessions_formatted_output(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Debug the API",
                "last_activity": "2026-02-12T10:00:00",
                "message_count": 5,
                "preview": "Let me check...",
                "is_active": True,
            },
            {
                "session_key": "discord:123:def",
                "title": "Write tests",
                "last_activity": "2026-02-11T10:00:00",
                "message_count": 3,
                "preview": "Sure thing",
                "is_active": False,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        msg = _make_msg("/sessions")
        response = await self.handler.handle(msg)

        assert "Debug the API" in response.content
        assert "Write tests" in response.content
        assert "(active)" in response.content
        assert "5 msgs" in response.content

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_sessions_stores_last_shown(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Chat 1",
                "last_activity": "",
                "message_count": 1,
                "preview": "",
                "is_active": True,
            }
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        msg = _make_msg("/sessions")
        await self.handler.handle(msg)

        assert "discord:12345" in self.handler._last_shown


# =========================================================================
# /resume command
# =========================================================================


class TestResumeCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_resume_no_args_shows_sessions(self, mock_get_mm):
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        msg = _make_msg("/resume")
        response = await self.handler.handle(msg)

        assert response is not None
        # Should behave like /sessions
        mm.list_sessions_for_chat.assert_called_once()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_resume_valid_number(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "First Chat",
                "last_activity": "",
                "message_count": 2,
                "preview": "",
                "is_active": False,
            },
            {
                "session_key": "discord:123:def",
                "title": "Second Chat",
                "last_activity": "",
                "message_count": 1,
                "preview": "",
                "is_active": True,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        # Pre-populate _last_shown
        self.handler._last_shown["discord:12345"] = sessions

        msg = _make_msg("/resume 1")
        response = await self.handler.handle(msg)

        assert "First Chat" in response.content
        mm.set_session_alias.assert_called_once_with("discord:12345", "discord:123:abc")

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_resume_invalid_number(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Chat",
                "last_activity": "",
                "message_count": 1,
                "preview": "",
                "is_active": True,
            }
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        self.handler._last_shown["discord:12345"] = sessions

        msg = _make_msg("/resume 5")
        response = await self.handler.handle(msg)

        assert "invalid" in response.content.lower()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_resume_text_search_single(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Debug the API",
                "last_activity": "",
                "message_count": 5,
                "preview": "",
                "is_active": False,
            },
            {
                "session_key": "discord:123:def",
                "title": "Write tests",
                "last_activity": "",
                "message_count": 3,
                "preview": "",
                "is_active": True,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        msg = _make_msg("/resume debug")
        response = await self.handler.handle(msg)

        assert "Debug the API" in response.content
        mm.set_session_alias.assert_called_once()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_resume_text_search_multi(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Write tests A",
                "last_activity": "",
                "message_count": 5,
                "preview": "",
                "is_active": False,
            },
            {
                "session_key": "discord:123:def",
                "title": "Write tests B",
                "last_activity": "",
                "message_count": 3,
                "preview": "",
                "is_active": True,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        msg = _make_msg("/resume write")
        response = await self.handler.handle(msg)

        assert "multiple" in response.content.lower()
        assert "Write tests A" in response.content
        assert "Write tests B" in response.content

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_resume_text_search_no_match(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Debug the API",
                "last_activity": "",
                "message_count": 5,
                "preview": "",
                "is_active": True,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        msg = _make_msg("/resume foobar")
        response = await self.handler.handle(msg)

        assert "no sessions matching" in response.content.lower()


# =========================================================================
# /help command
# =========================================================================


class TestHelpCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    async def test_help_lists_commands(self):
        msg = _make_msg("/help")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "/new" in response.content
        assert "/sessions" in response.content
        assert "/resume" in response.content
        assert "/clear" in response.content
        assert "/rename" in response.content
        assert "/status" in response.content
        assert "/delete" in response.content
        assert "/help" in response.content


# =========================================================================
# /clear command
# =========================================================================


class TestClearCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_clear_with_messages(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.clear_session = AsyncMock(return_value=7)
        mock_get_mm.return_value = mm

        msg = _make_msg("/clear")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "7 messages" in response.content
        mm.clear_session.assert_called_once_with("discord:12345")

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_clear_empty_session(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.clear_session = AsyncMock(return_value=0)
        mock_get_mm.return_value = mm

        msg = _make_msg("/clear")
        response = await self.handler.handle(msg)

        assert "already empty" in response.content.lower()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_clear_resolves_alias(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345:abc")
        mm.clear_session = AsyncMock(return_value=3)
        mock_get_mm.return_value = mm

        msg = _make_msg("/clear")
        await self.handler.handle(msg)

        mm.clear_session.assert_called_once_with("discord:12345:abc")


# =========================================================================
# /rename command
# =========================================================================


class TestRenameCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_rename_success(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.update_session_title = AsyncMock(return_value=True)
        mock_get_mm.return_value = mm

        msg = _make_msg("/rename My Cool Chat")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "My Cool Chat" in response.content
        mm.update_session_title.assert_called_once_with("discord:12345", "My Cool Chat")

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_rename_no_args(self, mock_get_mm):
        mock_get_mm.return_value = MagicMock()

        msg = _make_msg("/rename")
        response = await self.handler.handle(msg)

        assert "usage" in response.content.lower()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_rename_session_not_found(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.update_session_title = AsyncMock(return_value=False)
        mock_get_mm.return_value = mm

        msg = _make_msg("/rename New Title")
        response = await self.handler.handle(msg)

        assert "not found" in response.content.lower()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_rename_with_bot_suffix(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.update_session_title = AsyncMock(return_value=True)
        mock_get_mm.return_value = mm

        msg = _make_msg("/rename@PocketPawBot New Title")
        response = await self.handler.handle(msg)

        assert "New Title" in response.content


# =========================================================================
# /status command
# =========================================================================


class TestStatusCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketclaw.config.get_settings")
    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_status_with_active_session(self, mock_get_mm, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings

        sessions = [
            {
                "session_key": "discord:12345:abc",
                "title": "Debug the API",
                "last_activity": "",
                "message_count": 5,
                "preview": "",
                "is_active": True,
            }
        ]
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345:abc")
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        msg = _make_msg("/status")
        response = await self.handler.handle(msg)

        assert "Debug the API" in response.content
        assert "5" in response.content
        assert "claude_agent_sdk" in response.content
        assert "discord" in response.content

    @patch("pocketclaw.config.get_settings")
    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_status_no_sessions(self, mock_get_mm, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "pocketpaw_native"
        mock_settings.return_value = settings

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        msg = _make_msg("/status")
        response = await self.handler.handle(msg)

        assert "Default" in response.content
        assert "pocketpaw_native" in response.content

    @patch("pocketclaw.config.get_settings")
    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_status_shows_aliased_key(self, mock_get_mm, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345:abc")
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        msg = _make_msg("/status")
        response = await self.handler.handle(msg)

        # When aliased, both keys should appear
        assert "discord:12345:abc" in response.content
        assert "discord:12345" in response.content


# =========================================================================
# /delete command
# =========================================================================


class TestDeleteCommand:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_delete_success(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345:abc")
        mm.delete_session = AsyncMock(return_value=True)
        mm._store = MagicMock()
        mm._store.remove_session_alias = AsyncMock(return_value=True)
        mock_get_mm.return_value = mm

        msg = _make_msg("/delete")
        response = await self.handler.handle(msg)

        assert "deleted" in response.content.lower()
        mm.delete_session.assert_called_once_with("discord:12345:abc")
        mm._store.remove_session_alias.assert_called_once_with("discord:12345")

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_delete_nothing(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.delete_session = AsyncMock(return_value=False)
        mm._store = MagicMock()
        mm._store.remove_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        msg = _make_msg("/delete")
        response = await self.handler.handle(msg)

        assert "no session" in response.content.lower()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_delete_removes_alias(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345:xyz")
        mm.delete_session = AsyncMock(return_value=True)
        mm._store = MagicMock()
        mm._store.remove_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        msg = _make_msg("/delete")
        await self.handler.handle(msg)

        mm._store.remove_session_alias.assert_called_once_with("discord:12345")


# =========================================================================
# is_command for new commands
# =========================================================================


class TestIsCommandNewCommands:
    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    def test_recognises_clear(self):
        assert self.handler.is_command("/clear")

    def test_recognises_rename(self):
        assert self.handler.is_command("/rename My Chat")

    def test_recognises_status(self):
        assert self.handler.is_command("/status")

    def test_recognises_delete(self):
        assert self.handler.is_command("/delete")


# =========================================================================
# ! prefix fallback
# =========================================================================


class TestBangPrefixFallback:
    """Commands with ! prefix should work identically to / prefix."""

    def setup_method(self):
        from pocketclaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    def test_recognises_bang_new(self):
        assert self.handler.is_command("!new")

    def test_recognises_bang_sessions(self):
        assert self.handler.is_command("!sessions")

    def test_recognises_bang_resume_with_arg(self):
        assert self.handler.is_command("!resume 3")

    def test_recognises_bang_help(self):
        assert self.handler.is_command("!help")

    def test_recognises_bang_clear(self):
        assert self.handler.is_command("!clear")

    def test_recognises_bang_rename(self):
        assert self.handler.is_command("!rename My Chat")

    def test_recognises_bang_status(self):
        assert self.handler.is_command("!status")

    def test_recognises_bang_delete(self):
        assert self.handler.is_command("!delete")

    def test_rejects_bang_unknown(self):
        assert not self.handler.is_command("!foobar")

    def test_bang_with_bot_suffix(self):
        assert self.handler.is_command("!new@PocketPawBot")

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_bang_new_works(self, mock_get_mm):
        mm = MagicMock()
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        msg = _make_msg("!new")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "new conversation" in response.content.lower()
        mm.set_session_alias.assert_called_once()

    @patch("pocketclaw.bus.commands.get_memory_manager")
    async def test_bang_resume_works(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "First Chat",
                "last_activity": "",
                "message_count": 2,
                "preview": "",
                "is_active": False,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        self.handler._last_shown["discord:12345"] = sessions

        msg = _make_msg("!resume 1")
        response = await self.handler.handle(msg)

        assert "First Chat" in response.content
        mm.set_session_alias.assert_called_once()

    async def test_bang_help_works(self):
        msg = _make_msg("!help")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "/new" in response.content
        assert "!command" in response.content


# =========================================================================
# Slack slash command handler
# =========================================================================


class TestSlackSlashCommands:
    """Verify SlackAdapter registers native slash commands that publish InboundMessages."""

    async def test_slash_handler_publishes_inbound(self):
        """The Slack @app.command handler acks and publishes an InboundMessage."""
        # We can't easily start the full Slack app, but we can simulate
        # the handler logic that _on_start registers. The key contract is:
        # given a command dict, it builds an InboundMessage with the right content.

        # Simulate what _slash_handler does internally
        command = {
            "text": "3",
            "channel_id": "C12345",
            "user_id": "U67890",
            "thread_ts": None,
        }

        # Reproduce handler logic
        _cmd = "/resume"
        text = command.get("text", "").strip()
        content = f"{_cmd} {text}" if text else _cmd
        ch_id = command.get("channel_id", "")
        user = command.get("user_id", "")

        msg = InboundMessage(
            channel=Channel.SLACK,
            sender_id=user,
            chat_id=ch_id,
            content=content,
            metadata={"channel_id": ch_id},
        )

        assert msg.content == "/resume 3"
        assert msg.chat_id == "C12345"
        assert msg.sender_id == "U67890"

    async def test_slash_handler_no_text(self):
        """Command with no args uses just the command name."""
        _cmd = "/new"
        text = ""
        content = f"{_cmd} {text}" if text else _cmd

        assert content == "/new"

    async def test_slash_handler_with_thread(self):
        """Thread_ts propagates in metadata."""
        command = {
            "text": "",
            "channel_id": "C12345",
            "user_id": "U67890",
            "thread_ts": "1234567890.123456",
        }

        meta = {"channel_id": command["channel_id"]}
        if command.get("thread_ts"):
            meta["thread_ts"] = command["thread_ts"]

        assert meta["thread_ts"] == "1234567890.123456"

    async def test_all_commands_registered(self):
        """All 8 commands should be in the registration loop."""
        import ast

        from pocketclaw.bus.adapters import slack_adapter

        source = ast.parse(Path(slack_adapter.__file__).read_text())

        # Find the tuple of command names in the for loop
        expected = {
            "/new",
            "/sessions",
            "/resume",
            "/clear",
            "/rename",
            "/status",
            "/delete",
            "/help",
        }
        found = set()
        for node in ast.walk(source):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("/") and node.value in expected:
                    found.add(node.value)

        assert found == expected


# =========================================================================
# AgentLoop integration
# =========================================================================


class TestAgentLoopCommandIntegration:
    @patch("pocketclaw.agents.loop.get_injection_scanner")
    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_command_intercepted_before_agent(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Commands should be handled without invoking the agent backend."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_system = AsyncMock()
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.add_to_session = AsyncMock()
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        cmd_handler.is_command.return_value = True
        cmd_handler.handle = AsyncMock(
            return_value=OutboundMessage(
                channel=Channel.DISCORD,
                chat_id="12345",
                content="Started a new conversation.",
            )
        )
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("/new")

        await loop._process_message_inner(msg, "discord:12345")

        # Command response was published
        bus.publish_outbound.assert_called()
        calls = bus.publish_outbound.call_args_list
        # First call: the command response, second call: stream_end
        assert "new conversation" in calls[0][0][0].content.lower()
        assert calls[1][0][0].is_stream_end is True

        # Agent was NOT invoked (no add_to_session for user message)
        mm.add_to_session.assert_not_called()

    @patch("pocketclaw.agents.loop.get_injection_scanner")
    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_normal_message_not_intercepted(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Non-command messages should pass through to the agent."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.welcome_hint_enabled = False
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_system = AsyncMock()
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.add_to_session = AsyncMock()
        mm.get_compacted_history = AsyncMock(return_value=[])
        mm.get_session_history = AsyncMock(return_value=[])
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        cmd_handler.is_command.return_value = False
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("hello world")

        # This will try to run the agent, which we'll let fail gracefully
        with patch.object(loop, "_get_router") as mock_router:
            router = MagicMock()

            async def _empty_gen():
                yield {"type": "done", "content": ""}

            router.run.return_value = _empty_gen()
            router.stop = AsyncMock()
            mock_router.return_value = router

            with patch.object(loop, "context_builder") as mock_ctx:
                mock_ctx.memory = mm
                mock_ctx.build_system_prompt = AsyncMock(return_value="sys prompt")
                await loop._process_message_inner(msg, "discord:12345")

        # User message WAS stored in memory
        mm.add_to_session.assert_called()

    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_alias_resolved_for_session_lock(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn
    ):
        """_process_message should resolve alias before acquiring session lock."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.consume_inbound = AsyncMock(return_value=None)
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345:abc123")
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("/new")

        # Mock _process_message_inner to just track what session_key it receives
        received_keys = []

        async def _capture_inner(message, session_key):
            received_keys.append(session_key)

        loop._process_message_inner = _capture_inner

        await loop._process_message(msg)

        assert received_keys == ["discord:12345:abc123"]
        mm.resolve_session_key.assert_called_once_with("discord:12345")


# =========================================================================
# MemoryManager alias pass-through
# =========================================================================


class TestMemoryManagerAliases:
    def setup_method(self):
        import tempfile

        from pocketclaw.memory.manager import MemoryManager

        self.tmpdir = tempfile.mkdtemp()
        self.store = FileMemoryStore(base_path=Path(self.tmpdir))
        self.mm = MemoryManager(store=self.store)

    async def test_resolve_no_alias(self):
        result = await self.mm.resolve_session_key("discord:123")
        assert result == "discord:123"

    async def test_resolve_with_alias(self):
        await self.store.set_session_alias("discord:123", "discord:123:abc")
        result = await self.mm.resolve_session_key("discord:123")
        assert result == "discord:123:abc"

    async def test_list_sessions_empty(self):
        result = await self.mm.list_sessions_for_chat("discord:123")
        assert result == []

    async def test_list_sessions_with_data(self):
        from pocketclaw.memory.protocol import MemoryEntry, MemoryType

        # Create a session via alias
        await self.store.set_session_alias("discord:123", "discord:123:abc")
        # Write a message to the aliased session
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="Hello",
            role="user",
            session_key="discord:123:abc",
        )
        await self.store.save(entry)

        result = await self.mm.list_sessions_for_chat("discord:123")
        assert len(result) >= 1
        keys = [s["session_key"] for s in result]
        assert "discord:123:abc" in keys


# =========================================================================
# Welcome Hint in AgentLoop
# =========================================================================


class TestWelcomeHint:
    """Test the one-time welcome hint on first channel interaction."""

    @patch("pocketclaw.agents.loop.get_injection_scanner")
    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_welcome_on_new_discord_session(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """First message on Discord should trigger a welcome hint."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.welcome_hint_enabled = True
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_system = AsyncMock()
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.add_to_session = AsyncMock()
        mm.get_compacted_history = AsyncMock(return_value=[])
        mm.get_session_history = AsyncMock(return_value=[])  # empty = new session
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        cmd_handler.is_command.return_value = False
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("hello", channel=Channel.DISCORD)

        with patch.object(loop, "_get_router") as mock_router:
            router = MagicMock()

            async def _empty_gen():
                yield {"type": "done", "content": ""}

            router.run.return_value = _empty_gen()
            router.stop = AsyncMock()
            mock_router.return_value = router

            with patch.object(loop, "context_builder") as mock_ctx:
                mock_ctx.memory = mm
                mock_ctx.build_system_prompt = AsyncMock(return_value="sys prompt")
                await loop._process_message_inner(msg, "discord:12345")

        # Welcome was published
        outbound_calls = bus.publish_outbound.call_args_list
        welcome_found = any("Welcome to PocketPaw" in str(c) for c in outbound_calls)
        assert welcome_found, f"Expected welcome hint in {outbound_calls}"

    @patch("pocketclaw.agents.loop.get_injection_scanner")
    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_no_welcome_on_existing_session(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Existing session should NOT get welcome hint."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.welcome_hint_enabled = True
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_system = AsyncMock()
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.add_to_session = AsyncMock()
        mm.get_compacted_history = AsyncMock(return_value=[])
        mm.get_session_history = AsyncMock(return_value=[{"role": "user", "content": "old msg"}])
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        cmd_handler.is_command.return_value = False
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("hello", channel=Channel.DISCORD)

        with patch.object(loop, "_get_router") as mock_router:
            router = MagicMock()

            async def _empty_gen():
                yield {"type": "done", "content": ""}

            router.run.return_value = _empty_gen()
            router.stop = AsyncMock()
            mock_router.return_value = router

            with patch.object(loop, "context_builder") as mock_ctx:
                mock_ctx.memory = mm
                mock_ctx.build_system_prompt = AsyncMock(return_value="sys prompt")
                await loop._process_message_inner(msg, "discord:12345")

        outbound_calls = bus.publish_outbound.call_args_list
        welcome_found = any("Welcome to PocketPaw" in str(c) for c in outbound_calls)
        assert not welcome_found

    @patch("pocketclaw.agents.loop.get_injection_scanner")
    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_no_welcome_on_websocket(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """WebSocket channel should never get welcome hint."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.welcome_hint_enabled = True
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_system = AsyncMock()
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="websocket:12345")
        mm.add_to_session = AsyncMock()
        mm.get_compacted_history = AsyncMock(return_value=[])
        mm.get_session_history = AsyncMock(return_value=[])  # empty, but excluded
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        cmd_handler.is_command.return_value = False
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("hello", channel=Channel.WEBSOCKET, chat_id="12345")

        with patch.object(loop, "_get_router") as mock_router:
            router = MagicMock()

            async def _empty_gen():
                yield {"type": "done", "content": ""}

            router.run.return_value = _empty_gen()
            router.stop = AsyncMock()
            mock_router.return_value = router

            with patch.object(loop, "context_builder") as mock_ctx:
                mock_ctx.memory = mm
                mock_ctx.build_system_prompt = AsyncMock(return_value="sys prompt")
                await loop._process_message_inner(msg, "websocket:12345")

        # get_session_history should NOT have been called (channel excluded)
        mm.get_session_history.assert_not_called()

        outbound_calls = bus.publish_outbound.call_args_list
        welcome_found = any("Welcome to PocketPaw" in str(c) for c in outbound_calls)
        assert not welcome_found

    @patch("pocketclaw.agents.loop.get_injection_scanner")
    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_no_welcome_when_disabled(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """welcome_hint_enabled=False should suppress the hint."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.welcome_hint_enabled = False
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_system = AsyncMock()
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.add_to_session = AsyncMock()
        mm.get_compacted_history = AsyncMock(return_value=[])
        mm.get_session_history = AsyncMock(return_value=[])
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        cmd_handler.is_command.return_value = False
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("hello", channel=Channel.DISCORD)

        with patch.object(loop, "_get_router") as mock_router:
            router = MagicMock()

            async def _empty_gen():
                yield {"type": "done", "content": ""}

            router.run.return_value = _empty_gen()
            router.stop = AsyncMock()
            mock_router.return_value = router

            with patch.object(loop, "context_builder") as mock_ctx:
                mock_ctx.memory = mm
                mock_ctx.build_system_prompt = AsyncMock(return_value="sys prompt")
                await loop._process_message_inner(msg, "discord:12345")

        # get_session_history should NOT have been called (feature disabled)
        mm.get_session_history.assert_not_called()

        outbound_calls = bus.publish_outbound.call_args_list
        welcome_found = any("Welcome to PocketPaw" in str(c) for c in outbound_calls)
        assert not welcome_found

    @patch("pocketclaw.agents.loop.get_injection_scanner")
    @patch("pocketclaw.agents.loop.get_command_handler")
    @patch("pocketclaw.agents.loop.get_memory_manager")
    @patch("pocketclaw.agents.loop.get_message_bus")
    @patch("pocketclaw.agents.loop.get_settings")
    async def test_welcome_not_stored_in_memory(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Welcome hint must not be stored in session memory."""
        from pocketclaw.agents.loop import AgentLoop

        settings = MagicMock()
        settings.max_concurrent_conversations = 5
        settings.injection_scan_enabled = False
        settings.welcome_hint_enabled = True
        mock_settings.return_value = settings

        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        bus.publish_system = AsyncMock()
        mock_bus_fn.return_value = bus

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.add_to_session = AsyncMock()
        mm.get_compacted_history = AsyncMock(return_value=[])
        mm.get_session_history = AsyncMock(return_value=[])
        mock_mm_fn.return_value = mm

        cmd_handler = MagicMock()
        cmd_handler.is_command.return_value = False
        mock_cmd_fn.return_value = cmd_handler

        loop = AgentLoop()
        msg = _make_msg("hello", channel=Channel.DISCORD)

        with patch.object(loop, "_get_router") as mock_router:
            router = MagicMock()

            async def _empty_gen():
                yield {"type": "message", "content": "Hi!"}
                yield {"type": "done", "content": ""}

            router.run.return_value = _empty_gen()
            router.stop = AsyncMock()
            mock_router.return_value = router

            with patch.object(loop, "context_builder") as mock_ctx:
                mock_ctx.memory = mm
                mock_ctx.build_system_prompt = AsyncMock(return_value="sys prompt")
                await loop._process_message_inner(msg, "discord:12345")

        # add_to_session should be called for user msg + assistant response
        # but NOT for the welcome hint
        for call in mm.add_to_session.call_args_list:
            content = call.kwargs.get("content") or call[1].get("content", "")
            assert "Welcome to PocketPaw" not in content
