# tests/cloud/calendar/test_service.py — Cloud calendar service.
#
# Updated: 2026-05-24 (feat/calendar-entity-surface, #1218) — added a
# fifth guarantee: cross-workspace calls against the same mock
# upstream payload tag each returned event with the requesting
# workspace_id. Protects the tenancy invariant when the same Composio
# response (in this contrived test) is read by two workspaces in
# sequence — the wire dicts must carry their own workspace_id, not
# leak whichever workspace happened to run first.
#
# Five guarantees:
#   1. Composio disabled  → ``[]`` (no SDK touch, no error).
#   2. Happy path         → events flow through with workspace tagging.
#   3. Workspace required → empty workspace_id raises ``ValidationError``.
#   4. Limit parameter    → caps the returned slice even when upstream
#                            returns more rows than asked for.
#   5. Cross-workspace    → two calls with different workspace_ids
#                            against one upstream payload return wire
#                            dicts each tagged with their own workspace.
#
# Tests monkeypatch the composio service boundary
# (``is_enabled`` / ``_get_client`` / ``composio_user_id``) rather than
# the upstream SDK — we're testing the calendar adapter's contract,
# not Composio's wire format. The one place we DO exercise Composio's
# wire shape is the happy-path test, which feeds a Google-shaped
# payload through the real parsing helpers.

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.calendar import service as calendar_service
from pocketpaw_ee.cloud.composio.domain import ComposioUserId

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_composio(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    execute_return: Any = None,
    execute_side_effect: Exception | None = None,
) -> MagicMock:
    """Wire the composio service surface to a controllable double.

    Returns the ``client.tools.execute`` mock so individual tests can
    assert on how many times it was called and with what arguments.
    """
    from pocketpaw_ee.cloud.composio import service as composio_service

    monkeypatch.setattr(composio_service, "is_enabled", lambda *a, **kw: enabled)

    namespaced = ComposioUserId(enterprise_id="ent_test", user_id="user_test")
    monkeypatch.setattr(
        composio_service,
        "composio_user_id",
        lambda ctx, settings=None: namespaced,
    )

    execute = MagicMock(name="tools.execute")
    if execute_side_effect is not None:
        execute.side_effect = execute_side_effect
    else:
        execute.return_value = execute_return

    client = MagicMock(name="composio_client")
    client.tools.execute = execute

    async def _fake_get_client(settings: Any = None) -> MagicMock:
        return client

    monkeypatch.setattr(composio_service, "_get_client", _fake_get_client)
    return execute


def _google_event(
    *,
    id: str,
    summary: str,
    start: str,
    end: str | None = None,
    attendees: list[str] | None = None,
    all_day: bool = False,
) -> dict[str, Any]:
    """Build a Google-Calendar-shaped event dict (the ``items[]`` row).

    Mirrors Google's wire format closely enough that the adapter's
    parser is exercised the same way it would be in production: nested
    ``start.dateTime`` (or ``start.date`` for all-day), ``attendees``
    list of ``{"email": ...}`` dicts, ``summary`` for the title.
    """
    start_block = {"date": start} if all_day else {"dateTime": start}
    end_block: dict[str, Any]
    if end is None:
        end_block = start_block
    elif all_day:
        end_block = {"date": end}
    else:
        end_block = {"dateTime": end}
    item: dict[str, Any] = {
        "id": id,
        "summary": summary,
        "start": start_block,
        "end": end_block,
    }
    if attendees:
        item["attendees"] = [{"email": e} for e in attendees]
    return item


# ---------------------------------------------------------------------------
# Tenancy + bounds guards
# ---------------------------------------------------------------------------


async def test_empty_workspace_id_raises_validation_error() -> None:
    """The first cloud entity rule is "domain enforces tenancy at
    construction". The service mirrors it: empty workspace_id is a
    refusal, not a quiet degrade — same pattern other cloud services
    use to refuse a missing workspace."""
    with pytest.raises(ValidationError, match="workspace_required"):
        await calendar_service.list_upcoming("", "user_test", limit=5)


async def test_empty_user_id_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="user_required"):
        await calendar_service.list_upcoming("ws_acme", "", limit=5)


async def test_non_positive_limit_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="invalid_limit"):
        await calendar_service.list_upcoming("ws_acme", "user_test", limit=0)


# ---------------------------------------------------------------------------
# Composio gating
# ---------------------------------------------------------------------------


async def test_returns_empty_when_composio_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most common "no data" state: Composio not configured at all.
    The handler will fall back to its hint; the service stays silent."""
    execute = _patch_composio(monkeypatch, enabled=False)
    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert out == []
    execute.assert_not_called()


async def test_returns_empty_when_upstream_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network or auth errors from Composio must degrade gracefully —
    the handler always sees a list, never a raised exception."""
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_side_effect=RuntimeError("composio: no connected account"),
    )
    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert out == []


async def test_returns_empty_when_upstream_returns_no_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composio call succeeded but the user's calendar is empty — same
    end state as disabled. The handler folds these into one fallback."""
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={"data": {"items": []}, "successful": True},
    )
    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert out == []


# ---------------------------------------------------------------------------
# Happy path + tenancy tagging
# ---------------------------------------------------------------------------


async def test_happy_path_renders_events_with_workspace_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five Google-shaped events flow through: titles, ISO timestamps,
    attendees, and the workspace tag all land on each wire dict.
    Implicitly verifies the domain object refuses to be built without
    workspace_id (it would raise at construction otherwise)."""
    items = [
        _google_event(
            id="ev1",
            summary="Sync with Sarah",
            start="2026-05-25T10:30:00-07:00",
            end="2026-05-25T11:00:00-07:00",
            attendees=["sarah@example.com", "me@example.com"],
        ),
        _google_event(
            id="ev2",
            summary="Q2 planning",
            start="2026-05-26T14:00:00-07:00",
            end="2026-05-26T15:30:00-07:00",
        ),
        _google_event(
            id="ev3",
            summary="All-hands",
            start="2026-05-27",
            end="2026-05-28",
            all_day=True,
        ),
    ]
    execute = _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={"data": {"items": items}, "successful": True},
    )

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=10)

    assert len(out) == 3
    assert {ev["id"] for ev in out} == {"ev1", "ev2", "ev3"}
    # Every event carries the requesting workspace_id — tenancy tag
    # applied at domain construction time, mirrored onto the wire dict.
    assert all(ev["workspace_id"] == "ws_acme" for ev in out)
    # Source is the upstream system slug, not the toolkit name.
    assert all(ev["source"] == "google" for ev in out)
    # Title flows from Google's ``summary`` field.
    titles = [ev["title"] for ev in out]
    assert "Sync with Sarah" in titles
    # Attendees collapse to plain emails.
    sarah_event = next(ev for ev in out if ev["id"] == "ev1")
    assert sarah_event["attendees"] == ["sarah@example.com", "me@example.com"]
    # All-day event keeps the date-only ISO string in ``start``.
    allhands = next(ev for ev in out if ev["id"] == "ev3")
    assert allhands["start"] == "2026-05-27"
    # The Composio call was made exactly once with the right action.
    execute.assert_called_once()
    args, kwargs = execute.call_args
    assert args[0] == "GOOGLECALENDAR_LIST_EVENTS"
    assert kwargs["user_id"] == "ent_test:user_test"


async def test_skips_items_missing_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream payload missing ``id`` is dropped silently — better
    than fabricating a placeholder that would confuse the agent."""
    items = [
        {"id": "ok", "summary": "Valid", "start": {"dateTime": "2026-05-25T10:00:00Z"}},
        {"summary": "Missing id", "start": {"dateTime": "2026-05-25T11:00:00Z"}},
        {"id": "", "summary": "Empty id"},
    ]
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={"data": {"items": items}, "successful": True},
    )
    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=10)
    assert [ev["id"] for ev in out] == ["ok"]


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------


async def test_limit_caps_results_even_if_upstream_returns_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composio honors ``maxResults`` as a hint — if the upstream
    returns more rows than asked for, the service trims to ``limit``
    before mapping to wire dicts."""
    items = [
        _google_event(
            id=f"ev{i}",
            summary=f"Event {i}",
            start=f"2026-05-2{i}T10:00:00-07:00",
            end=f"2026-05-2{i}T11:00:00-07:00",
        )
        for i in range(1, 8)
    ]
    execute = _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={"data": {"items": items}, "successful": True},
    )

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=3)

    assert len(out) == 3
    assert [ev["id"] for ev in out] == ["ev1", "ev2", "ev3"]
    # Forwarded the limit as ``maxResults`` so the upstream can
    # short-circuit when possible.
    _, kwargs = execute.call_args
    assert kwargs["arguments"] == {"maxResults": 3}


# ---------------------------------------------------------------------------
# Cross-workspace tenancy tagging
# ---------------------------------------------------------------------------


async def test_cross_workspace_calls_tag_each_event_with_caller_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive calls — one from ``ws_a``, one from ``ws_b`` —
    against the same mocked Composio response. Every returned event
    must carry the requesting workspace_id, never the other's.

    The shared payload is contrived — in production each workspace
    would have its own Composio connection. The point of this test is
    the tag-at-construction invariant: the domain object refuses to
    be built without a workspace_id and stamps the caller's tag onto
    every event before it crosses the service boundary."""
    items = [
        _google_event(
            id="shared_ev_1",
            summary="Shared Event 1",
            start="2026-05-25T10:00:00-07:00",
            end="2026-05-25T11:00:00-07:00",
        ),
        _google_event(
            id="shared_ev_2",
            summary="Shared Event 2",
            start="2026-05-26T14:00:00-07:00",
            end="2026-05-26T15:00:00-07:00",
        ),
    ]
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={"data": {"items": items}, "successful": True},
    )

    out_a = await calendar_service.list_upcoming("ws_a", "user_test", limit=10)
    out_b = await calendar_service.list_upcoming("ws_b", "user_test", limit=10)

    # Both calls see the same upstream rows.
    assert {ev["id"] for ev in out_a} == {"shared_ev_1", "shared_ev_2"}
    assert {ev["id"] for ev in out_b} == {"shared_ev_1", "shared_ev_2"}
    # But every wire dict carries the requesting workspace_id —
    # never the other workspace's tag.
    assert all(ev["workspace_id"] == "ws_a" for ev in out_a)
    assert all(ev["workspace_id"] == "ws_b" for ev in out_b)
    # Sanity: no event from out_a ended up tagged with ws_b (would
    # signal shared-state leak between calls).
    assert all(ev["workspace_id"] != "ws_b" for ev in out_a)
    assert all(ev["workspace_id"] != "ws_a" for ev in out_b)
