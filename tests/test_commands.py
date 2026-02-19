"""Tests for cross-channel command handler and session aliases."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pocketpaw.bus.events import Channel, InboundMessage, OutboundMessage
from pocketpaw.memory.file_store import FileMemoryStore

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
        from pocketpaw.bus.commands import CommandHandler

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
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.bus.commands.get_memory_manager")
    async def test_sessions_empty(self, mock_get_mm):
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        msg = _make_msg("/sessions")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "no sessions" in response.content.lower()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.bus.commands.get_memory_manager")
    async def test_resume_no_args_shows_sessions(self, mock_get_mm):
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        msg = _make_msg("/resume")
        response = await self.handler.handle(msg)

        assert response is not None
        # Should behave like /sessions
        mm.list_sessions_for_chat.assert_called_once()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        from pocketpaw.bus.commands import CommandHandler

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
        assert "/backend" in response.content
        assert "/backends" in response.content
        assert "/model" in response.content
        assert "/tools" in response.content
        assert "/help" in response.content


# =========================================================================
# /clear command
# =========================================================================


class TestClearCommand:
    def setup_method(self):
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
    async def test_clear_empty_session(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.clear_session = AsyncMock(return_value=0)
        mock_get_mm.return_value = mm

        msg = _make_msg("/clear")
        response = await self.handler.handle(msg)

        assert "already empty" in response.content.lower()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
    async def test_rename_no_args(self, mock_get_mm):
        mock_get_mm.return_value = MagicMock()

        msg = _make_msg("/rename")
        response = await self.handler.handle(msg)

        assert "usage" in response.content.lower()

    @patch("pocketpaw.bus.commands.get_memory_manager")
    async def test_rename_session_not_found(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.update_session_title = AsyncMock(return_value=False)
        mock_get_mm.return_value = mm

        msg = _make_msg("/rename New Title")
        response = await self.handler.handle(msg)

        assert "not found" in response.content.lower()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.bus.commands.get_memory_manager")
    async def test_status_no_sessions(self, mock_get_mm, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings

        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:12345")
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        msg = _make_msg("/status")
        response = await self.handler.handle(msg)

        assert "Default" in response.content
        assert "claude_agent_sdk" in response.content

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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

    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        from pocketpaw.bus.commands import CommandHandler

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
        from pocketpaw.bus.commands import CommandHandler

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

    @patch("pocketpaw.bus.commands.get_memory_manager")
    async def test_bang_new_works(self, mock_get_mm):
        mm = MagicMock()
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        msg = _make_msg("!new")
        response = await self.handler.handle(msg)

        assert response is not None
        assert "new conversation" in response.content.lower()
        mm.set_session_alias.assert_called_once()

    @patch("pocketpaw.bus.commands.get_memory_manager")
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
        """All 12 commands should be in the registration loop."""
        import ast

        from pocketpaw.bus.adapters import slack_adapter

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
            "/backend",
            "/backends",
            "/model",
            "/tools",
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
    @patch("pocketpaw.agents.loop.get_injection_scanner")
    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_command_intercepted_before_agent(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Commands should be handled without invoking the agent backend."""
        from pocketpaw.agents.loop import AgentLoop

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

    @patch("pocketpaw.agents.loop.get_injection_scanner")
    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_normal_message_not_intercepted(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Non-command messages should pass through to the agent."""
        from pocketpaw.agents.loop import AgentLoop

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

    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_alias_resolved_for_session_lock(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn
    ):
        """_process_message should resolve alias before acquiring session lock."""
        from pocketpaw.agents.loop import AgentLoop

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

        from pocketpaw.memory.manager import MemoryManager

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
        from pocketpaw.memory.protocol import MemoryEntry, MemoryType

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

    @patch("pocketpaw.agents.loop.get_injection_scanner")
    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_welcome_on_new_discord_session(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """First message on Discord should trigger a welcome hint."""
        from pocketpaw.agents.loop import AgentLoop

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

    @patch("pocketpaw.agents.loop.get_injection_scanner")
    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_no_welcome_on_existing_session(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Existing session should NOT get welcome hint."""
        from pocketpaw.agents.loop import AgentLoop

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

    @patch("pocketpaw.agents.loop.get_injection_scanner")
    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_no_welcome_on_websocket(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """WebSocket channel should never get welcome hint."""
        from pocketpaw.agents.loop import AgentLoop

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

    @patch("pocketpaw.agents.loop.get_injection_scanner")
    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_no_welcome_when_disabled(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """welcome_hint_enabled=False should suppress the hint."""
        from pocketpaw.agents.loop import AgentLoop

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

    @patch("pocketpaw.agents.loop.get_injection_scanner")
    @patch("pocketpaw.agents.loop.get_command_handler")
    @patch("pocketpaw.agents.loop.get_memory_manager")
    @patch("pocketpaw.agents.loop.get_message_bus")
    @patch("pocketpaw.agents.loop.get_settings")
    async def test_welcome_not_stored_in_memory(
        self, mock_settings, mock_bus_fn, mock_mm_fn, mock_cmd_fn, mock_scanner_fn
    ):
        """Welcome hint must not be stored in session memory."""
        from pocketpaw.agents.loop import AgentLoop

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


# =========================================================================
# is_command for settings commands
# =========================================================================


class TestIsCommandSettingsCommands:
    def setup_method(self):
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    def test_recognises_backend(self):
        assert self.handler.is_command("/backend")

    def test_recognises_backend_with_arg(self):
        assert self.handler.is_command("/backend openai_agents")

    def test_recognises_backends(self):
        assert self.handler.is_command("/backends")

    def test_recognises_model(self):
        assert self.handler.is_command("/model")

    def test_recognises_model_with_arg(self):
        assert self.handler.is_command("/model gpt-4o")

    def test_recognises_tools(self):
        assert self.handler.is_command("/tools")

    def test_recognises_tools_with_arg(self):
        assert self.handler.is_command("/tools minimal")

    def test_bang_backend(self):
        assert self.handler.is_command("!backend")

    def test_bang_backends(self):
        assert self.handler.is_command("!backends")

    def test_bang_model(self):
        assert self.handler.is_command("!model gpt-4o")

    def test_bang_tools(self):
        assert self.handler.is_command("!tools full")


# =========================================================================
# /backends command
# =========================================================================


class TestBackendsCommand:
    def setup_method(self):
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.agents.registry.get_backend_info")
    @patch("pocketpaw.agents.registry.get_backend_class")
    @patch("pocketpaw.agents.registry.list_backends")
    async def test_lists_backends(self, mock_list, mock_cls, mock_info, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings

        mock_list.return_value = ["claude_agent_sdk", "openai_agents"]

        from pocketpaw.agents.backend import Capability

        info1 = MagicMock()
        info1.display_name = "Claude Agent SDK"
        info1.capabilities = Capability.STREAMING | Capability.TOOLS
        info2 = MagicMock()
        info2.display_name = "OpenAI Agents"
        info2.capabilities = Capability.STREAMING
        mock_info.side_effect = lambda n: info1 if n == "claude_agent_sdk" else info2

        msg = _make_msg("/backends")
        response = await self.handler.handle(msg)

        assert "Claude Agent SDK" in response.content
        assert "OpenAI Agents" in response.content
        assert "(active)" in response.content

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.agents.registry.get_backend_info")
    @patch("pocketpaw.agents.registry.get_backend_class")
    @patch("pocketpaw.agents.registry.list_backends")
    async def test_shows_not_installed(self, mock_list, mock_cls, mock_info, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings

        mock_list.return_value = ["claude_agent_sdk", "missing_backend"]
        mock_info.side_effect = (
            lambda n: None
            if n == "missing_backend"
            else MagicMock(
                display_name="Claude", capabilities=MagicMock(__iter__=lambda s: iter([]))
            )
        )
        mock_cls.side_effect = lambda n: None if n == "missing_backend" else MagicMock()

        msg = _make_msg("/backends")
        response = await self.handler.handle(msg)

        assert "not installed" in response.content


# =========================================================================
# /backend command
# =========================================================================


class TestBackendCommand:
    def setup_method(self):
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.config.get_settings")
    async def test_show_current(self, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.claude_sdk_model = "claude-sonnet-4-20250514"
        mock_settings.return_value = settings

        msg = _make_msg("/backend")
        response = await self.handler.handle(msg)

        assert "claude_agent_sdk" in response.content
        assert "claude-sonnet-4-20250514" in response.content

    @patch("pocketpaw.config.get_settings")
    async def test_show_current_default_model(self, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.claude_sdk_model = ""
        mock_settings.return_value = settings

        msg = _make_msg("/backend")
        response = await self.handler.handle(msg)

        assert "default model" in response.content

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.agents.registry.get_backend_class")
    @patch("pocketpaw.agents.registry.list_backends")
    async def test_switch_valid(self, mock_list, mock_cls, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.save = MagicMock()
        mock_settings.return_value = settings
        mock_list.return_value = ["claude_agent_sdk", "openai_agents"]
        mock_cls.return_value = MagicMock()  # installed

        callback = MagicMock()
        self.handler.set_on_settings_changed(callback)

        msg = _make_msg("/backend openai_agents")
        response = await self.handler.handle(msg)

        assert "openai_agents" in response.content
        assert "Switched" in response.content
        settings.save.assert_called_once()
        callback.assert_called_once()

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.agents.registry.list_backends")
    async def test_reject_unknown(self, mock_list, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings
        mock_list.return_value = ["claude_agent_sdk", "openai_agents"]

        msg = _make_msg("/backend fake_backend")
        response = await self.handler.handle(msg)

        assert "Unknown backend" in response.content

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.agents.registry.get_backend_class")
    @patch("pocketpaw.agents.registry.list_backends")
    async def test_reject_not_installed(self, mock_list, mock_cls, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings
        mock_list.return_value = ["claude_agent_sdk", "openai_agents"]
        mock_cls.return_value = None  # not installed

        msg = _make_msg("/backend openai_agents")
        response = await self.handler.handle(msg)

        assert "not installed" in response.content

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.agents.registry.list_backends")
    async def test_already_active(self, mock_list, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_settings.return_value = settings
        mock_list.return_value = ["claude_agent_sdk"]

        msg = _make_msg("/backend claude_agent_sdk")
        response = await self.handler.handle(msg)

        assert "Already using" in response.content

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.agents.registry.get_backend_class")
    @patch("pocketpaw.agents.registry.list_backends")
    async def test_fires_callback(self, mock_list, mock_cls, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.save = MagicMock()
        mock_settings.return_value = settings
        mock_list.return_value = ["claude_agent_sdk", "openai_agents"]
        mock_cls.return_value = MagicMock()

        callback = MagicMock()
        self.handler.set_on_settings_changed(callback)

        msg = _make_msg("/backend openai_agents")
        await self.handler.handle(msg)

        callback.assert_called_once()


# =========================================================================
# /model command
# =========================================================================


class TestModelCommand:
    def setup_method(self):
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.config.get_settings")
    async def test_show_current(self, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "openai_agents"
        settings.openai_agents_model = "gpt-4o"
        mock_settings.return_value = settings

        msg = _make_msg("/model")
        response = await self.handler.handle(msg)

        assert "gpt-4o" in response.content
        assert "openai_agents" in response.content

    @patch("pocketpaw.config.get_settings")
    async def test_show_default_when_empty(self, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "openai_agents"
        settings.openai_agents_model = ""
        mock_settings.return_value = settings

        msg = _make_msg("/model")
        response = await self.handler.handle(msg)

        assert "default" in response.content

    @patch("pocketpaw.config.get_settings")
    async def test_set_new_model(self, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "openai_agents"
        settings.openai_agents_model = "gpt-4o"
        settings.save = MagicMock()
        mock_settings.return_value = settings

        callback = MagicMock()
        self.handler.set_on_settings_changed(callback)

        msg = _make_msg("/model gpt-4-turbo")
        response = await self.handler.handle(msg)

        assert "gpt-4-turbo" in response.content
        settings.save.assert_called_once()
        callback.assert_called_once()

    @patch("pocketpaw.config.get_settings")
    async def test_fires_callback(self, mock_settings):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.claude_sdk_model = ""
        settings.save = MagicMock()
        mock_settings.return_value = settings

        callback = MagicMock()
        self.handler.set_on_settings_changed(callback)

        msg = _make_msg("/model claude-opus-4-20250514")
        await self.handler.handle(msg)

        callback.assert_called_once()


# =========================================================================
# /tools command
# =========================================================================


class TestToolsCommand:
    def setup_method(self):
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    @patch("pocketpaw.config.get_settings")
    async def test_show_current(self, mock_settings):
        settings = MagicMock()
        settings.tool_profile = "coding"
        mock_settings.return_value = settings

        msg = _make_msg("/tools")
        response = await self.handler.handle(msg)

        assert "coding" in response.content
        assert "minimal" in response.content
        assert "full" in response.content

    @patch("pocketpaw.config.get_settings")
    async def test_switch_valid(self, mock_settings):
        settings = MagicMock()
        settings.tool_profile = "coding"
        settings.save = MagicMock()
        mock_settings.return_value = settings

        callback = MagicMock()
        self.handler.set_on_settings_changed(callback)

        msg = _make_msg("/tools minimal")
        response = await self.handler.handle(msg)

        assert "minimal" in response.content
        assert "switched" in response.content.lower()
        settings.save.assert_called_once()
        callback.assert_called_once()

    @patch("pocketpaw.config.get_settings")
    async def test_reject_invalid(self, mock_settings):
        settings = MagicMock()
        settings.tool_profile = "coding"
        mock_settings.return_value = settings

        msg = _make_msg("/tools nonexistent")
        response = await self.handler.handle(msg)

        assert "Unknown profile" in response.content

    @patch("pocketpaw.config.get_settings")
    async def test_already_active(self, mock_settings):
        settings = MagicMock()
        settings.tool_profile = "coding"
        mock_settings.return_value = settings

        msg = _make_msg("/tools coding")
        response = await self.handler.handle(msg)

        assert "Already using" in response.content

    @patch("pocketpaw.config.get_settings")
    async def test_fires_callback(self, mock_settings):
        settings = MagicMock()
        settings.tool_profile = "coding"
        settings.save = MagicMock()
        mock_settings.return_value = settings

        callback = MagicMock()
        self.handler.set_on_settings_changed(callback)

        msg = _make_msg("/tools full")
        await self.handler.handle(msg)

        callback.assert_called_once()


# =========================================================================
# Settings-changed callback mechanism
# =========================================================================


class TestSettingsChangedCallback:
    def setup_method(self):
        from pocketpaw.bus.commands import CommandHandler

        self.handler = CommandHandler()

    def test_callback_initially_none(self):
        assert self.handler._on_settings_changed is None

    def test_set_callback(self):
        cb = MagicMock()
        self.handler.set_on_settings_changed(cb)
        assert self.handler._on_settings_changed is cb

    def test_notify_with_no_callback(self):
        # Should not raise
        self.handler._notify_settings_changed()

    def test_notify_fires_callback(self):
        cb = MagicMock()
        self.handler.set_on_settings_changed(cb)
        self.handler._notify_settings_changed()
        cb.assert_called_once()
