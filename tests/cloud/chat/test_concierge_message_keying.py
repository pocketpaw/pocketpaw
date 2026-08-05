# tests/cloud/chat/test_concierge_message_keying.py — the concierge branch of
# persist_assistant_message_for_scope. Created 2026-07-30 (inbox slice 0).
#
# The seam this pins: ContextType has no "concierge" value, so a concierge reply
# used to fall through to the group branch and write
# Message(context_type="group", group=<pocket_id>, session_key unset) — an orphan
# row no surface reads, keyed by a pocket id in a field meaning "room id".
# Concierge transcripts derive from ChatRunDoc, so the fix is to persist nothing
# while keeping the caller contract (msg.id for mark_completed + the SSE tail,
# msg.createdAt for the broadcast).
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws-1"
_POCKET = "6a6a0c5e1012578f4e4c1e69"
_SESSION_KEY = f"cloud:concierge:{_POCKET}:cust-abc12345:agent-1"


async def _persist(kind: str) -> _MessageDoc:
    return await message_service.persist_assistant_message_for_scope(
        kind=kind,
        scope_id=_POCKET,
        user_id="cust-abc12345",
        workspace_id=_WS,
        session_key=_SESSION_KEY,
        target_agent_id="agent-1",
        content="Our hours are 7am to 6pm.",
    )


async def test_concierge_reply_writes_no_message_row() -> None:
    """The orphan is gone: a concierge turn persists NOTHING."""
    before = await _MessageDoc.find_all().count()

    await _persist("concierge")

    assert await _MessageDoc.find_all().count() == before, (
        "a concierge reply wrote a Message row — the orphan seam is back"
    )
    # Specifically: no group-keyed row carrying the pocket id.
    assert await _MessageDoc.find(_MessageDoc.group == _POCKET).count() == 0


async def test_concierge_reply_still_satisfies_the_caller_contract() -> None:
    """run_core needs a usable id + createdAt off the returned doc."""
    msg = await _persist("concierge")

    assert msg.id is not None and str(msg.id) != "None"
    assert msg.createdAt is not None
    assert msg.content == "Our hours are 7am to 6pm."
    # Two turns never collide on the synthetic id.
    other = await _persist("concierge")
    assert str(other.id) != str(msg.id)


async def test_pocket_and_session_kinds_still_persist() -> None:
    """The fix is scoped to concierge — the real surfaces are untouched."""
    before = await _MessageDoc.find_all().count()

    session_msg = await _persist("session")
    pocket_msg = await _persist("pocket")

    assert await _MessageDoc.find_all().count() == before + 2
    assert session_msg.context_type == "session"
    assert session_msg.session_key == _SESSION_KEY
    assert pocket_msg.context_type == "pocket"


async def test_unknown_kind_keeps_the_group_fallback() -> None:
    """An unrecognised kind still lands in the group branch (unchanged)."""
    before = await _MessageDoc.find(_MessageDoc.group == _POCKET).count()

    await _persist("something-else")

    assert await _MessageDoc.find(_MessageDoc.group == _POCKET).count() == before + 1
