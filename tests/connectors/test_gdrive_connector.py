# GoogleDriveConnector — Phase 1 PR-6 contract tests.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.connectors.adapters.gdrive import GoogleDriveConnector
from pocketpaw.connectors.protocol import (
    ConnectorStatus,
    ExecutionMode,
    TrustLevel,
)


def test_metadata():
    c = GoogleDriveConnector()
    assert c.name == "drive"
    assert c.display_name == "Google Drive"
    assert c.type == "knowledge"


@pytest.mark.asyncio
async def test_action_names():
    c = GoogleDriveConnector()
    schemas = await c.actions()
    assert sorted(s.name for s in schemas) == [
        "drive_download",
        "drive_list",
        "drive_share",
        "drive_upload",
    ]


@pytest.mark.asyncio
async def test_actions_use_cloud_mode():
    c = GoogleDriveConnector()
    for s in await c.actions():
        assert s.execution_mode is ExecutionMode.CLOUD


@pytest.mark.asyncio
async def test_trust_levels():
    c = GoogleDriveConnector()
    by_name = {s.name: s for s in await c.actions()}
    assert by_name["drive_list"].trust_level is TrustLevel.AUTO
    assert by_name["drive_download"].trust_level is TrustLevel.AUTO
    assert by_name["drive_upload"].trust_level is TrustLevel.CONFIRM
    assert by_name["drive_share"].trust_level is TrustLevel.CONFIRM


@pytest.mark.asyncio
async def test_widget_recipes():
    c = GoogleDriveConnector()
    recipes = await c.widgets()
    assert [r.title for r in recipes] == ["Recent Drive Files", "Shared with Me"]


@pytest.mark.asyncio
async def test_execute_list_caps_at_100():
    c = GoogleDriveConnector()
    with patch(
        "pocketpaw.clients.gdrive.DriveClient.list_files",
        new=AsyncMock(return_value=[]),
    ) as mock:
        await c.execute("drive_list", {"max_results": 999})
    mock.assert_called_once_with(query=None, max_results=100)


@pytest.mark.asyncio
async def test_health_ok():
    c = GoogleDriveConnector()
    with patch(
        "pocketpaw.clients.gdrive.DriveClient.list_files",
        new=AsyncMock(return_value=[]),
    ):
        h = await c.health()
    assert h.ok is True
    assert h.status is ConnectorStatus.CONNECTED


def test_registry_returns_drive_connector():
    """Native adapter wins over the YAML-only DirectRESTAdapter."""
    from pocketpaw.connectors.registry import _create_native_adapter

    adapter = _create_native_adapter("drive")
    assert isinstance(adapter, GoogleDriveConnector)
