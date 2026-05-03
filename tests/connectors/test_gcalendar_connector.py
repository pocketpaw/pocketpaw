# GoogleCalendarConnector — Phase 1 PR-4 contract + snapshot tests.
# Created: 2026-05-03 — pins the 4-action surface (3 mirror existing
# tools + calendar_summary for the home widget) and the 3 widget
# recipes (Today / This Week / Calendar Stats).

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.connectors.adapters.gcalendar import GoogleCalendarConnector
from pocketpaw.connectors.protocol import (
    ConnectorStatus,
    ExecutionMode,
    TrustLevel,
    WidgetRecipe,
)


def test_metadata():
    c = GoogleCalendarConnector()
    assert c.name == "gcalendar"
    assert c.display_name == "Google Calendar"
    assert c.type == "communication"
    assert c.icon == "calendar"


@pytest.mark.asyncio
async def test_action_names_match_existing_tool_classes():
    """Pins the 3 existing tool names + calendar_summary for the widget."""
    c = GoogleCalendarConnector()
    schemas = await c.actions()
    names = sorted(s.name for s in schemas)
    assert names == [
        "calendar_create",
        "calendar_list",
        "calendar_prep",
        "calendar_summary",
    ]


@pytest.mark.asyncio
async def test_actions_use_cloud_mode():
    c = GoogleCalendarConnector()
    schemas = await c.actions()
    for s in schemas:
        assert s.execution_mode is ExecutionMode.CLOUD
        assert s.requires_binary is None


@pytest.mark.asyncio
async def test_action_trust_levels():
    c = GoogleCalendarConnector()
    by_name = {s.name: s for s in await c.actions()}
    assert by_name["calendar_list"].trust_level is TrustLevel.AUTO
    assert by_name["calendar_prep"].trust_level is TrustLevel.AUTO
    assert by_name["calendar_summary"].trust_level is TrustLevel.AUTO
    assert by_name["calendar_create"].trust_level is TrustLevel.CONFIRM


@pytest.mark.asyncio
async def test_widget_recipes():
    c = GoogleCalendarConnector()
    recipes = await c.widgets()
    assert len(recipes) == 3
    titles = [r.title for r in recipes]
    assert titles == ["Today's Calendar", "This Week", "Calendar Stats"]
    assert all(isinstance(r, WidgetRecipe) for r in recipes)
    assert recipes[0].action == "calendar_list"
    assert recipes[0].params == {"days_ahead": 1, "max_results": 10}
    assert recipes[1].params == {"days_ahead": 7, "max_results": 15}
    assert recipes[2].action == "calendar_summary"


@pytest.mark.asyncio
async def test_health_ok_when_list_events_succeeds():
    c = GoogleCalendarConnector()
    with patch(
        "pocketpaw.integrations.gcalendar.CalendarClient.list_events",
        new=AsyncMock(return_value=[]),
    ):
        h = await c.health()
    assert h.ok is True
    assert h.status is ConnectorStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_error_when_list_events_raises():
    c = GoogleCalendarConnector()
    with patch(
        "pocketpaw.integrations.gcalendar.CalendarClient.list_events",
        new=AsyncMock(side_effect=RuntimeError("token expired")),
    ):
        h = await c.health()
    assert h.ok is False
    assert h.status is ConnectorStatus.ERROR


@pytest.mark.asyncio
async def test_execute_list_delegates_to_client():
    c = GoogleCalendarConnector()
    sample = [{"summary": "Standup", "start": "now", "end": "later"}]
    with patch(
        "pocketpaw.integrations.gcalendar.CalendarClient.list_events",
        new=AsyncMock(return_value=sample),
    ):
        result = await c.execute("calendar_list", {"days_ahead": 1, "max_results": 5})
    assert result.success is True
    assert result.records_affected == 1


@pytest.mark.asyncio
async def test_execute_summary_aggregates():
    """Aggregator calls list_events twice — short window for today,
    longer window for the week. Stub picks the right list by which
    call we're on (first call = today, second = week)."""
    c = GoogleCalendarConnector()
    today = [{"summary": "Standup"}, {"summary": "Lunch with X"}]
    week = today + [{"summary": "Q2 review"}, {"summary": "1:1"}]

    mock = AsyncMock(side_effect=[today, week])
    with patch(
        "pocketpaw.integrations.gcalendar.CalendarClient.list_events",
        new=mock,
    ):
        result = await c.execute("calendar_summary", {})
    assert result.success is True
    assert result.data["today"] == 2
    assert result.data["this_week"] == 4
    assert result.data["next"] == "Standup"


@pytest.mark.asyncio
async def test_execute_unknown_action():
    c = GoogleCalendarConnector()
    result = await c.execute("not_real", {})
    assert result.success is False


def test_registry_returns_calendar_connector():
    from pocketpaw.connectors.registry import _create_native_adapter

    adapter = _create_native_adapter("gcalendar")
    assert isinstance(adapter, GoogleCalendarConnector)
