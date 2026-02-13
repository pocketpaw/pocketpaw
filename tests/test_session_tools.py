"""Tests for session management tools."""

from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.tools.builtin.sessions import (
    ClearSessionTool,
    DeleteSessionTool,
    ListSessionsTool,
    NewSessionTool,
    RenameSessionTool,
    SwitchSessionTool,
)

# =========================================================================
# NewSessionTool
# =========================================================================


class TestNewSessionTool:
    def setup_method(self):
        self.tool = NewSessionTool()

    def test_name(self):
        assert self.tool.name == "new_session"

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_creates_alias(self, mock_get_mm):
        mm = MagicMock()
        mm.set_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123")

        assert "new conversation" in result.lower()
        mm.set_session_alias.assert_called_once()
        call_args = mm.set_session_alias.call_args
        assert call_args[0][0] == "discord:123"
        assert call_args[0][1].startswith("discord:123:")

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_error_handling(self, mock_get_mm):
        mock_get_mm.side_effect = RuntimeError("boom")

        result = await self.tool.execute(session_key="discord:123")

        assert "Error" in result


# =========================================================================
# ListSessionsTool
# =========================================================================


class TestListSessionsTool:
    def setup_method(self):
        self.tool = ListSessionsTool()

    def test_name(self):
        assert self.tool.name == "list_sessions"

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_empty(self, mock_get_mm):
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123")

        assert "no sessions" in result.lower()

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_with_sessions(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Debug API",
                "last_activity": "",
                "message_count": 5,
                "preview": "",
                "is_active": True,
            },
            {
                "session_key": "discord:123:def",
                "title": "Write tests",
                "last_activity": "",
                "message_count": 3,
                "preview": "",
                "is_active": False,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123")

        assert "Debug API" in result
        assert "Write tests" in result
        assert "(active)" in result
        assert "5 msgs" in result


# =========================================================================
# SwitchSessionTool
# =========================================================================


class TestSwitchSessionTool:
    def setup_method(self):
        self.tool = SwitchSessionTool()

    def test_name(self):
        assert self.tool.name == "switch_session"

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_switch_by_number(self, mock_get_mm):
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

        result = await self.tool.execute(session_key="discord:123", target="1")

        assert "First Chat" in result
        mm.set_session_alias.assert_called_once_with("discord:123", "discord:123:abc")

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_switch_invalid_number(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Chat",
                "last_activity": "",
                "message_count": 1,
                "preview": "",
                "is_active": True,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123", target="5")

        assert "invalid" in result.lower()

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_switch_by_text_single(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Debug API",
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

        result = await self.tool.execute(session_key="discord:123", target="debug")

        assert "Debug API" in result
        mm.set_session_alias.assert_called_once()

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_switch_by_text_no_match(self, mock_get_mm):
        sessions = [
            {
                "session_key": "discord:123:abc",
                "title": "Debug API",
                "last_activity": "",
                "message_count": 5,
                "preview": "",
                "is_active": True,
            },
        ]
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=sessions)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123", target="foobar")

        assert "no sessions matching" in result.lower()

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_switch_by_text_multiple(self, mock_get_mm):
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

        result = await self.tool.execute(session_key="discord:123", target="write")

        assert "multiple" in result.lower()
        assert "Write tests A" in result
        assert "Write tests B" in result

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_switch_no_sessions(self, mock_get_mm):
        mm = MagicMock()
        mm.list_sessions_for_chat = AsyncMock(return_value=[])
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123", target="1")

        assert "no sessions" in result.lower()


# =========================================================================
# ClearSessionTool
# =========================================================================


class TestClearSessionTool:
    def setup_method(self):
        self.tool = ClearSessionTool()

    def test_name(self):
        assert self.tool.name == "clear_session"

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_clear_with_messages(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:123")
        mm.clear_session = AsyncMock(return_value=7)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123")

        assert "7 messages" in result

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_clear_empty(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:123")
        mm.clear_session = AsyncMock(return_value=0)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123")

        assert "already empty" in result.lower()


# =========================================================================
# RenameSessionTool
# =========================================================================


class TestRenameSessionTool:
    def setup_method(self):
        self.tool = RenameSessionTool()

    def test_name(self):
        assert self.tool.name == "rename_session"

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_rename_success(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:123")
        mm.update_session_title = AsyncMock(return_value=True)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123", title="My Project")

        assert "My Project" in result

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_rename_not_found(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:123")
        mm.update_session_title = AsyncMock(return_value=False)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123", title="New Title")

        assert "not found" in result.lower()


# =========================================================================
# DeleteSessionTool
# =========================================================================


class TestDeleteSessionTool:
    def setup_method(self):
        self.tool = DeleteSessionTool()

    def test_name(self):
        assert self.tool.name == "delete_session"

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_delete_success(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:123:abc")
        mm.delete_session = AsyncMock(return_value=True)
        mm._store = MagicMock()
        mm._store.remove_session_alias = AsyncMock(return_value=True)
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123")

        assert "deleted" in result.lower()
        mm.delete_session.assert_called_once_with("discord:123:abc")

    @patch("pocketclaw.tools.builtin.sessions.get_memory_manager")
    async def test_delete_nothing(self, mock_get_mm):
        mm = MagicMock()
        mm.resolve_session_key = AsyncMock(return_value="discord:123")
        mm.delete_session = AsyncMock(return_value=False)
        mm._store = MagicMock()
        mm._store.remove_session_alias = AsyncMock()
        mock_get_mm.return_value = mm

        result = await self.tool.execute(session_key="discord:123")

        assert "no session" in result.lower()


# =========================================================================
# Policy: group:sessions in minimal profile
# =========================================================================


class TestSessionToolPolicy:
    def test_group_sessions_exists(self):
        from pocketclaw.tools.policy import TOOL_GROUPS

        assert "group:sessions" in TOOL_GROUPS
        names = TOOL_GROUPS["group:sessions"]
        assert "new_session" in names
        assert "list_sessions" in names
        assert "switch_session" in names
        assert "clear_session" in names
        assert "rename_session" in names
        assert "delete_session" in names

    def test_minimal_profile_includes_sessions(self):
        from pocketclaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile="minimal")
        assert policy.is_tool_allowed("new_session")
        assert policy.is_tool_allowed("list_sessions")
        assert policy.is_tool_allowed("switch_session")
        assert policy.is_tool_allowed("clear_session")
        assert policy.is_tool_allowed("rename_session")
        assert policy.is_tool_allowed("delete_session")
