# tests/ee/calendar/conftest.py — Pytest fixtures for the calendar module.
# Created: 2026-05-19 (feat/calendar-module).
#
# We deliberately avoid spinning up a real Mongo for these tests. The
# service layer is exercised by stubbing _EventDoc at the boundary so we
# never hit Beanie. That makes the tests fast and CI-friendly, at the
# cost of giving up a few coupling guarantees that an integration suite
# would catch — those land in tests/cloud/ in a follow-up PR.

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from ee.calendar._context import RequestContext
from ee.calendar.domain import Event


@pytest.fixture
def ctx() -> RequestContext:
    """A standard tenant + actor for tests."""
    return RequestContext(workspace_id="ws-test", user_id="user-test")


@pytest.fixture
def other_ctx() -> RequestContext:
    """A second tenant for cross-workspace isolation tests."""
    return RequestContext(workspace_id="ws-other", user_id="user-other")


@pytest.fixture
def event_factory():
    """Build an Event without hitting the DB."""

    def _make(
        id: str = "evt-1",
        workspace_id: str = "ws-test",
        calendar_id: str = "cal-1",
        title: str = "Standup",
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        **overrides: Any,
    ) -> Event:
        starts_at = starts_at or datetime(2026, 5, 19, 9, 0)
        ends_at = ends_at or datetime(2026, 5, 19, 9, 30)
        return Event(
            id=id,
            workspace_id=workspace_id,
            calendar_id=calendar_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=overrides.pop("timezone", "UTC"),
            description=overrides.pop("description", ""),
            location=overrides.pop("location", None),
            attendees=overrides.pop("attendees", []),
            recurrence=overrides.pop("recurrence", None),
            fabric_object_id=overrides.pop("fabric_object_id", None),
            source_connector=overrides.pop("source_connector", None),
            source_external_id=overrides.pop("source_external_id", None),
            created_at=overrides.pop("created_at", datetime(2026, 5, 1)),
            updated_at=overrides.pop("updated_at", datetime(2026, 5, 1)),
        )

    return _make


@pytest.fixture
async def bus_spy() -> AsyncIterator[list[tuple[str, dict]]]:
    """Capture every event_bus.emit call.

    Returns a list of (topic, payload) tuples in emission order. Cleanly
    restores the bus on teardown.
    """
    from ee.cloud.shared.events import event_bus

    captured: list[tuple[str, dict]] = []
    original_emit = event_bus.emit

    async def _spy(topic: str, data: dict) -> None:
        captured.append((topic, data))
        # Don't actually fan out — handlers are tested separately.

    event_bus.emit = _spy  # type: ignore[method-assign]
    try:
        yield captured
    finally:
        event_bus.emit = original_emit  # type: ignore[method-assign]
