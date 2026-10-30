"""Tests that ``connectors.service`` emits realtime events via the bus.

Each state-mutating service function must fire the appropriate Event
class through ``emit()``. Tests run against a real Beanie in-memory
database (``mongo_db`` fixture) and assert on ``recording_bus.events``.

These assertions live alongside the existing legacy ``event_bus`` topic
publishes — the realtime ``emit()`` is additive (fans out to FE
subscribers), the legacy bus carries internal subscribers (e.g. the
local runtime's ``connector.exec.requested`` listener) and stays as-is.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    ConnectorConfigUpdated,
    ConnectorDisabled,
    ConnectorEnabled,
    ConnectorSyncRecorded,
)
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import (
    EnableConnectorRequest,
    UpdateConnectorConfigRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


# ``stripe`` is a YAML-only connector shipped in repo /connectors;
# it's available in every registry instance so tests don't need to
# seed the catalog.
_CONNECTOR = "stripe"


async def test_enable_emits_connector_enabled_on_insert(recording_bus) -> None:
    await connectors_service.enable_connector(
        "w1", _CONNECTOR, EnableConnectorRequest(scope="workspace")
    )

    enabled = [e for e in recording_bus.events if isinstance(e, ConnectorEnabled)]
    assert len(enabled) == 1
    ev = enabled[0]
    assert ev.type == "connector.enabled"
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["name"] == _CONNECTOR
    assert ev.data["scope"] == "workspace"


async def test_enable_emits_connector_enabled_on_re_enable(recording_bus) -> None:
    await connectors_service.enable_connector(
        "w1", _CONNECTOR, EnableConnectorRequest(scope="workspace")
    )
    await connectors_service.disable_connector("w1", _CONNECTOR)
    recording_bus.events.clear()

    await connectors_service.enable_connector(
        "w1", _CONNECTOR, EnableConnectorRequest(scope="workspace")
    )

    enabled = [e for e in recording_bus.events if isinstance(e, ConnectorEnabled)]
    assert len(enabled) == 1


async def test_disable_emits_connector_disabled(recording_bus) -> None:
    await connectors_service.enable_connector(
        "w1", _CONNECTOR, EnableConnectorRequest(scope="workspace")
    )
    recording_bus.events.clear()

    await connectors_service.disable_connector("w1", _CONNECTOR)

    disabled = [e for e in recording_bus.events if isinstance(e, ConnectorDisabled)]
    assert len(disabled) == 1
    ev = disabled[0]
    assert ev.type == "connector.disabled"
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["name"] == _CONNECTOR


async def test_disable_no_emit_when_row_absent(recording_bus) -> None:
    """Disabling a never-enabled connector is a no-op write; no emit."""
    await connectors_service.disable_connector("w1", _CONNECTOR)

    disabled = [e for e in recording_bus.events if isinstance(e, ConnectorDisabled)]
    assert disabled == []


async def test_update_config_emits_connector_config_updated(recording_bus) -> None:
    await connectors_service.enable_connector(
        "w1", _CONNECTOR, EnableConnectorRequest(scope="workspace")
    )
    recording_bus.events.clear()

    await connectors_service.update_config(
        "w1", _CONNECTOR, UpdateConnectorConfigRequest(config={"api_key": "sk_test"})
    )

    updated = [e for e in recording_bus.events if isinstance(e, ConnectorConfigUpdated)]
    assert len(updated) == 1
    ev = updated[0]
    assert ev.type == "connector.config_updated"
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["name"] == _CONNECTOR


async def test_record_sync_emits_connector_sync_recorded(recording_bus) -> None:
    await connectors_service.enable_connector(
        "w1", _CONNECTOR, EnableConnectorRequest(scope="workspace")
    )
    recording_bus.events.clear()

    await connectors_service.record_sync("w1", _CONNECTOR, status="ok")

    synced = [e for e in recording_bus.events if isinstance(e, ConnectorSyncRecorded)]
    assert len(synced) == 1
    ev = synced[0]
    assert ev.type == "connector.sync_recorded"
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["name"] == _CONNECTOR
    assert ev.data["status"] == "ok"


async def test_record_sync_error_carries_status(recording_bus) -> None:
    await connectors_service.enable_connector(
        "w1", _CONNECTOR, EnableConnectorRequest(scope="workspace")
    )
    recording_bus.events.clear()

    await connectors_service.record_sync("w1", _CONNECTOR, status="error", error="boom")

    synced = [e for e in recording_bus.events if isinstance(e, ConnectorSyncRecorded)]
    assert len(synced) == 1
    assert synced[0].data["status"] == "error"
