# tests/cloud/chat/test_send_message_fanout.py
# Guards the send_message notification fan-out: notifications are written with
# one insert_many per kind, the workspace delivery config is read once per
# message, external webhook delivery runs in the background (the request does
# not wait on a slow sink), recipients are unchanged, and a message with the
# startup bus handlers registered bumps message_count and writes each mention
# notification exactly once.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core.realtime.events import NotificationNew
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.chat.schemas import SendMessageRequest
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
from pocketpaw_ee.cloud.notifications import delivery
from pocketpaw_ee.cloud.notifications import service as notifications_service
from pocketpaw_ee.cloud.shared.events import event_bus

WEBHOOK_URL = "https://alerts.example.com/ingest"


async def _group(members: list[str]) -> _GroupDoc:
    doc = _GroupDoc(
        workspace="w1", name="G", slug="g", type="private", members=members, owner=members[0]
    )
    await doc.insert()
    return doc


async def _drain_delivery() -> None:
    if delivery._inflight:
        await asyncio.gather(*list(delivery._inflight))


@pytest.fixture
def _no_mention_bump(monkeypatch):
    async def _bump(_user_id, _group_id):
        return None

    monkeypatch.setattr(message_service.unread_service, "bump_mention", _bump)


async def test_notifications_are_batched_and_recipients_unchanged(
    mongo_db, recording_bus, monkeypatch, _no_mention_bump
):
    group = await _group(["sender", "u2", "u3", "u4"])
    batches: list[list] = []
    real_insert_many = _NotificationDoc.insert_many

    async def spy_insert_many(docs, *args, **kwargs):
        batches.append([d.recipient for d in docs])
        return await real_insert_many(docs, *args, **kwargs)

    async def no_single_insert(*_a, **_k):
        raise AssertionError("per-member Notification.insert on the send path")

    monkeypatch.setattr(_NotificationDoc, "insert_many", spy_insert_many)
    monkeypatch.setattr(notifications_service, "create", no_single_insert)

    body = SendMessageRequest(
        content="hi",
        mentions=[
            {"type": "user", "id": "u2", "display_name": "@u2"},
            {"type": "user", "id": "sender", "display_name": "@me"},  # self: skipped
        ],
    )
    await message_service.send_message(str(group.id), "sender", body)
    await _drain_delivery()

    assert batches == [["u2", "u3", "u4"], ["u2"]]  # one insert per kind
    rows = await _NotificationDoc.find_all().to_list()
    by_kind = {k: sorted(r.recipient for r in rows if r.type == k) for k in ("message", "mention")}
    assert by_kind == {"message": ["u2", "u3", "u4"], "mention": ["u2"]}
    assert all(r.id is not None for r in rows)
    emitted = sorted(
        e.data["user_id"] for e in recording_bus.events if isinstance(e, NotificationNew)
    )
    assert emitted == ["u2", "u2", "u3", "u4"]


async def test_external_delivery_is_off_the_request_and_reads_config_once(
    mongo_db, recording_bus, monkeypatch, _no_mention_bump
):
    await notifications_service.set_delivery_config(
        "w1", slack_webhook_url="", webhook_url=WEBHOOK_URL, enabled=True, routes={}
    )
    group = await _group(["sender", "u2", "u3"])

    loads = 0
    real_load = delivery._load_config

    async def counting_load(workspace_id):
        nonlocal loads
        loads += 1
        return await real_load(workspace_id)

    gate = asyncio.Event()
    posted: list[tuple[str, str]] = []

    async def slow_post(_client, _sink, _url, notification):
        await gate.wait()  # a sink that hangs until the test releases it
        posted.append((notification.kind, notification.recipient_id))

    monkeypatch.setattr(delivery, "_load_config", counting_load)
    monkeypatch.setattr(delivery, "_post_one", slow_post)

    body = SendMessageRequest(
        content="hi all", mentions=[{"type": "everyone", "id": "", "display_name": "@everyone"}]
    )
    # Returns while every sink is still hung: delivery is not on the request path.
    await asyncio.wait_for(message_service.send_message(str(group.id), "sender", body), 2)
    assert posted == []

    gate.set()
    await _drain_delivery()
    assert loads == 1  # one config read for the whole message, not one per member
    assert sorted(posted) == [
        ("mention", "u2"),
        ("mention", "u3"),
        ("message", "u2"),
        ("message", "u3"),
    ]


async def test_stats_and_mentions_are_written_exactly_once_with_handlers_registered(
    mongo_db, recording_bus, _no_mention_bump
):
    from pocketpaw_ee.cloud.shared.event_handlers import register_event_handlers

    saved = {k: list(v) for k, v in event_bus._handlers.items()}
    register_event_handlers()
    try:
        group = await _group(["sender", "u2", "u3"])
        body = SendMessageRequest(
            content="hey u2", mentions=[{"type": "user", "id": "u2", "display_name": "@u2"}]
        )
        await message_service.send_message(str(group.id), "sender", body)
        await _drain_delivery()
    finally:
        event_bus._handlers.clear()
        event_bus._handlers.update(saved)

    refreshed = await _GroupDoc.get(group.id)
    assert refreshed is not None
    assert refreshed.message_count == 1
    mentions = await _NotificationDoc.find({"type": "mention"}).to_list()
    assert [m.recipient for m in mentions] == ["u2"]


async def test_meeting_notes_bump_group_stats_themselves():
    """Meeting notes used to rely on the deleted bus handler for message_count."""
    from unittest.mock import AsyncMock, patch

    from pocketpaw_ee.cloud.livekit.service import post_meeting_notes_to_group

    with (
        patch(
            "pocketpaw_ee.cloud.chat.message_service._create_group_message_doc",
            new_callable=AsyncMock,
        ) as mock_create,
        patch("pocketpaw_ee.cloud.shared.events.event_bus.emit", new_callable=AsyncMock),
        patch(
            "pocketpaw_ee.cloud.chat.group_service.bump_message_stats", new_callable=AsyncMock
        ) as bump,
    ):
        mock_create.return_value.id = "msg_1"
        await post_meeting_notes_to_group(
            group_id="g1",
            transcript="",
            summary="s",
            action_items=[],
            participants=[],
            duration_seconds=1,
        )
    bump.assert_awaited_once()
    assert bump.await_args.args == ("g1",)
