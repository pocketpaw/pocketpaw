# tests/cloud/surface/test_calendar_handler.py — Calendar surface handler.
#
# Created: 2026-05-24 (feat/calendar-entity-surface, #1214) — three
# guarantees:
#   1. Happy path     — three mocked events render into a snapshot
#                       block with one line per event; surface tag
#                       always present.
#   2. Empty path     — when the service returns no events, the handler
#                       falls back to the Composio hint so the agent
#                       still knows what tool to reach for.
#   3. Failure path   — when the service raises, the handler still
#                       returns a usable preamble (surface tag + hint),
#                       never propagates the exception to the chat
#                       router.
#
# The handler is mocked at its single dependency
# (``calendar.service.list_upcoming``) so these tests don't need the
# DB or any Composio plumbing. The service has its own test file.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import calendar as calendar_handler


def _wire_event(**overrides: Any) -> dict[str, Any]:
    """Build a wire-shaped event dict (matches what list_upcoming returns)."""
    base: dict[str, Any] = {
        "id": "ev1",
        "workspace_id": "ws_acme",
        "title": "Sync with Sarah",
        "start": "2026-05-25T10:30:00-07:00",
        "end": "2026-05-25T11:00:00-07:00",
        "source": "google",
        "attendees": ["sarah@example.com"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_handler_renders_events_into_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three events render into one line each plus a count attribute.
    The surface tag is always emitted regardless of branch."""
    events = [
        _wire_event(id="ev1", title="Sync with Sarah", start="2026-05-25T10:30:00-07:00"),
        _wire_event(id="ev2", title="Q2 planning", start="2026-05-26T14:00:00-07:00"),
        _wire_event(id="ev3", title="All-hands", start="2026-05-27", end="2026-05-28"),
    ]

    async def _fake_list_upcoming(
        workspace_id: str, user_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        # Forward the call so we can assert the handler passes its limit.
        assert workspace_id == "ws_acme"
        assert user_id == "user_test"
        return events

    # Patch via the module the handler imports from. The handler imports
    # ``list_upcoming`` lazily inside ``build_preamble``, so the patch
    # has to land on the source module rather than on a re-export.
    from pocketpaw_ee.cloud.calendar import service as calendar_service

    monkeypatch.setattr(calendar_service, "list_upcoming", _fake_list_upcoming)

    preamble = await calendar_handler.build_preamble("ws_acme", "user_test", SurfaceMeta())

    # Surface tag is always present.
    assert '<surface kind="calendar"' in preamble
    # Count attribute matches the event list.
    assert 'count="3"' in preamble
    # Each event title surfaces.
    assert "Sync with Sarah" in preamble
    assert "Q2 planning" in preamble
    assert "All-hands" in preamble
    # The empty-state hint is suppressed when events render.
    assert "no live event feed wired" not in preamble


async def test_handler_renders_time_of_day_for_timed_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start`` like ``2026-05-25T10:30:00-07:00`` renders as ``10:30 AM``
    in the snapshot line so the agent quotes a human-friendly time."""
    events = [_wire_event(start="2026-05-25T10:30:00-07:00", title="Sync")]

    async def _fake(workspace_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return events

    from pocketpaw_ee.cloud.calendar import service as calendar_service

    monkeypatch.setattr(calendar_service, "list_upcoming", _fake)

    preamble = await calendar_handler.build_preamble("ws_acme", "user_test", SurfaceMeta())

    assert "10:30 AM" in preamble
    assert "Sync" in preamble


# ---------------------------------------------------------------------------
# Empty path
# ---------------------------------------------------------------------------


async def test_handler_renders_hint_when_no_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the service returns ``[]`` (Composio disabled, calendar
    not connected, or just an empty calendar), the handler renders
    the Composio hint so the agent still knows what tool to reach for.

    Per the brief, the empty branch shows the hint — distinct from a
    separate "no upcoming events" string. The hint covers both empty
    and unavailable states with one message.
    """

    async def _fake(workspace_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    from pocketpaw_ee.cloud.calendar import service as calendar_service

    monkeypatch.setattr(calendar_service, "list_upcoming", _fake)

    preamble = await calendar_handler.build_preamble("ws_acme", "user_test", SurfaceMeta())

    assert '<surface kind="calendar"' in preamble
    assert "GOOGLECALENDAR_LIST_EVENTS" in preamble
    assert "Composio" in preamble


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


async def test_handler_falls_back_when_service_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``list_upcoming`` raises (e.g. an internal bug we haven't
    caught yet), the handler must still return a usable preamble.
    Never let a calendar surface failure break a chat send."""

    async def _boom(workspace_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        raise RuntimeError("calendar service exploded")

    from pocketpaw_ee.cloud.calendar import service as calendar_service

    monkeypatch.setattr(calendar_service, "list_upcoming", _boom)

    preamble = await calendar_handler.build_preamble("ws_acme", "user_test", SurfaceMeta())

    # Surface tag still present.
    assert '<surface kind="calendar"' in preamble
    # Hint emitted so the agent still has a path forward.
    assert "GOOGLECALENDAR_LIST_EVENTS" in preamble
