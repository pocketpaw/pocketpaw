"""Tests for ee.cloud.notifications.dto — wire DTO + mapping."""

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.notifications.domain import Notification, NotificationSource
from pocketpaw_ee.cloud.notifications.dto import NotificationOut, notification_to_dto


def _domain(**overrides) -> Notification:
    base = dict(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="You were mentioned",
        body="hello",
        source=None,
        read=False,
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        expires_at=None,
    )
    base.update(overrides)
    return Notification(**base)


def test_dto_contains_wire_keys() -> None:
    """Pins the FULL wire shape — every key a client may read.

    This assertion had drifted: it still listed only the nine original keys while
    the DTO had since grown ``source_type`` / ``source_pocket_id`` /
    ``source_room_id`` / ``actor_id``, so it was failing on dev before this
    change (the notifications tests are in the ``tests/cloud`` blind spot, so
    nothing surfaced it). Brought back in line and extended with
    ``source_agent_id``. Keep it exhaustive rather than a subset: this DTO is
    also what the ``notification.new`` bus frame dumps, so a key added here
    changes the socket contract too, and that should be a deliberate edit.
    """
    out = NotificationOut(
        id="n1",
        user_id="u1",
        workspace_id="w1",
        kind="mention",
        title="t",
        body="b",
        source_id=None,
        read=False,
        created_at="2026-04-27T12:00:00+00:00",
    )
    dump = out.model_dump()
    assert set(dump.keys()) == {
        "id",
        "user_id",
        "workspace_id",
        "kind",
        "title",
        "body",
        "source_id",
        "source_type",
        "source_pocket_id",
        "source_room_id",
        "source_agent_id",
        "actor_id",
        "read",
        "created_at",
    }


def test_notification_to_dto_no_source() -> None:
    out = notification_to_dto(_domain())
    assert out.id == "n1"
    assert out.user_id == "u1"
    assert out.workspace_id == "w1"
    assert out.kind == "mention"
    assert out.title == "You were mentioned"
    assert out.body == "hello"
    assert out.source_id is None
    assert out.read is False
    assert out.created_at == "2026-04-27T12:00:00+00:00"


def test_notification_to_dto_with_source() -> None:
    src = NotificationSource(type="message", id="m42", pocket_id=None)
    out = notification_to_dto(_domain(source=src))
    assert out.source_id == "m42"


def test_notification_to_dto_serializes_naive_created_at_as_utc() -> None:
    """Beanie reads return naive datetimes; iso_utc anchors them to +00:00."""
    naive = datetime(2026, 4, 27, 12, 0, 0)
    out = notification_to_dto(_domain(created_at=naive))
    assert out.created_at == "2026-04-27T12:00:00+00:00"


def test_the_source_agent_id_reaches_the_wire():
    """A concierge notification's agent id must survive the domain → DTO hop.

    Regression (found live 2026-07-31): the concierge inbox lives on an AGENT,
    not in a chat room, so a client with no agent id has nothing to build a link
    from and falls back to the chat surface — the click landed on
    /chat/<widget_id>:<customer_ref>, a room that cannot exist, and the default
    agent rendered empty. The DTO is the only path to both the REST read and the
    ``notification.new`` bus frame (which dumps this same DTO), so dropping the
    field here breaks the link on every surface at once.
    """
    dto = notification_to_dto(
        _domain(
            kind="paw_bar_conversation_new",
            source=NotificationSource(
                type="paw_bar_conversation",
                id="pp-w1:cust-abc",
                agent_id="agt_ridgeline",
            ),
        )
    )
    assert dto.source_agent_id == "agt_ridgeline"
    assert dto.source_id == "pp-w1:cust-abc"


def test_a_source_without_an_agent_serializes_as_none():
    """An ordinary (non-concierge) source has no agent — that is not an error."""
    dto = notification_to_dto(
        _domain(source=NotificationSource(type="message", id="m1", room_id="r1"))
    )
    assert dto.source_agent_id is None
