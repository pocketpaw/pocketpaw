# tests/cloud/chat/test_about_member_block.py — agent member-orientation block.
# Created: 2026-06-08 (feat/vip-agent-block, pp#1367).
# Updated: 2026-08-03 (feat/about-member-id) — adds contract 5: the block carries
#   the member's user_id, so two members sharing a name are distinguishable. The
#   block identified people by name alone, and rooms are shared.
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
    user_id: str = "user-7",
) -> Person:
    return Person(
        id=f"person-ws1-{user_id}",
        workspace_id="ws1",
        user_id=user_id,
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


class TestTwoMembersWithOneName:
    """A name does not identify anybody, and rooms are shared.

    Added 2026-08-03. The block carried name / role / team / focus and no id, so
    two members called the same thing rendered byte-identical blocks: the agent
    could not tell which one it was addressing, and anything it attributed to
    "Alex" became ambiguous the moment a second Alex joined. Not hypothetical —
    ``_resolve_about_member`` is called by every scope resolver and is NOT gated
    on room type, unlike the member-private ``user:`` KB scope.
    """

    def test_same_name_and_role_still_render_different_blocks(self) -> None:
        """MUTATION: drop the ``id:`` line from ``_render_about_member_block``.

        Both members render identically and this fails. (Applied 2026-08-03.)
        """
        one = _render_about_member_block(_person(name="Alex", user_id="user-a"))
        two = _render_about_member_block(_person(name="Alex", user_id="user-b"))
        assert one != two, "two members sharing a name are indistinguishable to the agent"
        assert "user-a" in one
        assert "user-b" in two

    def test_the_id_is_the_cloud_user_id_not_the_fabric_object_id(self) -> None:
        """``person.id`` is ``person-{workspace}-{user}`` and leaks a tenant id.

        The block should carry the same opaque cloud id the KB scope keys on, and
        nothing more.

        MUTATION: render ``person.id`` instead of ``person.user_id``. The
        workspace assertion fails. (Applied 2026-08-03.)
        """
        block = _render_about_member_block(_person(user_id="user-7"))
        assert "  id: user-7" in block
        assert "ws1" not in block, "the block must not carry the workspace id"

    def test_a_member_with_no_id_still_renders(self) -> None:
        """An id-less Person must not lose its block — degrade, never drop.

        MUTATION: make the ``id:`` line unconditional. This raises or renders a
        bare ``id:`` line. (Applied 2026-08-03.)
        """
        block = _render_about_member_block(_person(user_id=""))
        assert "Alex" in block or "Ada" in block
        assert "id:" not in block


class TestTheFoundingAdminIsNotAStranger:
    """A member with no Fabric ``Person`` still gets identified.

    Added 2026-08-04. The Person is created by exactly one path,
    ``materialize_person_from_invite``, so a member who was never invited has
    none — and the founding admin of a workspace is never invited, they created
    it. The block rendered nothing for them, so the agent answered "I don't know
    who you are" while ``full_name`` sat in their user document the whole time.

    Confirmed live before the fix: ``about_member_block`` was ``None`` and the
    36,608-char instruction stack contained neither the member's name nor their
    id. After: ``who: Admin``.
    """

    @pytest.mark.asyncio
    async def test_a_member_with_no_person_is_still_named(self) -> None:
        """The bug, directly.

        MUTATION: make ``_resolve_about_member`` return ``None`` when
        ``get_person`` yields ``None`` (the pre-fix behaviour). Run: the block
        was None and this failed. (Applied 2026-08-04.)
        """

        async def _no_person(workspace_id: str, user_id: str):
            return None

        async def _names(user_ids):
            return {"user-7": "Admin"}

        with (
            patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_no_person),
            patch("pocketpaw_ee.cloud.auth.service.resolve_display_names", side_effect=_names),
        ):
            block = await _resolve_about_member("ws1", "user-7")

        assert block is not None
        assert "Admin" in block
        assert "user-7" in block

    @pytest.mark.asyncio
    async def test_the_fallback_assigns_no_role_or_team(self) -> None:
        """Saying less is the point.

        The user record carries a name and nothing else. A block that invented a
        role would be worse than no block — the agent would state it confidently.

        The assertion is about ASSIGNING a value, not about the words appearing:
        the block's disclaimer necessarily says "role, team and focus", and an
        earlier version of this test asserted ``"team" not in block``, which
        broke the moment the disclaimer got more specific. Wrong property.

        MUTATION: add a ``role: member`` line to the fallback render. Run: the
        no-role-field assertion failed. (Applied 2026-08-04.)
        """

        async def _no_person(workspace_id: str, user_id: str):
            return None

        async def _names(user_ids):
            return {"user-7": "Admin"}

        with (
            patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_no_person),
            patch("pocketpaw_ee.cloud.auth.service.resolve_display_names", side_effect=_names),
        ):
            block = await _resolve_about_member("ws1", "user-7")

        assert "role:" not in block, "the fallback assigned a role it does not know"
        assert "focus:" not in block
        assert "team " not in block.replace("role, team and focus", "")
        assert "do not infer" in block

    @pytest.mark.asyncio
    async def test_a_role_shaped_display_name_is_labelled_as_a_name(self) -> None:
        """The trap this fallback walks straight into if worded carelessly.

        The founding admin's ``full_name`` is very often the literal string
        "Admin" — it is on the deploy where this bug was found. A block that
        said ``who: Admin`` next to "their role is not on file" gives the model
        two readings and an obvious way to reconcile them: decide Admin IS the
        role. That is a guess about the exact field the block exists to stop it
        guessing at.

        So the name must be stated AS a name, and the disclaimer must name the
        trap rather than gesture at it. Checked across the display names most
        likely to be mistaken for roles.

        MUTATION: revert to the ``who: {display}`` field form with a generic
        "do not guess" line. Run: the "is the display name" assertion failed
        for every one of them. (Applied 2026-08-04.)
        """
        for role_shaped in ("Admin", "Owner", "Support", "root"):

            async def _no_person(workspace_id: str, user_id: str):
                return None

            async def _names(user_ids, _n=role_shaped):
                return {"user-7": _n}

            with (
                patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_no_person),
                patch("pocketpaw_ee.cloud.auth.service.resolve_display_names", side_effect=_names),
            ):
                block = await _resolve_about_member("ws1", "user-7")

            assert f"You are talking to {role_shaped} " in block
            assert "is the display name on their account" in block
            assert "NOT their role" in block, (
                f"{role_shaped!r} reads as a role and the block does not say otherwise"
            )

    @pytest.mark.asyncio
    async def test_a_real_person_still_wins(self) -> None:
        """The rich source takes precedence — the fallback is a fallback.

        MUTATION: check the user record FIRST. Run: the thin block rendered and
        the ``focus`` assertion failed. (Applied 2026-08-04.)
        """

        async def _has_person(workspace_id: str, user_id: str):
            return _person(name="Ada Lovelace", focus="Own the billing rewrite")

        with patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_has_person):
            block = await _resolve_about_member("ws1", "user-7")

        assert "Ada Lovelace" in block
        assert "Own the billing rewrite" in block
        assert "do not guess" not in block

    @pytest.mark.asyncio
    async def test_a_name_that_is_just_the_id_yields_no_block(self) -> None:
        """``who: 69f88339dc…`` tells the agent nothing the ``id:`` line does not.

        ``resolve_display_names`` falls back to the raw id when a user has
        neither a name nor an email, so this case is reachable.

        MUTATION: drop the ``display == user_id`` guard. Run: a block rendered
        with the id as the name and this failed. (Applied 2026-08-04.)
        """

        async def _no_person(workspace_id: str, user_id: str):
            return None

        async def _names(user_ids):
            return {"user-7": "user-7"}

        with (
            patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_no_person),
            patch("pocketpaw_ee.cloud.auth.service.resolve_display_names", side_effect=_names),
        ):
            assert await _resolve_about_member("ws1", "user-7") is None

    @pytest.mark.asyncio
    async def test_a_fabric_hiccup_degrades_to_the_user_record_not_to_silence(self) -> None:
        """A people-read failure used to cost the member their identity entirely.

        MUTATION: ``return None`` in the ``except`` around ``get_person``
        instead of falling through. Run: the block was None and this failed.
        (Applied 2026-08-04.)
        """

        async def _boom(workspace_id: str, user_id: str):
            raise RuntimeError("fabric exploded")

        async def _names(user_ids):
            return {"user-7": "Admin"}

        with (
            patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_boom),
            patch("pocketpaw_ee.cloud.auth.service.resolve_display_names", side_effect=_names),
        ):
            block = await _resolve_about_member("ws1", "user-7")

        assert block is not None and "Admin" in block

    @pytest.mark.asyncio
    async def test_both_sources_failing_is_still_no_block(self) -> None:
        """Degrade to silence only when the member truly cannot be identified.

        MUTATION: drop the try/except around ``resolve_display_names``. Run: the
        RuntimeError propagated out of scope resolution and sank the turn.
        (Applied 2026-08-04.)
        """

        async def _boom(*a, **k):
            raise RuntimeError("everything exploded")

        with (
            patch("pocketpaw_ee.cloud.people.service.get_person", side_effect=_boom),
            patch("pocketpaw_ee.cloud.auth.service.resolve_display_names", side_effect=_boom),
        ):
            assert await _resolve_about_member("ws1", "user-7") is None
