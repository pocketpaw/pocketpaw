# tests/cloud/chat/test_kiosk_byok_gate.py — the two seams give the SAME answer.
#
# Created 2026-09-13 (feat/kiosk-plan-gate). The kiosk BYOK rule is asked at two
# places on every turn: ``agent_router`` fast-rejects before the stream opens so
# the browser gets a clean 402, and ``run_core`` enforces from the server-side
# context. The router answers FIRST.
#
# While the rule was "flag on AND surface is kiosk" the duplication was
# harmless. Adding the plan term made it dangerous in one specific direction: a
# plan check in the executor alone leaves the router still refusing every
# keyless account, so a paying member is told to add a key by the seam that
# runs first and never reaches the seam that would have let them through. The
# symptom is "I upgraded and it still asks for a key", and no test of either
# file alone would have caught it — each would have been internally correct.
#
# So this file tests the AGREEMENT, not either implementation. It is the same
# shape as the ladder-drift and version-drift bugs found earlier the same day:
# one decision, two files, nothing comparing them.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.chat import kiosk_byok
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, SurfaceMeta

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_settings():
    """``get_settings`` is ``lru_cache``d — see the sibling kiosk test file."""
    from pocketpaw.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ctx(*, surface: SurfaceKind | None = SurfaceKind.OTHER_HAND, workspace: str = "w1"):
    return SimpleNamespace(
        workspace_id=workspace,
        user_id="u1",
        surface_context=(
            None
            if surface is None
            else SurfaceContext(
                workspace_id=workspace,
                user_id="u1",
                kind=surface,
                meta=SurfaceMeta(),
                preamble="",
                preamble_cache_key=None,
            )
        ),
    )


def _on_plan(monkeypatch, plan: str) -> None:
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def _plan(_ws):
        return plan

    monkeypatch.setattr(workspace_service, "get_workspace_plan", _plan)


# ── the agreement ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("plan", "expected"),
    [("free", True), ("go", False), ("pro", False), ("pro_max", False), ("enterprise", False)],
)
async def test_both_seams_answer_the_same_for_every_tier(monkeypatch, plan, expected):
    """``run_core._requires_own_key`` must be the shared predicate, not a copy.

    Mutation that must break this: give ``run_core`` its own plan check.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")
    _on_plan(monkeypatch, plan)
    ctx = _ctx()

    assert await kiosk_byok.requires_own_key(ctx) is expected
    assert await run_core._requires_own_key(ctx) is expected


# ── the rule's own edges ─────────────────────────────────────────────────


async def test_the_flag_still_switches_the_whole_thing_off(monkeypatch):
    """The default. Every deploy that has not set the variable, and the whole of
    Paw OS, must be untouched — including by the new plan read, which is a
    database call an ordinary turn should never pay for.
    """
    monkeypatch.delenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", raising=False)

    async def _must_not_run(_ws):
        raise AssertionError("resolved a plan with the gate switched off")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.entitlements.service.resolve_entitlements", _must_not_run
    )

    assert await kiosk_byok.requires_own_key(_ctx()) is False


async def test_a_non_kiosk_surface_is_never_gated(monkeypatch):
    """The same deployment serves the full Paw OS, where the platform fallback
    IS the product. Also asserts no plan read happens — the surface check must
    come first so a Paw OS turn pays one boolean.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")

    async def _must_not_run(_ws):
        raise AssertionError("resolved a plan for a non-kiosk surface")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.entitlements.service.resolve_entitlements", _must_not_run
    )

    assert await kiosk_byok.requires_own_key(_ctx(surface=SurfaceKind.SITES)) is False
    assert await kiosk_byok.requires_own_key(_ctx(surface=None)) is False


async def test_an_unresolvable_plan_asks_for_a_key(monkeypatch):
    """Fails CLOSED, unlike the daily budgets and the storage cap beside it.

    Those fail OPEN because refusing the product over one unreadable counter is
    worse than one workspace briefly exceeding a limit. Here the reverse holds:
    failing open hands an unbounded platform credential to anyone who can make
    a lookup fail, and the closed answer is not an outage — it is a key prompt
    the user can act on in ten seconds.

    Mutation that must break this: return False from the except branch.
    """
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")

    async def _boom(_ws):
        raise RuntimeError("mongo is having a day")

    monkeypatch.setattr("pocketpaw_ee.cloud.entitlements.service.resolve_entitlements", _boom)

    assert await kiosk_byok.requires_own_key(_ctx()) is True


async def test_no_workspace_asks_for_a_key(monkeypatch):
    """Same direction: nothing to resolve a plan for is not a paid plan."""
    monkeypatch.setenv("POCKETPAW_OTHER_HAND_REQUIRE_BYOK", "1")
    assert await kiosk_byok.requires_own_key(_ctx(workspace="")) is True
