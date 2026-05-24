# tests/cloud/surface/test_calendar_handler.py — Calendar surface handler.
#
# Updated: 2026-05-24 (feat/calendar-entity-surface, #1218) — split the
# single empty test into two so the three rendering states (events
# present / Composio on + empty / Composio off) are each pinned. Now
# four guarantees:
#   1. Happy path        — three mocked events render into a snapshot
#                           block with one line per event; surface tag
#                           always present.
#   2. Empty + enabled   — when ``list_upcoming`` returns ``[]`` AND
#                           Composio is enabled, the handler renders
#                           the ``(no upcoming events)`` snapshot
#                           rather than the hint — the agent needs to
#                           tell "genuinely empty" apart from "no
#                           integration".
#   3. Empty + disabled  — when Composio is off, the hint stays —
#                           agent learns the action name so it can
#                           guide the user to connect.
#   4. Failure path      — when the service raises, the handler still
#                           returns a usable preamble (surface tag +
#                           hint), never propagates the exception to
#                           the chat router.
#
# The handler is mocked at its single hot dependency
# (``calendar.service.list_upcoming``). For branches 2 and 3 we also
# stub ``composio_service.is_enabled`` so the test doesn't depend on
# environment Settings.

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


async def test_handler_renders_empty_snapshot_when_composio_enabled_but_no_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service returns ``[]`` AND Composio is on — the calendar is
    genuinely empty. Render the ``(no upcoming events)`` snapshot so
    the agent doesn't waste a turn telling the user to connect a
    calendar that's already connected."""

    async def _fake(workspace_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    from pocketpaw_ee.cloud.calendar import service as calendar_service
    from pocketpaw_ee.cloud.composio import service as composio_service

    monkeypatch.setattr(calendar_service, "list_upcoming", _fake)
    monkeypatch.setattr(composio_service, "is_enabled", lambda *a, **kw: True)

    preamble = await calendar_handler.build_preamble("ws_acme", "user_test", SurfaceMeta())

    assert '<surface kind="calendar"' in preamble
    assert "(no upcoming events)" in preamble
    # The hint is suppressed — Composio is wired up, no need to nudge.
    assert "GOOGLECALENDAR_LIST_EVENTS" not in preamble


async def test_handler_renders_hint_when_composio_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service returns ``[]`` because Composio is off — the agent
    needs the action-name hint so it can guide the user toward
    connecting an integration. Distinct from the genuinely-empty
    case above."""

    async def _fake(workspace_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    from pocketpaw_ee.cloud.calendar import service as calendar_service
    from pocketpaw_ee.cloud.composio import service as composio_service

    monkeypatch.setattr(calendar_service, "list_upcoming", _fake)
    monkeypatch.setattr(composio_service, "is_enabled", lambda *a, **kw: False)

    preamble = await calendar_handler.build_preamble("ws_acme", "user_test", SurfaceMeta())

    assert '<surface kind="calendar"' in preamble
    assert "GOOGLECALENDAR_LIST_EVENTS" in preamble
    assert "Composio" in preamble
    # The empty-snapshot string belongs to the enabled branch — confirm
    # we didn't accidentally fall through both messages.
    assert "(no upcoming events)" not in preamble


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
