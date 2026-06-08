# tests/cloud/chat/test_about_member_block.py — agent member-orientation block.
# Created: 2026-06-08 (feat/vip-agent-block, pp#1367).
#
# Locks the contract for the "about this member" block that orients the chat
# agent to who is talking (pp#1367):
#
#   1. WITH a Person — the rendered block is APPENDED to the assembled system
#      message (build_behavior_instructions / build_context_block), carrying
#      name · role · team · focus. Additive: the base persona (runtime-identity
#      rule, ripple prompt) is still present — the block does NOT replace it.
#   2. WITHOUT a Person — ctx.about_member_block is None → no block in the
#      assembled message, and assembly does not raise. Behavior == today.
#   3. The render is HARD char-capped (_ABOUT_MEMBER_CHAR_CAP) so a giant focus
#      line can't bloat the always-on system prompt (the known ~20K-char mode).
#   4. The async resolver (_resolve_about_member) degrades to None when the
#      member has no Person and when the people read raises — never an error.
#
# The block is pre-rendered onto ScopeContext by the resolvers (async); the
# sync assembly just appends it. So the assembly tests construct a ScopeContext
# directly with about_member_block set (mirroring test_pocket_agent_context.py).

from __future__ import annotations

from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    _ABOUT_MEMBER_CHAR_CAP,
    ScopeContext,
    ScopeKind,
    _render_about_member_block,
    _resolve_about_member,
    build_behavior_instructions,
    build_context_block,
)
from pocketpaw_ee.cloud.people.domain import Person


def _person(
    *,
    name: str = "Ada Lovelace",
    role: str = "admin",
    group: str | None = "team-eng",
    focus: str = "Own the billing rewrite",
) -> Person:
    return Person(
        id="person-ws1-user-7",
        workspace_id="ws1",
        user_id="user-7",
        name=name,
        email="ada@x.c",
        avatar="",
        role=role,
        group=group,
        focus=focus,
        profile_pic="",
        invited_by="admin-1",
    )


def _ctx(*, about_member_block: str | None) -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="ws1",
        user_id="user-7",
        members=["user-7"],
        target_agent_id="a1",
        about_member_block=about_member_block,
    )


# ---------------------------------------------------------------------------
# 1. WITH a Person — block is appended to the assembled system message
# ---------------------------------------------------------------------------


class TestBlockPresentWithPerson:
    def test_block_appended_to_behavior_instructions(self) -> None:
        block = _render_about_member_block(_person())
        ctx = _ctx(about_member_block=block)

        assembled = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")

        # The block — and its identity content — is in the assembled message.
        assert "<about-member>" in assembled
        assert "Ada Lovelace" in assembled
        assert "admin" in assembled
        assert "team team-eng" in assembled
        assert "Own the billing rewrite" in assembled

    def test_block_is_additive_not_a_replacement(self) -> None:
        # The base persona content must still be present alongside the block —
        # the block is APPENDED, it does not clobber the base system message.
        block = _render_about_member_block(_person())
        ctx = _ctx(about_member_block=block)

        assembled = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")

        # Base persona markers (runtime-identity rule + ripple law) survive.
        assert "<runtime-identity>" in assembled
        assert "<ripple>" in assembled
        # And the block comes AFTER the base content (appended last).
        assert assembled.index("<about-member>") > assembled.index("<runtime-identity>")

    def test_block_present_via_build_context_block(self) -> None:
        # The combined entry point used by tests / legacy callers also carries it.
        block = _render_about_member_block(_person())
        ctx = _ctx(about_member_block=block)

        assembled = build_context_block(ctx, backend_name="claude_agent_sdk")
        assert "<about-member>" in assembled
        assert "Ada Lovelace" in assembled


# ---------------------------------------------------------------------------
# 2. WITHOUT a Person — no block, no error
# ---------------------------------------------------------------------------


class TestGracefullyAbsent:
    def test_no_block_when_about_member_is_none(self) -> None:
        ctx = _ctx(about_member_block=None)

        assembled = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")

        # No member block, and the base persona is unaffected.
        assert "<about-member>" not in assembled
        assert "<runtime-identity>" in assembled

    def test_assembly_does_not_raise_without_person(self) -> None:
        ctx = _ctx(about_member_block=None)
        # Must not raise on any backend path.
        build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
        build_behavior_instructions(ctx, backend_name="codex_cli")
        build_context_block(ctx, backend_name="claude_agent_sdk")

    def test_render_returns_empty_for_nameless_person(self) -> None:
        # A Person with no usable name yields no block (treated as "no Person").
        assert _render_about_member_block(_person(name="")) == ""
        assert _render_about_member_block(_person(name="   ")) == ""


# ---------------------------------------------------------------------------
# 3. Render is HARD char-capped (prompt-bloat backstop)
# ---------------------------------------------------------------------------


class TestTokenCap:
    def test_huge_focus_is_capped(self) -> None:
        # A pathological 20K-char focus line must not produce a 20K-char block.
        huge = "billing " * 4000  # ~32K chars
        block = _render_about_member_block(_person(focus=huge))

        assert len(block) <= _ABOUT_MEMBER_CHAR_CAP + len("…\n</about-member>")
        # ~4 chars/token ⇒ comfortably under the ~400-token budget.
        assert len(block) / 4 <= 400
        # Still well-formed: opens and closes the tag.
        assert block.startswith("<about-member>")
        assert block.rstrip().endswith("</about-member>")

    def test_cap_constant_is_within_budget(self) -> None:
        # Guard the budget itself: the char cap must stay under ~400 tokens
        # (~4 chars/token) so the block can never breach the stated limit.
        assert _ABOUT_MEMBER_CHAR_CAP / 4 <= 400

    def test_normal_block_is_small(self) -> None:
        # A realistic block is a fraction of the cap — the cap is only a backstop.
        block = _render_about_member_block(_person())
        assert len(block) < _ABOUT_MEMBER_CHAR_CAP // 2

    def test_focus_whitespace_is_collapsed(self) -> None:
        # Newlines / runs of spaces in the focus are collapsed to a single line
        # (keeps the block a tidy one-liner and helps the cap math).
        block = _render_about_member_block(_person(focus="own\n\n  the   billing\trewrite"))
        assert "own the billing rewrite" in block
        assert "\n\n" not in block.split("focus:")[1]


# ---------------------------------------------------------------------------
# 4. _resolve_about_member degrades gracefully
# ---------------------------------------------------------------------------


class TestResolveAboutMember:
    @pytest.mark.asyncio
    async def test_resolve_returns_block_for_member_with_person(self) -> None:
        async def _fake_get_person(workspace_id: str, user_id: str):
            return _person()

        with patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_fake_get_person):
            block = await _resolve_about_member("ws1", "user-7")

        assert block is not None
        assert "<about-member>" in block
        assert "Ada Lovelace" in block

    @pytest.mark.asyncio
    async def test_resolve_returns_none_when_no_person(self) -> None:
        async def _fake_get_person(workspace_id: str, user_id: str):
            return None

        with patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_fake_get_person):
            block = await _resolve_about_member("ws1", "user-7")

        assert block is None

    @pytest.mark.asyncio
    async def test_resolve_returns_none_when_read_raises(self) -> None:
        async def _boom(workspace_id: str, user_id: str):
            raise RuntimeError("journal exploded")

        with patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_boom):
            # Must swallow the error and degrade to None — never propagate.
            block = await _resolve_about_member("ws1", "user-7")

        assert block is None

    @pytest.mark.asyncio
    async def test_resolve_returns_none_on_empty_ids(self) -> None:
        # No workspace / user ⇒ no read attempted, None returned.
        assert await _resolve_about_member("", "user-7") is None
        assert await _resolve_about_member("ws1", "") is None
