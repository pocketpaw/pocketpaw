# Tests for the persisted-notification → push dispatch listener (pocketpaw#1393).
# Created: 2026-06-09 (feat/push-wire-events) — covered the four hand-wired
# product-event handlers (agent.stream_end / instinct.approval.created /
# meeting.started / message.sent).
#
# Updated: 2026-08-11 (fix/notif-push-convergence) — rewritten around
# ``on_notification_new``, the single handler push now converges on. What the
# suite pins:
#   * EVERY kind ``notifications/service.create`` writes reaches dispatch —
#     the coverage hole the convergence closes (invites, leads, tasks,
#     concierge kinds never reached the OS before);
#   * lock-screen privacy — the persisted body of a content-bearing kind
#     (message / mention / reaction / concierge / lead) NEVER crosses into the
#     push payload, and ``message`` keeps its "N new messages" count body;
#   * no double-fire — a mention writes two rows for one user action, and the
#     coalescer collapses them to one immediate push;
#   * the retired subscriptions are actually gone from ``register``;
#   * the coalesce key falls back to the KIND for room-less notifications, so
#     a burst of invites doesn't fan out one Web Push per row;
#   * ``on_agent_complete`` survives — agent replies persist no notification
#     row, so retiring it would have silently dropped their push.
# ``dispatch.notify`` is patched to record calls — no WS, no Web Push, no DB.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    AgentStreamEnd,
    NotificationNew,
)
from pocketpaw_ee.cloud.push import listeners


@pytest.fixture
def notify_calls(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_notify(workspace_id, user_id, payload):
        calls.append((workspace_id, user_id, payload))

    monkeypatch.setattr(listeners.dispatch, "notify", fake_notify)
    return calls


@pytest.fixture(autouse=True)
def _no_coalesce(monkeypatch):
    """Run the handler with the coalescer OFF (pass-through) so these tests
    assert the recipient/payload logic directly. The leading-edge throttle has
    its own dedicated suite in test_coalesce.py; the two tests below that DO
    exercise it re-enable it locally. ``reset()`` clears any window state so a
    lingering task can't leak into the next test."""
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", "0")
    listeners.coalesce.reset()
    yield
    listeners.coalesce.reset()


def _dto(**overrides) -> dict:
    """The wire DTO ``notification.new`` carries (notifications/dto.py)."""
    data: dict = {
        "id": "n1",
        "user_id": "alice",
        "workspace_id": "w1",
        "kind": "invite",
        "title": "You were invited",
        "body": "",
        "source_id": "inv1",
        "source_type": "invite",
        "source_pocket_id": None,
        "source_room_id": None,
        "source_agent_id": None,
        "read": False,
        "created_at": "2026-08-11T00:00:00Z",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Coverage — every persisted kind reaches dispatch
# ---------------------------------------------------------------------------

# One representative row per kind ``notifications/service.create`` writes today.
# Kinds that used to have NO push at all are the point of the convergence.
ALL_KINDS: list[tuple[str, str | None]] = [
    ("message", "message"),
    ("mention", "message"),
    ("reaction", "message"),
    ("group_invite", "message"),
    ("invite", "invite"),
    ("task_assigned", None),
    ("lead_captured", "lead"),
    ("meeting_scheduled", "meeting_scheduled"),
    ("meeting_started", "meeting_started"),
    ("meeting_cancelled", "meeting_cancelled"),
    ("meeting_reminder", "meeting_reminder"),
    ("meeting_recording_ready", "meeting_recording_ready"),
    ("meeting_transcript_ready", "meeting_transcript_ready"),
    ("instinct_approval", "instinct_approval"),
    ("paw_bar_conversation_new", "paw_bar_conversation"),
    ("paw_bar_needs_human", "paw_bar_conversation"),
    ("paw_bar_visitor_reply", "paw_bar_conversation"),
]


@pytest.fixture
def _unread(monkeypatch):
    async def fake_count(user_id, group_id):
        return 3

    monkeypatch.setattr(listeners, "_unread_count", fake_count)


async def test_every_kind_reaches_dispatch(notify_calls, _unread) -> None:
    for index, (kind, source_type) in enumerate(ALL_KINDS):
        await listeners.on_notification_new(
            NotificationNew(
                data=_dto(
                    id=f"n{index}",
                    kind=kind,
                    title=f"{kind} happened",
                    source_type=source_type,
                    source_id=f"s{index}" if source_type else None,
                    source_room_id="g1" if kind in ("message", "mention", "reaction") else None,
                )
            )
        )

    # N notifications of distinct kinds → N dispatches, one per row.
    assert len(notify_calls) == len(ALL_KINDS)
    assert {uid for _, uid, _ in notify_calls} == {"alice"}
    assert all(ws == "w1" for ws, _, _ in notify_calls)
    assert all(p["title"].endswith("happened") for _, _, p in notify_calls)


async def test_noop_without_recipient_or_workspace(notify_calls) -> None:
    await listeners.on_notification_new(NotificationNew(data=_dto(user_id=None)))
    await listeners.on_notification_new(NotificationNew(data=_dto(workspace_id=None)))
    await listeners.on_notification_new(NotificationNew(data={}))
    assert notify_calls == []


# ---------------------------------------------------------------------------
# Lock-screen privacy — the persisted body must not cross into the push
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["message", "mention", "reaction"]
    + ["paw_bar_conversation_new", "paw_bar_needs_human", "paw_bar_visitor_reply"],
)
async def test_content_bearing_body_never_leaks(notify_calls, _unread, kind) -> None:
    # These kinds persist user-authored text in ``body`` (message content, a
    # visitor's words). A push body lands on a lock screen. ``lead_captured``
    # is absent on purpose — its persisted body is already content-free.
    secret = "wire me $10k to account 12345"
    await listeners.on_notification_new(
        NotificationNew(data=_dto(kind=kind, title="Something", body=secret, source_room_id="g1"))
    )

    assert len(notify_calls) == 1
    _, _, payload = notify_calls[0]
    assert secret not in payload["body"]
    assert secret not in payload["title"]
    assert payload["body"]  # still says something useful


async def test_message_body_is_the_unread_count(notify_calls, _unread) -> None:
    await listeners.on_notification_new(
        NotificationNew(
            data=_dto(kind="message", title="Alice", body="hello there", source_room_id="g1")
        )
    )

    _, _, payload = notify_calls[0]
    assert payload["body"] == "3 new messages"
    # Collapse per conversation so a later message updates the same toast.
    assert payload["tag"] == "g1"


async def test_message_body_singular_without_room(notify_calls, monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise AssertionError("no room → no unread lookup")

    monkeypatch.setattr(listeners, "_unread_count", boom)

    await listeners.on_notification_new(
        NotificationNew(data=_dto(kind="message", body="secret", source_room_id=None))
    )

    _, _, payload = notify_calls[0]
    assert payload["body"] == "1 new message"


async def test_lead_captured_body_passes_through(notify_calls) -> None:
    # leads/bridges writes "Someone submitted the {form_type} form on
    # {site_label}." — no visitor data — so overriding it would be a pure UX
    # downgrade. Guards against someone "hardening" it back into the map.
    body = "Someone submitted the contact form on Acme Dental."
    await listeners.on_notification_new(
        NotificationNew(data=_dto(kind="lead_captured", body=body, source_type="lead"))
    )

    _, _, payload = notify_calls[0]
    assert payload["body"] == body


async def test_generic_kind_body_passes_through(notify_calls) -> None:
    # ``task_assigned`` persists a generic body ("Assigned by <id>") — nothing
    # user-authored — so it is NOT rewritten.
    await listeners.on_notification_new(
        NotificationNew(data=_dto(kind="task_assigned", body="Assigned by u9", source_type=None))
    )

    _, _, payload = notify_calls[0]
    assert payload["body"] == "Assigned by u9"


# ---------------------------------------------------------------------------
# Deep links — mirrors paw-enterprise src/lib/core/notifications/target.ts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "extra", "expected"),
    [
        ("message", {"source_room_id": "g1"}, "/chat/g1"),
        ("mention", {"source_room_id": "g1"}, "/chat/g1"),
        ("message", {"source_pocket_id": "p1"}, "/pockets/p1/chat"),
        ("invite", {}, "/invite/s1"),
        ("pocket_shared", {}, "/pockets/s1"),
        ("meeting", {}, "/meetings?id=s1"),
        ("meeting_started", {"source_room_id": "g1"}, "/chat/g1?join=meeting-s1"),
        ("paw_bar_conversation", {"source_agent_id": "a1"}, "/agents/a1?tab=conversations"),
        ("paw_bar_conversation", {}, "/agents"),
        # A lead's room_id is its SITE — without the arm the default builds
        # /chat/<site_id>, a room that cannot exist.
        ("lead", {"source_room_id": "site1"}, "/sites/site1?view=leads"),
        ("meeting_reminder", {"source_room_id": "g1"}, "/chat/g1"),
    ],
)
def test_target_url(source_type, extra, expected) -> None:
    assert listeners._target_url(_dto(source_type=source_type, source_id="s1", **extra)) == expected


def test_target_url_omitted_when_ambiguous() -> None:
    # No source at all → no link.
    assert listeners._target_url(_dto(source_type=None, source_id=None)) is None
    # The approvals tray is a global overlay, not a route — a ``/chat/<id>``
    # default arm would land on a room that cannot exist.
    assert listeners._target_url(_dto(source_type="instinct_approval", source_id="a1")) is None


async def test_payload_omits_url_when_there_is_none(notify_calls) -> None:
    await listeners.on_notification_new(
        NotificationNew(data=_dto(kind="instinct_approval", source_type="instinct_approval"))
    )

    _, _, payload = notify_calls[0]
    assert "url" not in payload
    assert payload["tag"] == "instinct_approval"


# ---------------------------------------------------------------------------
# No double-fire — the coalescer collapses a mention's two rows
# ---------------------------------------------------------------------------


async def test_mention_flow_sends_twice_under_one_replacing_tag(
    notify_calls, _unread, monkeypatch
) -> None:
    """One @mention writes TWO notification rows (kind ``message`` for every
    member, kind ``mention`` for the target), so the same user action emits two
    ``notification.new`` events for the target.

    The honest behaviour, asserted end to end rather than only at the leading
    edge: the first send fires IMMEDIATELY, the second is suppressed and
    flushed when the cooldown window elapses — TWO sends, not one. What the
    shared coalesce key buys is that they are two rather than N, and both carry
    the same ``tag``, so the OS replaces the first toast instead of stacking a
    second. Mutation that breaks it: key the coalescer on the KIND first rather
    than the room, which splits the two rows onto different keys — both then
    fire on their own leading edge and the "once immediately" assertion goes to
    two."""
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", "0.05")

    await listeners.on_notification_new(
        NotificationNew(data=_dto(id="n1", kind="message", source_room_id="g1"))
    )
    await listeners.on_notification_new(
        NotificationNew(data=_dto(id="n2", kind="mention", source_room_id="g1"))
    )
    # The leading emit is detached (a background task) — let it run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(notify_calls) == 1, "leading edge fires immediately, once"

    # Advance past the cooldown: the suppressed mention row flushes.
    await asyncio.sleep(0.2)

    assert len(notify_calls) == 2, "the second row flushes after the window"
    assert {p["tag"] for _, _, p in notify_calls} == {"g1"}, "one replacing toast"
    assert {uid for _, uid, _ in notify_calls} == {"alice"}
    await listeners.coalesce.aclose()


async def test_coalesce_key_falls_back_to_kind_without_a_room(notify_calls, monkeypatch) -> None:
    """Room-less kinds (invites, tasks, leads) used to have no coalesce scope at
    all. Keyed on the kind, a burst for one recipient collapses instead of
    firing one Web Push per row."""
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", "5")

    await listeners.on_notification_new(NotificationNew(data=_dto(id="n1", kind="invite")))
    await listeners.on_notification_new(NotificationNew(data=_dto(id="n2", kind="invite")))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(notify_calls) == 1
    await listeners.coalesce.aclose()


async def test_different_kinds_without_a_room_do_not_collapse(notify_calls, monkeypatch) -> None:
    """The fallback scopes per KIND, so an invite and a task assignment for the
    same user are still two separate pushes."""
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", "5")

    await listeners.on_notification_new(NotificationNew(data=_dto(id="n1", kind="invite")))
    await listeners.on_notification_new(
        NotificationNew(data=_dto(id="n2", kind="task_assigned", source_type=None))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(notify_calls) == 2
    await listeners.coalesce.aclose()


# ---------------------------------------------------------------------------
# agent-complete (agent.stream_end) — KEPT: agent replies persist no row
# ---------------------------------------------------------------------------


async def test_agent_complete_notifies_group_members(notify_calls, monkeypatch) -> None:
    async def fake_ws_id(group_id):
        return "w-agent"

    async def fake_members(group_id):
        return ["alice", "bob"]

    monkeypatch.setattr(listeners, "_group_workspace_id", fake_ws_id)
    monkeypatch.setattr(listeners, "_group_member_ids", fake_members)

    event = AgentStreamEnd(data={"group_id": "g1", "agent_name": "Scout"})
    await listeners.on_agent_complete(event)

    assert {uid for _, uid, _ in notify_calls} == {"alice", "bob"}
    assert all(ws == "w-agent" for ws, _, _ in notify_calls)
    assert all("Scout" in p["title"] for _, _, p in notify_calls)


async def test_agent_complete_noop_without_group(notify_calls) -> None:
    await listeners.on_agent_complete(AgentStreamEnd(data={}))
    assert notify_calls == []


async def test_agent_complete_noop_when_workspace_unresolved(notify_calls, monkeypatch) -> None:
    async def fake_ws_id(group_id):
        return None

    monkeypatch.setattr(listeners, "_group_workspace_id", fake_ws_id)
    await listeners.on_agent_complete(AgentStreamEnd(data={"group_id": "g1"}))
    assert notify_calls == []


# ---------------------------------------------------------------------------
# Registration — the retired subscriptions must be gone
# ---------------------------------------------------------------------------


RETIRED_EVENTS = ("instinct.approval.created", "meeting.started", "message.sent")


def test_register_subscribes_only_the_converged_events(monkeypatch) -> None:
    subscribed: list[tuple[str, object]] = []

    class _FakeBus:
        def subscribe(self, event_type, handler):
            subscribed.append((event_type, handler))

    from pocketpaw_ee.cloud._core.realtime import bus as bus_module

    monkeypatch.setattr(bus_module, "get_bus", lambda: _FakeBus())
    listeners.register_push_event_listeners()

    types = [t for t, _ in subscribed]
    assert types == ["notification.new", "agent.stream_end"]
    # Each retired event's flow now persists a notification row (or, for
    # instinct approvals, does as of this change), so a subscription here
    # would double-fire against ``notification.new``.
    for retired in RETIRED_EVENTS:
        assert retired not in types
    assert dict(subscribed)["notification.new"] is listeners.on_notification_new


def test_retired_handlers_no_longer_exist() -> None:
    for name in ("on_guardian_block", "on_meeting_started", "on_new_message"):
        assert not hasattr(listeners, name), f"{name} should have been retired"
