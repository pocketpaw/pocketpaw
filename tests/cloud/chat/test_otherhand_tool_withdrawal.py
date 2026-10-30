# tests/cloud/chat/test_otherhand_tool_withdrawal.py — a tool that would only
# refuse is not offered.
#
# Created 2026-09-11, from a live kiosk turn. A guest asked for a butterfly. The
# agent called ``illustrate``; the tool refused ("needs an account"); the agent
# called it again — SIXTEEN times across 27 seconds, ending in a single reply
# that apologised sixteen times in one paragraph.
#
# The refusal text already said "do not try again this turn", and the tool
# bridge RETURNS that text rather than raising, so nothing forces a stop. The
# wording was never going to be the fix: a weaker model simply keeps trying.
#
# So the tool is withdrawn before the run instead. These pin the withdrawal and,
# just as importantly, its narrowness — it costs a database read, and a version
# that asked on every surface would put that read in front of every turn in the
# product.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile


class _Ctx:
    """The two fields the resolver reads, and nothing else."""

    def __init__(self, kind):
        self.workspace_id = "ws-1"
        self.user_id = "u-1"
        self.surface_context = type("_SC", (), {"kind": kind, "meta": SurfaceMeta()})()


@pytest.fixture
def illustration_access(monkeypatch):
    """Control what the shared credential module answers."""
    from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds

    def _set(result):
        async def _resolve(_workspace_id, *, is_guest):
            return result

        monkeypatch.setattr(creds, "resolve", _resolve)

    return _set


@pytest.fixture(autouse=True)
def _not_a_guest(monkeypatch):
    from pocketpaw_ee.cloud.auth import guest_budget

    async def _load(_user_id):
        return None

    monkeypatch.setattr(guest_budget, "load_guest", _load)


BASE = resolve_profile(SurfaceKind.OTHER_HAND, SurfaceMeta())


@pytest.mark.asyncio
async def test_a_caller_who_cannot_illustrate_is_not_offered_the_tools(illustration_access):
    from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds

    illustration_access(creds.IllustrationRefusal(reason="nope", guest_gate=True))

    profile = await run_core._deny_tools_that_would_only_refuse(_Ctx(SurfaceKind.OTHER_HAND), BASE)

    assert "illustrate" in profile.deny_mcp_tool_ids
    # ``image_generate`` rides the same withdrawal: it is the other drawing tool
    # this surface allows, and a model blocked from one reaches for the other.
    assert "image_generate" in profile.deny_mcp_tool_ids


@pytest.mark.asyncio
async def test_the_surface_s_own_denies_survive(illustration_access):
    """Folded INTO the profile, not substituted for it. The pocket-creation
    denies are the non-negotiable ones on this surface and dropping them would
    let 'draw me a diagram' build a POCKET."""
    from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds

    illustration_access(creds.IllustrationRefusal(reason="nope"))

    profile = await run_core._deny_tools_that_would_only_refuse(_Ctx(SurfaceKind.OTHER_HAND), BASE)

    for tool_id in BASE.deny_mcp_tool_ids:
        assert tool_id in profile.deny_mcp_tool_ids


@pytest.mark.asyncio
async def test_a_caller_who_can_illustrate_keeps_the_tools(illustration_access):
    from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds

    illustration_access(creds.IllustrationGrant(api_key="k", byok=False))

    profile = await run_core._deny_tools_that_would_only_refuse(_Ctx(SurfaceKind.OTHER_HAND), BASE)

    assert "illustrate" not in profile.deny_mcp_tool_ids
    assert profile is BASE, "an allowed caller should not pay for a rebuilt profile"


@pytest.mark.asyncio
async def test_other_surfaces_are_never_asked(monkeypatch):
    """The narrowness assertion. Every other surface carries neither tool, so
    asking there would add a database read to every run in the product."""
    from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds

    asked: list[str | None] = []

    async def _record(workspace_id, *, is_guest):
        # Recorded rather than raised: the resolver swallows exceptions by
        # design (it fails open), so an AssertionError in here would be caught
        # and the test would pass whatever the code did.
        asked.append(workspace_id)
        return creds.IllustrationGrant(api_key="k", byok=False)

    monkeypatch.setattr(creds, "resolve", _record)

    base = resolve_profile(SurfaceKind.GENERIC, SurfaceMeta())
    result = await run_core._deny_tools_that_would_only_refuse(_Ctx(SurfaceKind.GENERIC), base)

    assert asked == [], "a non-drawing surface asked about illustration access"
    assert result is base


@pytest.mark.asyncio
async def test_a_failure_to_decide_leaves_the_surface_alone(monkeypatch):
    """Fails OPEN, unlike the spend gates. This is a token-and-latency
    optimisation standing in front of a tool that refuses correctly on its own,
    so a database blip must not cost the feature."""
    from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds

    async def _boom(*_a, **_k):
        raise RuntimeError("mongo is having a day")

    monkeypatch.setattr(creds, "resolve", _boom)

    assert (
        await run_core._deny_tools_that_would_only_refuse(_Ctx(SurfaceKind.OTHER_HAND), BASE)
        is BASE
    )
