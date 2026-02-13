# Tests for integrations/gcalendar.py and tools/builtin/calendar.py
# Created: 2026-02-07

from unittest.mock import patch

from pocketclaw.tools.builtin.calendar import CalendarCreateTool, CalendarListTool, CalendarPrepTool

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    def test_calendar_list_tool(self):
        tool = CalendarListTool()
        assert tool.name == "calendar_list"
        assert tool.trust_level == "high"

    def test_calendar_create_tool(self):
        tool = CalendarCreateTool()
        assert tool.name == "calendar_create"
        assert "summary" in tool.parameters["properties"]
        assert "start" in tool.parameters["properties"]
        assert "end" in tool.parameters["properties"]

    def test_calendar_prep_tool(self):
        tool = CalendarPrepTool()
        assert tool.name == "calendar_prep"
        assert tool.trust_level == "high"


# ---------------------------------------------------------------------------
# Tool execution — error path (no OAuth token)
# ---------------------------------------------------------------------------


async def test_calendar_list_no_auth():
    tool = CalendarListTool()
    with patch(
        "pocketclaw.integrations.gcalendar.CalendarClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute()
        assert "Error" in result
        assert "authenticated" in result.lower()


async def test_calendar_create_no_auth():
    tool = CalendarCreateTool()
    with patch(
        "pocketclaw.integrations.gcalendar.CalendarClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(
            summary="Test",
            start="2026-02-08T10:00:00Z",
            end="2026-02-08T11:00:00Z",
        )
        assert "Error" in result


async def test_calendar_prep_no_auth():
    tool = CalendarPrepTool()
    with patch(
        "pocketclaw.integrations.gcalendar.CalendarClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute()
        assert "Error" in result
