# Golden deep-link contract for the push notification resolver.
# Created: 2026-08-20 (test/push-deeplink-contract) — pins the literal
# ``_target_url`` output for every source_type arm against a golden table so
# a route rename on either side of the stack trips a test instead of shipping
# a dead link.
#
# Twin of paw-enterprise src/lib/core/notifications/__tests__/
# deep-link-contract.test.ts pinning target.ts::targetUrl — if you change a
# row here, change it there. Known deliberate divergences: FE
# paw_bar_conversation appends &conversation=<ref>; FE enumerates several
# kinds that here fall to the /chat default.
#
# Updated 2026-08-20 (test/deeplink-contract-followups): review fixes — added
# the pocket-AND-room precedence row and the ``alert`` → None row, absorbed
# test_listeners.py's older duplicate table into this one, and wired the
# mutation plan (tests/mutations/deeplink_contract.json; run via
# ``uv run python scripts/mutate.py --plan tests/mutations/deeplink_contract.json``).
# Mutations proven to break this table: flipping the pocket-vs-room precedence,
# deleting the ``lead`` arm, deleting the ``alert`` arm.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.push.listeners import _target_url

# Each row is (label, wire-DTO fields, expected url). The urls are LITERAL on
# purpose — no helpers, no f-strings — so the table reads as the contract.
GOLDEN = [
    (
        "instinct_approval has no route (approvals tray is an overlay)",
        {"source_type": "instinct_approval", "source_id": "appr-1"},
        None,
    ),
    (
        "paw_bar_conversation with agent lands on its conversations tab",
        {
            "source_type": "paw_bar_conversation",
            "source_id": "widget-1:cust-9",
            "source_agent_id": "agent-7",
        },
        "/agents/agent-7?tab=conversations",
    ),
    (
        "paw_bar_conversation without agent falls back to the agents list",
        {"source_type": "paw_bar_conversation", "source_id": "widget-1:cust-9"},
        "/agents",
    ),
    (
        "message inside a pocket goes to the pocket chat",
        {
            "source_type": "message",
            "source_id": "msg-1",
            "source_pocket_id": "pocket-3",
        },
        "/pockets/pocket-3/chat",
    ),
    (
        "message without a pocket goes to its room",
        {
            "source_type": "message",
            "source_id": "msg-1",
            "source_room_id": "room-5",
        },
        "/chat/room-5",
    ),
    (
        "message without pocket or room falls back to the source id",
        {"source_type": "message", "source_id": "msg-1"},
        "/chat/msg-1",
    ),
    (
        # Pins the precedence, not just each branch alone: with BOTH set the
        # pocket must win. Flipping the ``if pocket_id`` order passes every
        # single-field row silently — this row is the one that trips.
        "message with both pocket and room prefers the pocket chat",
        {
            "source_type": "message",
            "source_id": "msg-1",
            "source_pocket_id": "pocket-3",
            "source_room_id": "room-5",
        },
        "/pockets/pocket-3/chat",
    ),
    (
        "alert has no route (no alerts page; a dead /chat link would be worse)",
        {"source_type": "alert", "source_id": "budget_exhausted"},
        None,
    ),
    (
        "mention inside a pocket goes to the pocket chat",
        {
            "source_type": "mention",
            "source_id": "msg-2",
            "source_pocket_id": "pocket-3",
        },
        "/pockets/pocket-3/chat",
    ),
    (
        "mention without a pocket goes to its room",
        {
            "source_type": "mention",
            "source_id": "msg-2",
            "source_room_id": "room-5",
        },
        "/chat/room-5",
    ),
    (
        "invite goes to the invite acceptance page",
        {"source_type": "invite", "source_id": "inv-42"},
        "/invite/inv-42",
    ),
    (
        "pocket_shared opens the shared pocket",
        {"source_type": "pocket_shared", "source_id": "pocket-8"},
        "/pockets/pocket-8",
    ),
    (
        "meeting opens the meetings view on that meeting",
        {"source_type": "meeting", "source_id": "meet-1"},
        "/meetings?id=meet-1",
    ),
    (
        "meeting_started deep-links the room with the join param",
        {
            "source_type": "meeting_started",
            "source_id": "meet-1",
            "source_room_id": "room-5",
        },
        "/chat/room-5?join=meeting-meet-1",
    ),
    (
        "lead lands on its site's leads view (room_id is the site)",
        {
            "source_type": "lead",
            "source_id": "lead-77",
            "source_room_id": "site-4",
        },
        "/sites/site-4?view=leads",
    ),
    (
        "unknown source types default to the chat room",
        {
            "source_type": "some_future_kind",
            "source_id": "x-1",
            "source_room_id": "room-9",
        },
        "/chat/room-9",
    ),
    (
        "missing source_type resolves to no link",
        {"source_id": "msg-1"},
        None,
    ),
    (
        "missing source_id resolves to no link",
        {"source_type": "message"},
        None,
    ),
]


@pytest.mark.parametrize(
    "data, expected",
    [(row[1], row[2]) for row in GOLDEN],
    ids=[row[0] for row in GOLDEN],
)
def test_target_url_golden(data: dict, expected: str | None) -> None:
    """Breaks under tests/mutations/deeplink_contract.json — flipped
    pocket-vs-room precedence, a deleted ``lead``/``alert`` arm, or a
    paw_bar arm that drops its agent binding (all 4 observed caught)."""
    assert _target_url(data) == expected
