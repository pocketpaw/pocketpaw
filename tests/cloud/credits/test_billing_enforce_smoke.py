# tests/cloud/credits/test_billing_enforce_smoke.py — LIVE flag-on smoke for the
# billing-enforcement wave. Unlike the unit tests (which mock over_billing_limit),
# this drives the REAL guard end-to-end: real credits.service.check_balance against
# a REAL (empty / funded / negative) wallet in Beanie, with the REAL flag flipped
# on via the guard's own get_settings. Only true infrastructure is stubbed (the
# group lookup, the realtime emit, the run transport) — never the billing seam.
#
# Proves the money guarantee the wave claims:
#   1. flag ON + empty wallet  -> the group/DM bridge (a NEW C1 seam) blocks with
#      NO model call (neither the Haiku relevance pre-classifier nor the agent run).
#   2. flag ON + funded wallet -> the bridge proceeds (no over-blocking).
#   3. flag OFF + empty wallet -> proceeds (the default-off safety: nothing changes
#      in prod until POCKETPAW_BILLING_ENFORCED is set).
#   4. flag ON + negative wallet -> the WORKER/executor path (run_core's delegate)
#      rejects with a terminal error frame — the balance-parity leg C1 added.
#
# Created 2026-07-08 (feat/billing-enforce-gate smoke, pre-Wave-2 gate).
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _flag(on: bool):
    """Patch the guard's own settings source so the REAL guard sees the flag."""
    return patch(
        "pocketpaw_ee.cloud.credits.guards.get_settings",
        new=lambda: SimpleNamespace(billing_enforced=on),
    )


def _one_agent_group() -> SimpleNamespace:
    return SimpleNamespace(
        members=["u1"],
        agents=[SimpleNamespace(agent_id="agent-a", respond_mode="smart")],
    )


async def test_smoke_bridge_blocks_empty_wallet_no_model_call(mongo_db):  # noqa: ARG001 — Beanie init
    """flag ON + REAL empty wallet -> the group/DM bridge blocks with no model call."""
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.shared import agent_bridge

    # ws "ws-empty" has NO wallet -> real balance() is 0 -> check_balance raises.
    assert await credits_service.balance("ws-empty") == 0

    should_spy = AsyncMock(return_value=True)
    run_spy = AsyncMock(return_value=None)
    emit_spy = AsyncMock()

    with (
        _flag(True),
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=_one_agent_group()),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond", new=should_spy),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_spy),
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=emit_spy),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "g1",
                "sender_id": "u1",
                "content": "hey team",
                "mentions": [],
                "workspace_id": "ws-empty",
            }
        )

    # No Haiku pre-classifier, no agent run — the real money guarantee.
    should_spy.assert_not_awaited()
    run_spy.assert_not_awaited()
    # One terminal billing error emitted to the group.
    assert emit_spy.await_count == 1
    assert emit_spy.await_args_list[0].args[0].data["code"] == "credits.insufficient"


async def test_smoke_bridge_proceeds_funded_wallet(mongo_db):  # noqa: ARG001 — Beanie init
    """flag ON + REAL funded wallet -> the bridge proceeds (no over-blocking)."""
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.shared import agent_bridge

    await credits_service.grant("ws-funded", 500, cause="smoke.seed", idempotency_key="smoke-f1")
    assert await credits_service.balance("ws-funded") == 500

    should_spy = AsyncMock(return_value=False)  # returns False so no agent actually runs
    run_spy = AsyncMock(return_value=None)

    with (
        _flag(True),
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=_one_agent_group()),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond", new=should_spy),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_spy),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "g1",
                "sender_id": "u1",
                "content": "hey team",
                "mentions": [],
                "workspace_id": "ws-funded",
            }
        )

    # The gate passed (real check_balance no-op + real check_quota under ceiling):
    # respond-mode evaluation was reached, so the bridge did NOT over-block.
    should_spy.assert_awaited()


async def test_smoke_flag_off_empty_wallet_proceeds(mongo_db):  # noqa: ARG001 — Beanie init
    """flag OFF + empty wallet -> proceeds. Default-off safety: inert until enabled."""
    from pocketpaw_ee.cloud.shared import agent_bridge

    should_spy = AsyncMock(return_value=False)

    with (
        _flag(False),
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=_one_agent_group()),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond", new=should_spy),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=AsyncMock()),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "g1",
                "sender_id": "u1",
                "content": "hey team",
                "mentions": [],
                "workspace_id": "ws-empty",  # empty wallet, but flag off
            }
        )

    # Gate is a no-op with the flag off — the bridge proceeds past it.
    should_spy.assert_awaited()


async def test_smoke_worker_path_rejects_negative_wallet(mongo_db):  # noqa: ARG001 — Beanie init
    """flag ON + REAL negative wallet -> the worker/executor guard rejects with a
    terminal error frame (the balance-parity leg C1 added to the worker path)."""
    from pocketpaw_ee.cloud.credits import guards
    from pocketpaw_ee.cloud.credits import service as credits_service

    # Drive the wallet negative through a real metered debit, like test_enforcement.
    await credits_service.grant("ws-neg", 10, cause="smoke.seed", idempotency_key="smoke-n1")
    await credits_service.debit(
        "ws-neg", 25, cause="smoke.debit", idempotency_key="smoke-n2", allow_negative=True
    )
    assert await credits_service.balance("ws-neg") == -15

    appended: list[tuple[str, dict]] = []

    class _SpyTransport:
        async def append_event(self, run_id: str, event: str, data: dict) -> None:  # noqa: ARG002
            appended.append((event, data))

        async def set_ttl(self, run_id: str, ttl: int) -> None:  # noqa: ARG002
            return None

    with (
        _flag(True),
        patch(
            "pocketpaw_ee.cloud.chat.runs.service.mark_terminal",
            new=AsyncMock(return_value=None),
        ),
    ):
        rejected = await guards.reject_if_over_billing(
            "ws-neg", run_id="run-smoke", transport=_SpyTransport(), log_label="smoke"
        )

    assert rejected is True
    assert appended and appended[0][0] == "error"
    assert appended[0][1]["code"] == "credits.insufficient"
