# tests/cloud/metering/test_sweeper_ledger.py — run counting rides billing's walk (AL-3).
#
# Created: 2026-08-01. Emitter #5 of the agent ledger, and the only one that sits
# inside the BILLING sweep rather than on a human's hot path. That placement is
# the whole design: the value board's run count and the wallet's spend come from
# the SAME pass over the SAME docs, so a run is counted if and only if it was
# billed. A second walk — a separate job, a completion hook — would be a second
# meter, and two meters over one quantity eventually disagree. We have paid for
# that once already (the usage chart read the proxy while the wallet held the
# spend, and they disagreed in public).
#
# What these tests hold, in the order the slice fails if any one is wrong:
#
#   * ONE ROW PER BILLED RUN. Not per terminal run — per BILLED run. The emit is
#     on the else branch, so a run the sweep failed to bill is not counted.
#   * IDEMPOTENT RE-SWEEP. ``ref`` is the run id, so UNIQUE(kind, ref) absorbs a
#     re-sweep at the database. This matters beyond tidiness: the metering tests
#     deliberately exercise a flag-loss path (``billed`` reset by a crash between
#     debit and save), and a ledger that double-counted there would inflate the
#     board every time billing recovered.
#   * NEVER COSTS THE BILLING. A raising ledger must not stop, skip, or unwind a
#     debit. Bookkeeping charging a workspace its billing is the one outcome this
#     design refuses outright.
#   * COUNT ONLY. No tokens, no cost, no latency on the row — those stay on the
#     run doc and are read federated by joining on the run id.
#
# The fail-soft test patches the seam the emitter ACTUALLY calls and asserts the
# NEGATIVE half (no row landed). AL-1 shipped a fail-soft test that patched a
# function the emitter no longer used; it passed while testing nothing. A
# fail-soft test that only asserts the happy half cannot tell "guarded" from
# "never attempted".

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.credits import service as credits  # noqa: E402
from pocketpaw_ee.cloud.metering.domain import RateCard  # noqa: E402
from pocketpaw_ee.cloud.metering.sweeper import sweep_unbilled_runs  # noqa: E402
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc  # noqa: E402

from pocketpaw.agent_ledger.models import KIND_RUN_COMPLETED  # noqa: E402
from pocketpaw.agent_ledger.store import AgentLedgerStore  # noqa: E402

pytestmark = pytest.mark.asyncio

WS = "ws_sweeper_ledger"
RATE = RateCard(markup=2.5, credit_usd=0.01)


async def _run(*, run_id: str, agent_id: str = "a1", context_type: str = "dm") -> ChatRunDoc:
    """One terminal, unbilled run with real usage the sweep will charge for."""
    doc = ChatRunDoc(
        run_id=run_id,
        workspace=WS,
        context_type=context_type,
        scope_id="scope-1",
        session_key="sk-1",
        user_id="u1",
        agent_id=agent_id,
        client_message_id=f"cmid-{run_id}",
        user_message_id=f"umid-{run_id}",
        status="completed",  # type: ignore[arg-type]
        usage={"total_cost_usd": 0.04, "model": "gpt-4o"},
        billed=False,
    )
    await doc.insert()
    return doc


@pytest.fixture
def ledger(tmp_path, monkeypatch) -> AgentLedgerStore:
    """A tmp ledger on the factory the sweeper's emitter lazy-imports.

    Records nothing else: the assertions read the store directly, because the
    failure this guards against (no row, silently) has no other symptom.
    """
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    monkeypatch.setattr("pocketpaw.stores.get_agent_ledger_store", lambda **_kw: store)
    return store


async def _seed_credits() -> None:
    await credits.grant(WS, 10_000, cause="top_up", idempotency_key=f"seed-{WS}")


class TestOneRowPerBilledRun:
    async def test_n_billed_runs_produce_exactly_n_rows(self, mongo_db, ledger) -> None:  # noqa: ARG002
        await _seed_credits()
        for i in range(3):
            await _run(run_id=f"run-{i}")

        billed = await sweep_unbilled_runs(rate_card=RATE, mode="shadow")

        rows = await ledger.query(kinds=[KIND_RUN_COMPLETED])
        assert billed == 3
        assert len(rows) == 3
        assert {r.ref for r in rows} == {"run-0", "run-1", "run-2"}
        assert all(r.agent_id == "a1" for r in rows)

    async def test_a_re_sweep_counts_nothing_twice(self, mongo_db, ledger) -> None:  # noqa: ARG002
        """UNIQUE(kind, ref) absorbs the replay at the DATABASE, not the caller.

        The metering suite deliberately exercises a flag-loss path — ``billed``
        reset by a crash between the debit and the save — so a re-sweep of an
        already-counted run is a real scenario, not a hypothetical.
        """
        await _seed_credits()
        await _run(run_id="run-replay")

        await sweep_unbilled_runs(rate_card=RATE, mode="shadow")
        # Force the flag-loss path: the run looks unbilled again.
        doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == "run-replay")
        assert doc is not None
        doc.billed = False
        await doc.save()
        await sweep_unbilled_runs(rate_card=RATE, mode="shadow")

        rows = await ledger.query(kinds=[KIND_RUN_COMPLETED])
        assert len(rows) == 1, "a re-swept run was counted twice"

    async def test_the_row_carries_no_ops_metric(self, mongo_db, ledger) -> None:  # noqa: ARG002
        """Tokens, cost and latency stay on the run doc. Two meters, one number."""
        await _seed_credits()
        await _run(run_id="run-ops")

        await sweep_unbilled_runs(rate_card=RATE, mode="shadow")

        row = (await ledger.query(kinds=[KIND_RUN_COMPLETED]))[0]
        assert row.value_cents is None
        assert not any(
            k in row.attrs for k in ("tokens", "cost", "latency", "gen_ai.usage", "total_cost_usd")
        )


class TestTheSweepNeverPaysForBookkeeping:
    async def test_a_raising_ledger_does_not_break_the_billing(
        self,
        mongo_db,  # noqa: ARG002
        tmp_path,
        monkeypatch,
    ) -> None:
        """The load-bearing guarantee: a broken ledger must not cost a debit.

        Patches the seam the emitter ACTUALLY calls, and asserts the negative
        half — no row landed — so this cannot rot into a test that passes because
        the exploding store was never reached.
        """
        await _seed_credits()
        await _run(run_id="run-boom")
        before = await credits.balance(WS)

        class _ExplodingStore:
            async def append(self, row):  # noqa: ARG002
                raise RuntimeError("ledger disk is on fire")

        monkeypatch.setattr(
            "pocketpaw.stores.get_agent_ledger_store", lambda **_kw: _ExplodingStore()
        )

        billed = await sweep_unbilled_runs(rate_card=RATE, mode="shadow")

        assert billed == 1, "the sweep skipped a run because bookkeeping failed"
        assert await credits.balance(WS) < before, "the run was not actually charged"
        reloaded = await ChatRunDoc.find_one(ChatRunDoc.run_id == "run-boom")
        assert reloaded is not None
        assert reloaded.billed is True, "the billed flag was not set"
        # The negative half: the exploding store really was reached.
        quiet = AgentLedgerStore(tmp_path / "agent_ledger.db")
        assert await quiet.query(kinds=[KIND_RUN_COMPLETED]) == []
