# GmailConnector — Phase 1 PR-3 contract + snapshot tests.
# Created: 2026-05-03 — pins:
#   1. The 9 GmailConnector actions (8 mirror tools/builtin/gmail.py
#      classes + gmail_summary added for the Email Stats widget).
#   2. The 3 widget recipes (Inbox / Important Emails / Email Stats).
#   3. The action surface matches the existing hand-written tool names
#      so a future PR can replace them via connector_tools_for(c)
#      without breaking the agent's tool registry.
#
# Connect / execute paths are covered with stubbed GmailClient calls
# so the suite doesn't need real Google OAuth credentials.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.connectors.adapters.gmail import GmailConnector
from pocketpaw.connectors.protocol import (
    ConnectorStatus,
    ExecutionMode,
    TrustLevel,
    WidgetRecipe,
)

# ---------------------------------------------------------------------------
# Static metadata
# ---------------------------------------------------------------------------


def test_metadata():
    c = GmailConnector()
    assert c.name == "gmail"
    assert c.display_name == "Gmail"
    assert c.type == "communication"
    assert c.icon == "mail"


# ---------------------------------------------------------------------------
# Action surface — snapshot pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_names_match_existing_tool_classes():
    """Snapshot test — these 8 names must equal the 8 hand-written
    tool classes in src/pocketpaw/tools/builtin/gmail.py.

    A future PR (3.5+) replaces those classes with generated tools
    via connector_tools_for(c). If this test breaks, either:
      (a) the connector dropped/added an action — update the tool
          list in tools/builtin/__init__.py to match
      (b) the legacy tool gained/lost a method — update this assert
    """
    c = GmailConnector()
    schemas = await c.actions()
    names = sorted(s.name for s in schemas)
    assert names == [
        "gmail_batch_modify",
        "gmail_create_label",
        "gmail_list_labels",
        "gmail_modify",
        "gmail_read",
        "gmail_search",
        "gmail_send",
        "gmail_summary",  # added for the Email Stats widget recipe
        "gmail_trash",
    ]


@pytest.mark.asyncio
async def test_actions_use_cloud_mode():
    """Gmail is a REST API — every action runs in cloud, no CLI involved."""
    c = GmailConnector()
    schemas = await c.actions()
    for s in schemas:
        assert s.execution_mode is ExecutionMode.CLOUD
        assert s.requires_binary is None


@pytest.mark.asyncio
async def test_action_trust_levels():
    """Read actions auto-execute; mutations require confirm."""
    c = GmailConnector()
    by_name = {s.name: s for s in await c.actions()}
    # Reads
    assert by_name["gmail_search"].trust_level is TrustLevel.AUTO
    assert by_name["gmail_read"].trust_level is TrustLevel.AUTO
    assert by_name["gmail_list_labels"].trust_level is TrustLevel.AUTO
    assert by_name["gmail_summary"].trust_level is TrustLevel.AUTO
    # Mutations
    assert by_name["gmail_send"].trust_level is TrustLevel.CONFIRM
    assert by_name["gmail_create_label"].trust_level is TrustLevel.CONFIRM
    assert by_name["gmail_modify"].trust_level is TrustLevel.CONFIRM
    assert by_name["gmail_trash"].trust_level is TrustLevel.CONFIRM
    assert by_name["gmail_batch_modify"].trust_level is TrustLevel.CONFIRM


# ---------------------------------------------------------------------------
# Widget recipes — exactly three, matching the home seed data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_widget_recipes():
    c = GmailConnector()
    recipes = await c.widgets()
    assert len(recipes) == 3
    titles = [r.title for r in recipes]
    assert titles == ["Inbox", "Important Emails", "Email Stats"]

    inbox = recipes[0]
    assert isinstance(inbox, WidgetRecipe)
    assert inbox.display_type == "feed"
    assert inbox.action == "gmail_search"
    assert inbox.params == {"query": "is:unread", "max_results": 10}
    assert inbox.default_size == "col-1 row-2"

    important = recipes[1]
    assert important.action == "gmail_search"
    assert important.params == {"query": "is:important newer_than:1d", "max_results": 10}

    stats = recipes[2]
    assert stats.display_type == "stats"
    assert stats.action == "gmail_summary"
    assert stats.default_size == "col-1 row-1"


# ---------------------------------------------------------------------------
# health() — pings list_labels, reflects success/error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_ok_when_list_labels_succeeds():
    c = GmailConnector()
    with patch(
        "pocketpaw.integrations.gmail.GmailClient.list_labels",
        new=AsyncMock(return_value=[{"id": "INBOX"}]),
    ):
        h = await c.health()
    assert h.ok is True
    assert h.status is ConnectorStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_error_when_list_labels_raises():
    c = GmailConnector()
    with patch(
        "pocketpaw.integrations.gmail.GmailClient.list_labels",
        new=AsyncMock(side_effect=RuntimeError("missing token")),
    ):
        h = await c.health()
    assert h.ok is False
    assert h.status is ConnectorStatus.ERROR
    assert "missing token" in h.message


# ---------------------------------------------------------------------------
# execute() — delegates to GmailClient with the right params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_search_delegates_to_client():
    c = GmailConnector()
    with patch(
        "pocketpaw.integrations.gmail.GmailClient.search",
        new=AsyncMock(return_value=[{"id": "m1"}, {"id": "m2"}]),
    ) as mock:
        result = await c.execute("gmail_search", {"query": "is:unread", "max_results": 5})
    assert result.success is True
    assert result.records_affected == 2
    mock.assert_called_once_with("is:unread", max_results=5)


@pytest.mark.asyncio
async def test_execute_search_caps_max_results_at_20():
    c = GmailConnector()
    with patch(
        "pocketpaw.integrations.gmail.GmailClient.search",
        new=AsyncMock(return_value=[]),
    ) as mock:
        await c.execute("gmail_search", {"query": "x", "max_results": 999})
    mock.assert_called_once_with("x", max_results=20)


@pytest.mark.asyncio
async def test_execute_summary_aggregates_two_searches():
    c = GmailConnector()
    with patch(
        "pocketpaw.integrations.gmail.GmailClient.search",
        new=AsyncMock(return_value=[{"id": "m1"}]),
    ):
        result = await c.execute("gmail_summary", {})
    assert result.success is True
    assert result.data["unread"] == 1
    assert result.data["today"] == 1
    assert "avg_reply_time" in result.data


@pytest.mark.asyncio
async def test_execute_unknown_action():
    c = GmailConnector()
    result = await c.execute("not_real", {})
    assert result.success is False
    assert "Unknown action" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_runtime_error_returns_failure():
    c = GmailConnector()
    with patch(
        "pocketpaw.integrations.gmail.GmailClient.search",
        new=AsyncMock(side_effect=RuntimeError("auth expired")),
    ):
        result = await c.execute("gmail_search", {"query": "x"})
    assert result.success is False
    assert "auth expired" in (result.error or "")


# ---------------------------------------------------------------------------
# Registry wiring — _create_native_adapter("gmail") returns GmailConnector
# ---------------------------------------------------------------------------


def test_registry_returns_gmail_connector():
    """ConnectorRegistry's native-adapter dispatch picks up Gmail."""
    from pocketpaw.connectors.registry import _create_native_adapter

    adapter = _create_native_adapter("gmail")
    assert isinstance(adapter, GmailConnector)


def test_registry_returns_none_for_unknown_native():
    from pocketpaw.connectors.registry import _create_native_adapter

    assert _create_native_adapter("not-a-real-connector") is None
