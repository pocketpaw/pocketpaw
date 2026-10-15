# tests/cloud/credits/test_early_access_run_bounds.py: each platform-funded
# free run is bounded. An unpaid early-access workspace has at most one active
# chat run (a second gets 429 ``credits.run_in_progress`` on both seams, the
# oldest run wins a race), and the run that does start carries the free-run
# bounds (turn cap plus the wallet it may spend) that force it onto pydantic_ai
# with ``UsageLimits``. A paid workspace and a BYOK run get none of this.
#
# Same harness as ``test_early_access_byok.py``: early-access posture, real
# ledger, real guard, real ChatRunDoc rows and Agent documents. Only scope
# resolution, the Redis stream and the agent event source are stubbed.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.cloud.credits.test_early_access_byok import (
    _agent,
    _post_turn,
    _spec,
    _store_key,
    _terminal,
    _wire_executor,
)
from tests.cloud.credits.test_early_access_cap import PRICED_MODEL, _rate_card

pytestmark = pytest.mark.asyncio


async def _active_run(ws: str, uid: str, run_id: str, *, age_s: int = 0) -> None:
    """A real queued ChatRunDoc for ``ws``, created ``age_s`` seconds ago."""
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    await run_service.create_run(_spec(ws, uid, run_id))
    if age_s:
        doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id)
        doc.createdAt = datetime.now(UTC) - timedelta(seconds=age_s)
        await doc.save()


async def _set_plan(ws: str, plan: str) -> None:
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.workspace import Workspace

    doc = await Workspace.get(PydanticObjectId(ws))
    doc.plan = plan
    await doc.save()


# ---------------------------------------------------------------------------
# One active run per unpaid workspace
# ---------------------------------------------------------------------------


async def test_second_concurrent_run_refused_on_chat_http(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    ws, uid = ea_user.workspace_id, ea_user.user_id
    await _active_run(ws, uid, "r-first")

    resp, submitted = await _post_turn(monkeypatch, ws, uid, await _agent(ws, uid))

    assert resp.status_code == 429, resp.text
    error = resp.json()["error"]
    assert error["code"] == "credits.run_in_progress"
    assert error["message"]
    assert submitted == []
    assert backend_factory_spy == []


async def test_second_concurrent_run_refused_on_execute_run(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    ws, uid = ea_user.workspace_id, ea_user.user_id
    await _active_run(ws, uid, "r-first", age_s=5)
    transport, seen = _wire_executor(monkeypatch, ws, uid, await _agent(ws, uid))

    spec = _spec(ws, uid, "r-second")
    await run_service.create_run(spec)
    await run_core.execute_run(spec)

    end = await _terminal(transport, spec.run_id)
    assert end.event == "error"
    assert end.data["code"] == "credits.run_in_progress"
    assert seen == []
    assert backend_factory_spy == []
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == spec.run_id)
    assert doc is not None and doc.status == "failed"


async def test_the_older_run_proceeds_while_a_newer_one_is_queued(
    ea_user, early_access_posture, monkeypatch
):
    """The executor's own run is active by the time it is gated, and a racing
    second tab may already have queued another. Neither may refuse the oldest
    run, or two tabs refuse each other and nothing runs.

    Mutation: count the run's own doc, or any other active run, as in progress.
    """
    from pocketpaw_ee.cloud.chat.runs import run_core

    ws, uid = ea_user.workspace_id, ea_user.user_id
    transport, seen = _wire_executor(monkeypatch, ws, uid, await _agent(ws, uid))
    spec = _spec(ws, uid, "r-older")
    await _active_run(ws, uid, spec.run_id, age_s=5)
    await _active_run(ws, uid, "r-newer")

    await run_core.execute_run(spec)

    end = await _terminal(transport, spec.run_id)
    assert end.event == "stream_end", end.data
    assert len(seen) == 1


async def test_other_billing_callers_are_not_refused_by_an_active_chat_run(
    ea_user, early_access_posture
):
    """Only the two chat seams ask for the one-run check. The planner, the
    group-chat bridge and the file routes keep the plain wallet gate."""
    from pocketpaw_ee.cloud.credits import guards

    ws, uid = ea_user.workspace_id, ea_user.user_id
    await _active_run(ws, uid, "r-first")

    assert await guards.over_billing_limit(ws) is None
    refused = await guards.over_billing_limit(ws, one_active_run=True)
    assert refused is not None and refused.code == "credits.run_in_progress"


# ---------------------------------------------------------------------------
# The run that starts carries the free-run bounds
# ---------------------------------------------------------------------------


async def test_free_run_carries_turn_cap_and_wallet(ea_user, early_access_posture, monkeypatch):
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.credits import service as credits_service

    ws, uid = ea_user.workspace_id, ea_user.user_id
    agent_id = await _agent(ws, uid, backend="claude_agent_sdk")
    transport, seen = _wire_executor(monkeypatch, ws, uid, agent_id)

    spec = _spec(ws, uid, "r-bounded")
    await run_service.create_run(spec)
    await run_core.execute_run(spec)

    end = await _terminal(transport, spec.run_id)
    assert end.event == "stream_end", end.data
    bounds = seen[0].free_run_bounds
    assert bounds is not None
    assert bounds.max_turns == early_access_posture.free_run_max_turns == 20
    assert bounds.balance_micro == await credits_service.balance_micro(ws)
    assert bounds.balance_micro > 0


@pytest.mark.parametrize("payer", ["paid_plan", "byok"])
async def test_paid_and_byok_runs_are_not_bounded(
    ea_user, early_access_posture, monkeypatch, payer
):
    """A paid workspace and a BYOK run get no turn cap, no forced backend and
    no one-run rule: an older active run does not refuse them.

    Mutation: apply the bounds without ``is_platform_funded_free`` or for
    ``source="byok"``.
    """
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    ws, uid = ea_user.workspace_id, ea_user.user_id
    if payer == "paid_plan":
        await _set_plan(ws, "go")
    else:
        await _store_key(monkeypatch, ws)
    await _active_run(ws, uid, "r-first", age_s=5)
    transport, seen = _wire_executor(monkeypatch, ws, uid, await _agent(ws, uid))

    spec = _spec(ws, uid, f"r-{payer}")
    await run_service.create_run(spec)
    await run_core.execute_run(spec)

    end = await _terminal(transport, spec.run_id)
    assert end.event == "stream_end", end.data
    assert getattr(seen[0], "free_run_bounds", None) is None


async def test_bounds_are_off_while_credits_are_not_enforced(ea_user, monkeypatch):
    """Before the launch flag flips nothing changes: no forced backend for
    every free workspace on the deployment."""
    from pocketpaw_ee.cloud.credits import guards

    from pocketpaw.config import get_settings

    settings = get_settings().model_copy(
        update={"credits_enforced": False, "billing_enforced": False}
    )
    monkeypatch.setattr(guards, "get_settings", lambda: settings)
    assert await guards.free_run_bounds(ea_user.workspace_id, "platform") is None


# ---------------------------------------------------------------------------
# Token limit sized from the wallet at the turn model's price
# ---------------------------------------------------------------------------


def test_token_limit_is_what_the_wallet_buys_at_the_output_price():
    from pocketpaw_ee.cloud.credits import guards
    from pocketpaw_ee.cloud.credits.domain import MICRO_PER_CREDIT

    from pocketpaw.usage_tracker import price_run

    card = _rate_card()
    balance_micro = 10 * MICRO_PER_CREDIT
    tokens = guards.free_run_token_limit(balance_micro, PRICED_MODEL, rate_card=card)

    assert tokens is not None and tokens > 0
    per_million = price_run(PRICED_MODEL, input_tokens=0, output_tokens=1_000_000, at=None)
    spend = card.to_micro_credits(per_million * tokens / 1_000_000)
    # The wallet covers the limit, and one more thousand tokens would not fit.
    assert spend <= balance_micro
    assert card.to_micro_credits(per_million * (tokens + 1000) / 1_000_000) > balance_micro


def test_token_limit_reads_through_a_provider_prefix():
    from pocketpaw_ee.cloud.credits import guards

    card = _rate_card()
    bare = guards.free_run_token_limit(5_000_000, PRICED_MODEL, rate_card=card)
    prefixed = guards.free_run_token_limit(5_000_000, f"litellm:{PRICED_MODEL}", rate_card=card)
    assert bare is not None and prefixed == bare


def test_token_limit_edges():
    from pocketpaw_ee.cloud.credits import guards

    card = _rate_card()
    assert guards.free_run_token_limit(0, PRICED_MODEL, rate_card=card) == 0
    assert guards.free_run_token_limit(-5, PRICED_MODEL, rate_card=card) == 0
    # Unpriced: no limit can be derived; the turn cap still binds and EA-4
    # refuses unpriced models outright.
    assert guards.free_run_token_limit(5_000_000, "no-such-model-xyz", rate_card=card) is None
