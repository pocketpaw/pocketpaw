# GoogleDocsConnector — Phase 1 PR-5 contract tests.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.connectors.adapters.gdocs import GoogleDocsConnector
from pocketpaw.connectors.protocol import (
    ConnectorStatus,
    ExecutionMode,
    TrustLevel,
)


def test_metadata():
    c = GoogleDocsConnector()
    assert c.name == "gdocs"
    assert c.display_name == "Google Docs"
    assert c.type == "knowledge"


@pytest.mark.asyncio
async def test_action_names():
    c = GoogleDocsConnector()
    schemas = await c.actions()
    assert sorted(s.name for s in schemas) == ["docs_create", "docs_read", "docs_search"]


@pytest.mark.asyncio
async def test_actions_use_cloud_mode():
    c = GoogleDocsConnector()
    for s in await c.actions():
        assert s.execution_mode is ExecutionMode.CLOUD


@pytest.mark.asyncio
async def test_trust_levels():
    c = GoogleDocsConnector()
    by_name = {s.name: s for s in await c.actions()}
    assert by_name["docs_read"].trust_level is TrustLevel.AUTO
    assert by_name["docs_search"].trust_level is TrustLevel.AUTO
    assert by_name["docs_create"].trust_level is TrustLevel.CONFIRM


@pytest.mark.asyncio
async def test_widget_recipes():
    c = GoogleDocsConnector()
    recipes = await c.widgets()
    assert len(recipes) == 1
    assert recipes[0].title == "Recent Docs"
    assert recipes[0].action == "docs_search"


@pytest.mark.asyncio
async def test_execute_search_caps_at_50():
    c = GoogleDocsConnector()
    with patch(
        "pocketpaw.integrations.gdocs.DocsClient.search_docs",
        new=AsyncMock(return_value=[]),
    ) as mock:
        await c.execute("docs_search", {"query": "x", "max_results": 999})
    mock.assert_called_once_with("x", max_results=50)


@pytest.mark.asyncio
async def test_health_ok():
    c = GoogleDocsConnector()
    with patch(
        "pocketpaw.integrations.gdocs.DocsClient.search_docs",
        new=AsyncMock(return_value=[]),
    ):
        h = await c.health()
    assert h.ok is True
    assert h.status is ConnectorStatus.CONNECTED


def test_registry_returns_docs_connector():
    from pocketpaw.connectors.registry import _create_native_adapter

    adapter = _create_native_adapter("gdocs")
    assert isinstance(adapter, GoogleDocsConnector)
