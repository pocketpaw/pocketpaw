# Connector bus listener — Phase 1 PR-8 contract tests.
# Created: 2026-05-03 — pins the in-process round-trip:
#   1. Cloud emits connector.exec.requested.
#   2. Listener picks it up, runs the adapter on this host, publishes
#      connector.exec.completed.
#   3. Failure modes: missing binary → connector.binary_missing,
#      unknown connector → connector.not_found, timeout, bare emit
#      shapes.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ee.cloud.shared.events import event_bus
from pocketpaw.connectors.protocol import ActionResult
from pocketpaw.runtime import connector_bus


@pytest.fixture(autouse=True)
def _clean_subscribers():
    """Reset the listener registration between tests."""
    connector_bus.reset_for_tests()
    # Snapshot + clear subscribers so previous tests don't bleed into ours.
    saved = dict(event_bus._handlers)  # noqa: SLF001
    event_bus._handlers.clear()  # noqa: SLF001
    yield
    event_bus._handlers.clear()  # noqa: SLF001
    event_bus._handlers.update(saved)  # noqa: SLF001
    connector_bus.reset_for_tests()


def _capture_completed() -> list[dict]:
    """Subscribe a capturing handler to connector.exec.completed."""
    captured: list[dict] = []

    async def handler(payload):
        captured.append(payload)

    event_bus.subscribe(connector_bus.EXEC_COMPLETED, handler)
    return captured


# ---------------------------------------------------------------------------
# register_listener idempotence
# ---------------------------------------------------------------------------


def test_register_is_idempotent():
    connector_bus.register_listener()
    connector_bus.register_listener()
    handlers = event_bus._handlers[connector_bus.EXEC_REQUESTED]  # noqa: SLF001
    assert len(handlers) == 1


# ---------------------------------------------------------------------------
# Happy path — Gmail (cloud-mode adapter; tests the round-trip plumbing
# even though Gmail isn't local-mode in production)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_runs_adapter_and_publishes_completed():
    captured = _capture_completed()
    connector_bus.register_listener()

    with patch(
        "pocketpaw.connectors.adapters.gmail.GmailConnector.execute",
        new=AsyncMock(return_value=ActionResult(success=True, data={"x": 1}, records_affected=1)),
    ):
        await event_bus.emit(
            connector_bus.EXEC_REQUESTED,
            {
                "request_id": "req-1",
                "connector": "gmail",
                "action": "gmail_search",
                "params": {"query": "x"},
                "scope": "workspace",
            },
        )

    assert len(captured) == 1
    completed = captured[0]
    assert completed["request_id"] == "req-1"
    assert completed["success"] is True
    assert completed["data"] == {"x": 1}
    assert completed["records_affected"] == 1
    assert completed["error"] is None


# ---------------------------------------------------------------------------
# Missing binary path — fails fast with connector.binary_missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_binary_fails_fast(monkeypatch):
    captured = _capture_completed()
    connector_bus.register_listener()

    monkeypatch.setattr("pocketpaw.runtime.connector_bus.shutil.which", lambda _: None)

    await event_bus.emit(
        connector_bus.EXEC_REQUESTED,
        {
            "request_id": "req-2",
            "connector": "firebase",
            "action": "list_projects",
            "params": {},
            "requires_binary": "firebase",
        },
    )

    assert len(captured) == 1
    assert captured[0]["success"] is False
    assert "connector.binary_missing" in (captured[0]["error"] or "")


# ---------------------------------------------------------------------------
# Unknown connector — connector.not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_connector_returns_not_found():
    captured = _capture_completed()
    connector_bus.register_listener()

    await event_bus.emit(
        connector_bus.EXEC_REQUESTED,
        {
            "request_id": "req-3",
            "connector": "this-is-not-a-real-connector",
            "action": "x",
            "params": {},
        },
    )

    assert len(captured) == 1
    assert captured[0]["success"] is False
    assert "connector.not_found" in (captured[0]["error"] or "")


# ---------------------------------------------------------------------------
# Malformed payload — emits an error completed event, doesn't crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_payload():
    captured = _capture_completed()
    connector_bus.register_listener()

    await event_bus.emit(connector_bus.EXEC_REQUESTED, {"request_id": "req-4"})

    assert len(captured) == 1
    assert captured[0]["success"] is False
    assert "malformed" in (captured[0]["error"] or "")
