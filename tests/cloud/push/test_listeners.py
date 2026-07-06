# Tests for the product-event → notification dispatch listeners (pocketpaw#1393).
# Created: 2026-06-09 (feat/push-wire-events) — proves each v1 handler resolves
# the right recipients + payload and routes through ``dispatch.notify``:
#   - on_agent_complete: resolves the group's workspace + members and notifies
#     every human member (agent.stream_end carries no workspace_id);
#   - on_guardian_block: notifies ``requested_by`` in the event's workspace;
#   - on_meeting_started: notifies the creator (recall) or group members
#     (livekit), mirroring the meeting bridge.
# ``dispatch.notify`` is patched to record calls — no WS, no Web Push, no DB.
# group_service lookups are patched so no Mongo is needed.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    AgentStreamEnd,
    InstinctApprovalCreated,
    MessageSent,
)
from pocketpaw_ee.cloud.meetings.events import MeetingStarted
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
    """Run on_new_message with the coalescer OFF (pass-through) so these tests
    assert the recipient/payload logic directly. The leading-edge throttle has
    its own dedicated suite in test_coalesce.py. ``reset()`` clears any window
    state so a lingering task can't leak into the next test."""
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", "0")
    listeners.coalesce.reset()
    yield
    listeners.coalesce.reset()


# ---------------------------------------------------------------------------
# agent-complete (agent.stream_end)
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
# guardian-block (instinct.approval.created)
# ---------------------------------------------------------------------------


async def test_guardian_block_notifies_requester(notify_calls) -> None:
    event = InstinctApprovalCreated(
        data={
            "workspace_id": "w1",
            "requested_by": "carol",
            "action_name": "send_email",
        }
    )
    await listeners.on_guardian_block(event)

    assert len(notify_calls) == 1
    ws, uid, payload = notify_calls[0]
    assert ws == "w1"
    assert uid == "carol"
    assert "send_email" in payload["body"]


async def test_guardian_block_noop_without_recipient(notify_calls) -> None:
    await listeners.on_guardian_block(InstinctApprovalCreated(data={"workspace_id": "w1"}))
    assert notify_calls == []


# ---------------------------------------------------------------------------
# meeting-start (meeting.started)
# ---------------------------------------------------------------------------


async def test_meeting_started_notifies_creator_for_recall(notify_calls) -> None:
    event = MeetingStarted(
        data={
            "workspace_id": "w1",
            "meeting_id": "m1",
            "source": "recall",
            "created_by": "dave",
        }
    )
    await listeners.on_meeting_started(event)

    assert len(notify_calls) == 1
    ws, uid, payload = notify_calls[0]
    assert (ws, uid) == ("w1", "dave")
    assert "meeting-m1" in payload["url"]


async def test_meeting_started_notifies_group_for_livekit(notify_calls, monkeypatch) -> None:
    async def fake_members(group_id):
        return ["eve", "frank"]

    monkeypatch.setattr(listeners, "_group_member_ids", fake_members)

    event = MeetingStarted(
        data={
            "workspace_id": "w1",
            "meeting_id": "m2",
            "source": "livekit",
            "group_id": "g9",
        }
    )
    await listeners.on_meeting_started(event)

    assert {uid for _, uid, _ in notify_calls} == {"eve", "frank"}


async def test_meeting_started_noop_without_meeting(notify_calls) -> None:
    await listeners.on_meeting_started(MeetingStarted(data={"workspace_id": "w1"}))
    assert notify_calls == []


# ---------------------------------------------------------------------------
# new-message (message.sent) — user↔user notifications
# ---------------------------------------------------------------------------


@pytest.fixture
def new_message_env(monkeypatch):
    """Patch the group + unread lookups so on_new_message needs no Mongo."""

    async def fake_ws_id(group_id):
        return "w-msg"

    async def fake_members(group_id):
        return ["alice", "bob", "carol"]

    async def fake_count(user_id, group_id):
        return 3

    monkeypatch.setattr(listeners, "_group_workspace_id", fake_ws_id)
    monkeypatch.setattr(listeners, "_group_member_ids", fake_members)
    monkeypatch.setattr(listeners, "_unread_count", fake_count)


async def test_new_message_notifies_other_members_with_count(notify_calls, new_message_env) -> None:
    event = MessageSent(
        data={
            "group_id": "g1",
            "sender_id": "alice",
            "senderName": "Alice",
            "senderType": "user",
        }
    )
    await listeners.on_new_message(event)

    # Alice is the sender → excluded; only bob + carol are notified.
    assert {uid for _, uid, _ in notify_calls} == {"bob", "carol"}
    assert all(ws == "w-msg" for ws, _, _ in notify_calls)
    # Title names the sender; body carries ONLY the count (no message text).
    assert all(p["title"] == "Alice" for _, _, p in notify_calls)
    assert all(p["body"] == "3 new messages" for _, _, p in notify_calls)
    # Collapse per-conversation so a later message updates the same toast.
    assert all(p["tag"] == "g1" for _, _, p in notify_calls)


async def test_new_message_body_never_leaks_content(notify_calls, new_message_env) -> None:
    secret = "wire me $10k to account 12345"
    event = MessageSent(
        data={
            "group_id": "g1",
            "sender_id": "alice",
            "senderName": "Alice",
            "senderType": "user",
            "content": secret,
        }
    )
    await listeners.on_new_message(event)

    assert notify_calls  # sanity: it did notify
    for _, _, payload in notify_calls:
        assert secret not in payload["body"]
        assert secret not in payload["title"]


async def test_new_message_singular_count_body(notify_calls, monkeypatch) -> None:
    async def fake_ws_id(group_id):
        return "w"

    async def fake_members(group_id):
        return ["a", "b"]

    async def fake_count(user_id, group_id):
        return 1

    monkeypatch.setattr(listeners, "_group_workspace_id", fake_ws_id)
    monkeypatch.setattr(listeners, "_group_member_ids", fake_members)
    monkeypatch.setattr(listeners, "_unread_count", fake_count)

    event = MessageSent(
        data={"group_id": "g", "sender_id": "a", "senderName": "A", "senderType": "user"}
    )
    await listeners.on_new_message(event)

    assert notify_calls
    assert all(p["body"] == "1 new message" for _, _, p in notify_calls)


async def test_new_message_skips_agent_authored(notify_calls, monkeypatch) -> None:
    # An agent-authored message.sent (senderType != "user") must not fire here;
    # agent replies are covered by on_agent_complete (agent.stream_end).
    async def boom(*_a, **_k):
        raise AssertionError("agent-authored message must not resolve recipients")

    monkeypatch.setattr(listeners, "_group_workspace_id", boom)

    await listeners.on_new_message(
        MessageSent(data={"group_id": "g", "sender_id": "bot", "senderType": "agent"})
    )
    assert notify_calls == []


async def test_new_message_noop_without_group(notify_calls) -> None:
    await listeners.on_new_message(MessageSent(data={"sender_id": "a", "senderType": "user"}))
    assert notify_calls == []


async def test_new_message_noop_when_sender_is_only_member(notify_calls, monkeypatch) -> None:
    async def fake_ws_id(group_id):
        return "w"

    async def fake_members(group_id):
        return ["solo"]

    monkeypatch.setattr(listeners, "_group_workspace_id", fake_ws_id)
    monkeypatch.setattr(listeners, "_group_member_ids", fake_members)

    await listeners.on_new_message(
        MessageSent(data={"group_id": "g", "sender_id": "solo", "senderType": "user"})
    )
    assert notify_calls == []
