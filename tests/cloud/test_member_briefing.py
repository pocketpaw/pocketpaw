# tests/cloud/test_member_briefing.py
# Created: 2026-06-08 — VIP Onboarding Phase B chunk 5 (the agent "your day"
#   briefing). RED-before / GREEN-after tests for the briefing block that the
#   agent receives at the top of the member's OWN solo session.
#
# The centerpiece is the gate parity with the KB ``user:`` scope: the "your
# day" block is injected ONLY in the member's solo session (``members ==
# [user_id]``) and is ABSENT in every shared / multi-member room — the exact
# same rule that keeps one member's private mail/calendar out of another
# member's agent context. Plus:
#   * the block is CAPPED (≤ ~400 tokens → a hard char cap) so it can't eat
#     the system-prompt budget.
#   * an EMPTY digest (no connected accounts) → NO block, no error (the agent
#     behaves exactly as today).
#   * a digest that RAISES → NO block, no crash (the stream is never sunk by a
#     flaky mail/calendar pull).
#
# The digest pull is injected as a fake (``digest_fn=``) so the suite needs no
# OAuth, no network, no Mongo.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    ScopeContext,
    ScopeKind,
    _member_briefing_block,
    build_knowledge_context,
)
from pocketpaw_ee.cloud.member_day_digest.dto import (  # noqa: E402
    DigestEvent,
    DigestMail,
    MemberDayDigest,
)

pytestmark = pytest.mark.asyncio


def _ctx(*, user_id: str, members: list[str], kind: ScopeKind = ScopeKind.SESSION) -> ScopeContext:
    return ScopeContext(
        kind=kind,
        scope_id="scope-1",
        workspace_id="w1",
        user_id=user_id,
        members=list(members),
        target_agent_id="a1",
    )


def _full_digest(member_id: str) -> MemberDayDigest:
    return MemberDayDigest(
        workspace_id="w1",
        member_id=member_id,
        events=[
            DigestEvent(summary="Standup", start="2026-06-09T09:00:00Z", end="", location="Zoom"),
            DigestEvent(summary="1:1 with Sam", start="2026-06-09T14:00:00Z", end="", location=""),
        ],
        unread_mail_count=4,
        top_mail=[
            DigestMail(subject="Invoice from Acme", sender="billing@acme.com", date=""),
            DigestMail(subject="Re: launch plan", sender="sam@team.com", date=""),
        ],
    )


def _digest_fn_returning(digest: MemberDayDigest):
    async def _fn(workspace_id: str, member_id: str):
        return digest

    return _fn


# --------------------------------------------------------------------------
# 1 — solo session: the block is PRESENT and carries the member's day.
# --------------------------------------------------------------------------


async def test_briefing_present_in_solo_session():
    member = "memberA"
    ctx = _ctx(user_id=member, members=[member])
    block = await _member_briefing_block(ctx, digest_fn=_digest_fn_returning(_full_digest(member)))

    assert block  # non-empty
    # It carries the member's real day content.
    assert "Standup" in block
    assert "Invoice from Acme" in block
    # And is wrapped in the dedicated "your day" briefing marker, not a raw dump.
    assert "<your-day>" in block


# --------------------------------------------------------------------------
# 2 — THE GATE. A shared / multi-member room gets NO block. Same rule as the
# KB ``user:`` scope: emit ⟺ members == [user_id].
# --------------------------------------------------------------------------


async def test_briefing_absent_in_multi_member_room():
    ctx = _ctx(
        user_id="memberA",
        members=["memberA", "memberB", "memberC"],
        kind=ScopeKind.GROUP,
    )
    # Even if the (would-be) digest is rich, the gate suppresses it entirely:
    block = await _member_briefing_block(
        ctx, digest_fn=_digest_fn_returning(_full_digest("memberA"))
    )
    assert block == ""


async def test_briefing_absent_in_two_person_dm():
    ctx = _ctx(user_id="memberA", members=["memberA", "memberB"], kind=ScopeKind.DM)
    block = await _member_briefing_block(
        ctx, digest_fn=_digest_fn_returning(_full_digest("memberA"))
    )
    assert block == ""


async def test_briefing_absent_when_principal_not_sole_member():
    """Defense-in-depth: a single-member room whose member is NOT the caller
    (stale membership / wrong principal) gets no block."""
    ctx = _ctx(user_id="memberA", members=["memberX"], kind=ScopeKind.POCKET)
    block = await _member_briefing_block(
        ctx, digest_fn=_digest_fn_returning(_full_digest("memberX"))
    )
    assert block == ""


async def test_briefing_gate_does_not_even_pull_digest_in_shared_room():
    """The gate short-circuits BEFORE the digest pull — a shared room never
    triggers a member's mail/calendar read at all (no wasted I/O, and no path
    where another member's session could cause a private pull)."""
    pulled: list[str] = []

    async def _spy(workspace_id: str, member_id: str):
        pulled.append(member_id)
        return _full_digest(member_id)

    ctx = _ctx(user_id="memberA", members=["memberA", "memberB"], kind=ScopeKind.GROUP)
    block = await _member_briefing_block(ctx, digest_fn=_spy)
    assert block == ""
    assert pulled == []  # digest was never pulled in a shared room


# --------------------------------------------------------------------------
# 3 — capped: the block is bounded so it can't eat the prompt budget.
# --------------------------------------------------------------------------


async def test_briefing_is_capped():
    member = "memberA"
    # A huge digest — many events + many mail items.
    big = MemberDayDigest(
        workspace_id="w1",
        member_id=member,
        events=[
            DigestEvent(summary="Event " + "x" * 200, start="2026-06-09T09:00:00Z")
            for _ in range(50)
        ],
        unread_mail_count=999,
        top_mail=[DigestMail(subject="Subject " + "y" * 200, sender="a@b.com") for _ in range(50)],
    )
    ctx = _ctx(user_id=member, members=[member])
    block = await _member_briefing_block(ctx, digest_fn=_digest_fn_returning(big))

    # Hard char cap (~400 tokens ≈ 1600 chars). The module exposes the cap so
    # the test pins the same constant the impl uses.
    from pocketpaw_ee.cloud.chat import agent_service

    assert len(block) <= agent_service._BRIEFING_MAX_CHARS
    assert block  # still produced something


# --------------------------------------------------------------------------
# 4 — empty digest → NO block (no connected accounts; behave as today).
# --------------------------------------------------------------------------


async def test_briefing_empty_digest_yields_no_block():
    member = "memberA"
    empty = MemberDayDigest(workspace_id="w1", member_id=member)
    assert empty.empty
    ctx = _ctx(user_id=member, members=[member])
    block = await _member_briefing_block(ctx, digest_fn=_digest_fn_returning(empty))
    assert block == ""


# --------------------------------------------------------------------------
# 5 — a digest that RAISES → NO block, no crash.
# --------------------------------------------------------------------------


async def test_briefing_digest_failure_is_swallowed():
    member = "memberA"

    async def _boom(workspace_id: str, member_id: str):
        raise RuntimeError("calendar API exploded")

    ctx = _ctx(user_id=member, members=[member])
    block = await _member_briefing_block(ctx, digest_fn=_boom)
    assert block == ""  # no crash, no block


# --------------------------------------------------------------------------
# 6 — integration: build_knowledge_context INCLUDES the briefing in a solo
# session and EXCLUDES it in a shared room.
# --------------------------------------------------------------------------


async def test_knowledge_context_includes_briefing_in_solo_session(monkeypatch):
    member = "memberA"

    async def _fn(workspace_id: str, member_id: str):
        return _full_digest(member_id)

    # Patch the digest entry point the knowledge-context path calls so no
    # OAuth/network is needed.
    monkeypatch.setattr("pocketpaw_ee.cloud.member_day_digest.service.member_day_digest", _fn)

    ctx = _ctx(user_id=member, members=[member])
    out = await build_knowledge_context(ctx, user_message="hi")
    assert "Standup" in out
    assert "<your-day>" in out


async def test_knowledge_context_excludes_briefing_in_shared_room(monkeypatch):
    member = "memberA"

    async def _fn(workspace_id: str, member_id: str):
        return _full_digest(member_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.member_day_digest.service.member_day_digest", _fn)

    ctx = _ctx(user_id=member, members=[member, "memberB"], kind=ScopeKind.GROUP)
    out = await build_knowledge_context(ctx, user_message="hi")
    assert "Standup" not in out
    assert "<your-day>" not in out
