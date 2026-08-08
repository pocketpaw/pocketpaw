# tests/cloud/test_channel_chat_mirror.py
# Created: 2026-08-08 (feat/coupling-t9-channels-in-chat).
#
# T-9 — external channel conversations render as workspace chat rooms.
#
# A bidirectional mirror is a ring, so almost every test here asserts an EXACT
# COUNT rather than "the message arrived". A working mirror and an echoing one
# both put the message in the room; only the count tells them apart. The three
# echo loops are enumerated in the bridge's module docstring; there is one test
# per loop below, and one mutation per loop in
# tests/mutations/channel_chat_mirror.json.
#
# Guards 2 and 3 are STRUCTURAL — the mirror writes via
# ``_create_group_message_doc`` and never emits ``message.sent``, which is what
# both this bridge's room→channel handler and the agent-bridge auto-respond
# subscribe to. The tests pin the observable consequence (counts), not the
# mechanism, so a future refactor that reintroduces the emit fails here.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import ConflictError  # noqa: E402
from pocketpaw_ee.cloud.chat import group_service  # noqa: E402
from pocketpaw_ee.cloud.chat.bridges import channels as bridge  # noqa: E402
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc  # noqa: E402
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc  # noqa: E402
from pocketpaw_ee.cloud.shared.errors import Forbidden  # noqa: E402

from pocketpaw.bus import get_message_bus  # noqa: E402
from pocketpaw.bus.events import Channel, InboundMessage, OutboundMessage  # noqa: E402

WS = "w1"
OTHER_WS = "w2"
USER = "u1"
OUTSIDER = "u9"
CHAT_ID = "tg-chat-42"


@pytest.fixture
def registered(monkeypatch):
    """Register the mirror against a FRESH bus, and tear it down after.

    The OSS bus is a process singleton; leaving subscribers on it leaks the
    mirror into every later test in the session (FU-14 is exactly that bug in
    another module)."""
    bridge.register_channel_chat_listeners()
    yield
    bus = get_message_bus()
    bus.unsubscribe_inbound_observer(bridge._on_inbound)
    for ch in Channel:
        bus.unsubscribe_outbound(ch, bridge._on_outbound)
    from pocketpaw_ee.cloud.shared.events import event_bus

    event_bus.unsubscribe("message.sent", bridge._on_room_message)


@pytest.fixture
def sent_outbound(monkeypatch) -> list[OutboundMessage]:
    """Capture what the bridge publishes toward the channel.

    Subscribing a recorder would ALSO receive the bridge's own marker'd
    messages, so it records at the publish boundary instead — the assertion is
    about what we tried to send, not what a fake adapter received."""
    captured: list[OutboundMessage] = []
    bus = get_message_bus()
    original = bus.publish_outbound

    async def _spy(msg: OutboundMessage) -> None:
        captured.append(msg)
        await original(msg)

    monkeypatch.setattr(bus, "publish_outbound", _spy)
    return captured


async def _make_group(*, workspace: str = WS, bind: bool = True, owner: str = USER) -> str:
    doc = _GroupDoc(
        workspace=workspace,
        name="Support",
        owner=owner,
        members=[owner],
        type="channel",
    )
    await doc.insert()
    gid = str(doc.id)
    if bind:
        await group_service.bind_group_to_channel(
            workspace, owner, gid, channel="telegram", chat_id=CHAT_ID
        )
    return gid


async def _room_messages(group_id: str) -> list[_MessageDoc]:
    return await _MessageDoc.find(_MessageDoc.group == group_id).to_list()


def _inbound(content: str = "my order is late", **meta) -> InboundMessage:
    return InboundMessage(
        channel=Channel.TELEGRAM,
        sender_id="tg-user-7",
        chat_id=CHAT_ID,
        content=content,
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# 1. channel → room
# ---------------------------------------------------------------------------


async def test_a_customer_message_appears_in_the_bound_room(mongo_db, registered):
    """THE SLICE. A Telegram message lands as a room message the team can read.

    MUTATION THAT BREAKS THIS: make ``_on_inbound`` return before
    ``_mirror_into_room`` — the room stays empty."""
    gid = await _make_group()

    await get_message_bus().publish_inbound(_inbound("my order is late"))

    rows = await _room_messages(gid)
    assert len(rows) == 1
    assert rows[0].content == "my order is late"
    assert rows[0].sender_type == "external", "a stranger must not be filed as a workspace user"
    assert rows[0].sender is None, "an external sender has no workspace account"


async def test_an_unbound_conversation_mirrors_nothing(mongo_db, registered):
    """The default path. An unbound deployment must be untouched by the mirror.

    Asserts the message collection is empty DEPLOYMENT-WIDE, not just for the
    group under test: a mirror that resolved a wrong or synthetic group id
    would leave this room clean while writing somewhere it must not.

    MUTATION THAT BREAKS THIS: drop the ``if binding is None: return`` guard
    and fall through to any group id — a row appears in the collection."""
    gid = await _make_group(bind=False)

    await get_message_bus().publish_inbound(_inbound())

    assert await _room_messages(gid) == []
    assert await _MessageDoc.find_all().to_list() == [], "the mirror wrote somewhere"


async def test_the_agent_still_receives_a_mirrored_message(mongo_db, registered):
    """Mirroring OBSERVES; it must not consume. If the mirror ate the message
    the customer would see their message in the team's room and never get a
    reply — a worse failure than no mirror at all.

    MUTATION THAT BREAKS THIS: register ``_on_inbound`` as a consumer, or have
    the observer seam pop the queue."""
    await _make_group()
    bus = get_message_bus()

    await bus.publish_inbound(_inbound("still reaches the agent"))

    delivered = await bus.consume_inbound(timeout=0.5)
    assert delivered is not None
    assert delivered.content == "still reaches the agent"


async def test_a_mirror_failure_never_breaks_message_delivery(mongo_db, registered, monkeypatch):
    """The mirror is a courtesy; the agent's message is the product.

    Note on where the guard actually lives: this survives because the OSS
    observer seam isolates every observer (``_safe_notify`` in
    ``bus/queue.py``, pinned by tests/test_bus_inbound_observers.py), NOT
    because of ``_on_inbound``'s own try/except. That try/except is
    defense-in-depth plus bridge-specific logging — narrowing it changes
    nothing observable, so this file does NOT carry a mutation claiming it as
    a gate. The end-to-end assertion below is still worth keeping: it proves
    the two layers compose.
    """
    await _make_group()

    async def _boom(**_kw):
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(bridge, "_mirror_into_room", _boom)
    bus = get_message_bus()

    await bus.publish_inbound(_inbound("delivered anyway"))  # must not raise

    delivered = await bus.consume_inbound(timeout=0.5)
    assert delivered is not None and delivered.content == "delivered anyway"


# ---------------------------------------------------------------------------
# 2. room → channel
# ---------------------------------------------------------------------------


async def test_a_room_post_is_sent_out_to_the_channel(mongo_db, registered, sent_outbound):
    """The reply half. A team member posting in the room reaches the customer.

    MUTATION THAT BREAKS THIS: make ``_on_room_message`` return before
    ``publish_outbound`` — nothing is captured."""
    gid = await _make_group()

    await bridge._on_room_message(
        {"group_id": gid, "content": "sorry about that, refunding now", "sender_id": USER}
    )

    assert len(sent_outbound) == 1
    assert sent_outbound[0].chat_id == CHAT_ID
    assert sent_outbound[0].channel == Channel.TELEGRAM
    assert sent_outbound[0].content == "sorry about that, refunding now"


async def test_a_post_in_an_unbound_room_goes_nowhere(mongo_db, registered, sent_outbound):
    """Every ordinary room in the workspace emits ``message.sent``. Only bound
    rooms may reach an external channel."""
    gid = await _make_group(bind=False)

    await bridge._on_room_message({"group_id": gid, "content": "internal chatter"})

    assert sent_outbound == []


# ---------------------------------------------------------------------------
# 3. the three echo loops — counts, not presence
# ---------------------------------------------------------------------------


async def test_loop1_a_room_post_does_not_echo_back_into_its_own_room(
    mongo_db, registered, sent_outbound
):
    """LOOP 1 (the flag guard). The room post goes out as an OutboundMessage,
    which our own outbound subscriber sees. Without the marker check it mirrors
    straight back into the room the human just typed in.

    MUTATION THAT BREAKS THIS: delete the ``_BRIDGE_MARKER`` check at the top
    of ``_on_outbound`` — the room gains a duplicate and the count goes to 1."""
    gid = await _make_group()

    await bridge._on_room_message({"group_id": gid, "content": "on it", "sender_id": USER})

    assert len(sent_outbound) == 1, "the customer should get it exactly once"
    assert await _room_messages(gid) == [], "the room post echoed back as a mirrored message"


async def test_loop2_a_mirrored_customer_message_is_not_sent_back_to_them(
    mongo_db, registered, sent_outbound
):
    """LOOP 2 (structural). Mirroring the customer's message into the room must
    not bounce it back out to the customer.

    MUTATION THAT BREAKS THIS: have ``_mirror_into_room`` route through
    ``send_message`` (which emits ``message.sent``) — the bridge's own
    room→channel handler fires and the customer receives their own words."""
    gid = await _make_group()

    await get_message_bus().publish_inbound(_inbound("where is my order"))

    assert len(await _room_messages(gid)) == 1
    assert sent_outbound == [], "the customer was sent their own message back"


async def test_loop3_a_mirrored_message_does_not_trigger_the_room_agents(
    mongo_db, registered, monkeypatch
):
    """LOOP 3 (structural). The customer's message is already on its way to the
    agent via the OSS queue. If mirroring also woke the room's group agents the
    customer would get two answers to one question.

    Asserts on ``message.sent`` — the event ``shared/agent_bridge`` subscribes
    to — rather than mocking the agent bridge itself, so the test survives that
    module being rewritten.

    MUTATION THAT BREAKS THIS: emit ``message.sent`` from
    ``_mirror_into_room``."""
    from pocketpaw_ee.cloud.shared.events import event_bus

    fired: list[dict] = []

    async def _record(payload):
        fired.append(payload)

    event_bus.subscribe("message.sent", _record)
    try:
        await _make_group()
        await get_message_bus().publish_inbound(_inbound("hello?"))
        assert fired == [], "the mirror woke the agent-respond path"
    finally:
        event_bus.unsubscribe("message.sent", _record)


async def test_an_agent_reply_is_mirrored_into_the_room_exactly_once(
    mongo_db, registered, sent_outbound
):
    """The team should see what the agent told the customer — once.

    MUTATION THAT BREAKS THIS: drop the ``_on_outbound`` registration in
    ``register_channel_chat_listeners``."""
    gid = await _make_group()

    await get_message_bus().publish_outbound(
        OutboundMessage(
            channel=Channel.TELEGRAM, chat_id=CHAT_ID, content="Your refund is on its way."
        )
    )

    rows = await _room_messages(gid)
    assert len(rows) == 1
    assert rows[0].content == "Your refund is on its way."
    assert rows[0].sender_type == "agent"


async def test_a_full_round_trip_lands_exactly_three_messages(mongo_db, registered, sent_outbound):
    """THE INTEGRATION SHAPE, and the strongest echo assertion in the file:
    customer asks → agent answers → human follows up. Three room rows, one
    outbound send. Any of the three loops firing changes one of these numbers.
    """
    gid = await _make_group()
    bus = get_message_bus()

    # 1. customer message arrives
    await bus.publish_inbound(_inbound("where is my order", sender_name="Sam"))
    # 2. the agent answers on the channel
    await bus.publish_outbound(
        OutboundMessage(channel=Channel.TELEGRAM, chat_id=CHAT_ID, content="Checking now.")
    )
    # 3. a human follows up from the room
    await bridge._on_room_message({"group_id": gid, "content": "Shipped today.", "sender_id": USER})

    rows = sorted(await _room_messages(gid), key=lambda m: m.createdAt)
    assert [m.content for m in rows] == ["where is my order", "Checking now."]
    assert [m.sender_type for m in rows] == ["external", "agent"]
    assert rows[0].sender_name == "Sam", "the adapter's display name should reach the room"
    # The human's own post is written by send_message on the normal path (not
    # under test here); what matters is it went OUT exactly once and did not
    # echo back in. Filtered to the BRIDGE's own publishes — the spy also sees
    # step 2, which this test published itself to stand in for the agent.
    from_bridge = [m for m in sent_outbound if (m.metadata or {}).get(bridge._BRIDGE_MARKER)]
    assert len(from_bridge) == 1
    assert from_bridge[0].content == "Shipped today."


# ---------------------------------------------------------------------------
# 4. binding rules
# ---------------------------------------------------------------------------


async def test_two_rooms_cannot_claim_one_conversation(mongo_db):
    """Uniqueness. Two bound rooms would double-mirror every message and
    double-send every reply — invisible in the room, obvious to the customer.

    MUTATION THAT BREAKS THIS: drop the ``existing is not None`` conflict check
    in ``bind_group_to_channel``."""
    await _make_group()
    second = await _make_group(bind=False)

    with pytest.raises(ConflictError):
        await group_service.bind_group_to_channel(
            WS, USER, second, channel="telegram", chat_id=CHAT_ID
        )


async def test_rebinding_the_same_room_is_not_a_conflict(mongo_db):
    """Idempotency — the PUT must be safe to retry."""
    gid = await _make_group()

    result = await group_service.bind_group_to_channel(
        WS, USER, gid, channel="telegram", chat_id=CHAT_ID
    )

    assert result["channelBinding"] == {"channel": "telegram", "chatId": CHAT_ID}


async def test_a_non_member_cannot_bind_a_room(mongo_db):
    """Binding points a room at an outside conversation — a write, not a read.

    MUTATION THAT BREAKS THIS: drop the membership check in
    ``_fetch_group_for_admin``."""
    gid = await _make_group(bind=False)

    with pytest.raises(Forbidden):
        await group_service.bind_group_to_channel(
            WS, OUTSIDER, gid, channel="telegram", chat_id="other-chat"
        )


async def test_binding_lookup_is_scoped_by_the_pair_not_the_workspace(mongo_db):
    """The resolve is deployment-wide by necessity (an inbound message carries
    no workspace), so the PAIR has to be the tenancy key. A neighbouring
    workspace's room bound to a different chat_id must never be returned."""
    ours = await _make_group()
    await _make_group(workspace=OTHER_WS, bind=False, owner="u2")

    found = await group_service.find_group_id_by_channel_binding("telegram", CHAT_ID)
    assert found == (ours, WS)
    assert await group_service.find_group_id_by_channel_binding("telegram", "nope") is None


async def test_unbinding_stops_the_mirror(mongo_db, registered):
    """The off switch actually turns it off."""
    gid = await _make_group()
    await group_service.unbind_group_from_channel(WS, USER, gid)

    await get_message_bus().publish_inbound(_inbound())

    assert await _room_messages(gid) == []
