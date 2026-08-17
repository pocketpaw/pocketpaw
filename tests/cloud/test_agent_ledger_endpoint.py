# tests/cloud/test_agent_ledger_endpoint.py — GET /agents/{id}/ledger (AL-1).
# Created: 2026-07-31. Covers the read side of the ledger spine:
#   * the payload shape the AL-5 value strip is built against;
#   * that every aggregate is computed over the SAME window and agrees with the
#     rows listed beneath it (the chart-vs-wallet lesson, asserted rather than
#     hoped for);
#   * that a bad window is a 422, not a silent fallback to the default;
#   * that value is reported per-currency and gets NO headline total when the
#     ledger holds more than one currency;
#   * that an agent with no rows returns a well-formed empty payload instead of
#     a broken grid;
#   * that the visibility gate runs BEFORE any read, so a private agent's track
#     record is not readable by another member.
#
# The router is mounted standalone with the auth/licence dependencies overridden
# and the agents service stubbed, so this exercises the endpoint's own logic
# without a Mongo round-trip. The ledger itself is real — the whole point is to
# prove the endpoint and the store agree.

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw import stores
from pocketpaw.agent_ledger.models import (
    KIND_ACTION_APPROVED,
    KIND_ACTION_OUTCOME,
    KIND_ACTION_REJECTED,
    KIND_VISITOR_ACTION,
    SURFACE_PAW_BAR,
    LedgerActor,
    LedgerRow,
)
from pocketpaw.instinct.models import OutcomeStatus

_WS = "ws1"
_AGENT = "agent-42"


def _row(**overrides) -> LedgerRow:
    base = dict(
        agent_id=_AGENT,
        workspace_id=_WS,
        surface=SURFACE_PAW_BAR,
        kind=KIND_ACTION_APPROVED,
        ref="act_1",
        actor=LedgerActor.OWNER.value,
    )
    base.update(overrides)
    return LedgerRow(**base)


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """A standalone app around the agents router, on an isolated ledger file."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.router import router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    stores.reset_store_caches()

    async def _allow(agent_id, workspace_id, user_id):  # noqa: ARG001
        return None

    monkeypatch.setattr(agents_service, "ensure_can_read", _allow)

    app = FastAPI()
    # The real app installs this; without it a domain error (NotFound) escapes
    # as an unhandled exception instead of the 404 the client would really get.
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: _WS
    app.dependency_overrides[current_user_id] = lambda: "user:maya"

    ledger = stores.get_agent_ledger_store(workspace_id=_WS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, ledger
    # Deliberately NO reset_store_caches() here: eviction schedules an async
    # WAL checkpoint on the loop that is about to close, which surfaces as
    # "Event loop is closed" thread noise. The setup-side reset above (which
    # runs on a live loop) is what actually keeps tests isolated.


@pytest.mark.asyncio
async def test_ledger_payload_reports_the_full_track_record(client):
    c, ledger = client
    await ledger.append(_row(ref="a1", kind=KIND_ACTION_APPROVED))
    await ledger.append(_row(ref="a2", kind=KIND_ACTION_APPROVED))
    await ledger.append(_row(ref="a3", kind=KIND_ACTION_REJECTED))
    await ledger.append(
        _row(ref="a4", kind=KIND_ACTION_OUTCOME, outcome=OutcomeStatus.SOLVED.value)
    )
    await ledger.append(
        _row(ref="a5", kind=KIND_ACTION_OUTCOME, outcome=OutcomeStatus.NOT_SOLVED.value)
    )
    await ledger.append(
        _row(
            ref="a6",
            kind=KIND_VISITOR_ACTION,
            value_cents=4200,
            currency="USD",
            actor=LedgerActor.VISITOR.value,
        )
    )

    res = await c.get(f"/agents/{_AGENT}/ledger?window=30d")
    assert res.status_code == 200
    body = res.json()

    assert body["agent_id"] == _AGENT
    assert body["window"] == "30d"
    assert body["counts_by_kind"] == {
        KIND_ACTION_APPROVED: 2,
        KIND_ACTION_REJECTED: 1,
        KIND_ACTION_OUTCOME: 2,
        KIND_VISITOR_ACTION: 1,
    }
    assert body["total_events"] == 6

    # The ratio ships with its denominator: "50% solved" out of two is a very
    # different claim from "50% solved" out of two hundred.
    assert body["outcome"]["decided"] == 2
    assert body["outcome"]["counts"] == {
        OutcomeStatus.SOLVED.value: 1,
        OutcomeStatus.NOT_SOLVED.value: 1,
    }
    assert body["outcome"]["ratio"] == {
        OutcomeStatus.SOLVED.value: 0.5,
        OutcomeStatus.NOT_SOLVED.value: 0.5,
    }

    assert body["value"] == {"by_currency": {"USD": 4200}, "currency": "USD", "total_cents": 4200}

    # The rows agree with the aggregates above them — same filter, one source.
    assert len(body["recent"]) == 6
    assert sum(1 for r in body["recent"] if r["kind"] == KIND_ACTION_APPROVED) == 2
    # Ops metrics stay federated: nothing here leaks a token or cost field.
    assert all(
        not any(k in r["attrs"] for k in ("tokens", "cost", "latency")) for r in body["recent"]
    )


@pytest.mark.asyncio
async def test_mixed_currencies_get_no_headline_total(client):
    """Summing cents and pence produces a number that is wrong invisibly."""
    c, ledger = client
    await ledger.append(_row(ref="v1", kind=KIND_VISITOR_ACTION, value_cents=100, currency="USD"))
    await ledger.append(_row(ref="v2", kind=KIND_VISITOR_ACTION, value_cents=200, currency="GBP"))

    body = (await c.get(f"/agents/{_AGENT}/ledger")).json()
    assert body["value"]["by_currency"] == {"USD": 100, "GBP": 200}
    assert body["value"]["currency"] == ""
    assert body["value"]["total_cents"] == 0


@pytest.mark.asyncio
async def test_window_filters_and_all_is_unbounded(client):
    c, ledger = client
    await ledger.append(_row(ref="ancient", ts="2020-01-01T00:00:00+00:00"))
    await ledger.append(_row(ref="fresh", kind=KIND_ACTION_REJECTED))

    recent = (await c.get(f"/agents/{_AGENT}/ledger?window=7d")).json()
    assert recent["total_events"] == 1
    assert [r["ref"] for r in recent["recent"]] == ["fresh"]

    everything = (await c.get(f"/agents/{_AGENT}/ledger?window=all")).json()
    assert everything["total_events"] == 2


@pytest.mark.parametrize("window", ["banana", "30", "0d", "9999d", ""])
@pytest.mark.asyncio
async def test_a_bad_window_is_a_422_not_a_silent_default(client, window):
    c, _ledger = client
    res = await c.get(f"/agents/{_AGENT}/ledger?window={window}")
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_an_agent_with_no_rows_returns_a_clean_empty_payload(client):
    """A brand-new agent must render a teaching empty state, not a broken grid."""
    c, _ledger = client
    body = (await c.get("/agents/agent-brand-new/ledger")).json()
    assert body["counts_by_kind"] == {}
    assert body["total_events"] == 0
    assert body["outcome"] == {"counts": {}, "decided": 0, "ratio": {}}
    assert body["value"] == {"by_currency": {}, "currency": "", "total_cents": 0}
    assert body["recent"] == []


@pytest.mark.asyncio
async def test_another_workspaces_rows_are_never_returned(client):
    """In-row tenancy is the second layer under the per-workspace file."""
    c, ledger = client
    await ledger.append(_row(ref="mine", workspace_id=_WS))
    await ledger.append(_row(ref="theirs", workspace_id="ws-other"))

    body = (await c.get(f"/agents/{_AGENT}/ledger?window=all")).json()
    assert [r["ref"] for r in body["recent"]] == ["mine"]


@pytest.mark.asyncio
async def test_the_visibility_gate_runs_before_any_read(client, monkeypatch):
    """A private agent's track record is as protected as its config."""
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.shared.errors import NotFound

    c, ledger = client
    await ledger.append(_row(ref="secret"))

    async def _deny(agent_id, workspace_id, user_id):  # noqa: ARG001
        raise NotFound("agent", agent_id)

    monkeypatch.setattr(agents_service, "ensure_can_read", _deny)
    res = await c.get(f"/agents/{_AGENT}/ledger")
    assert res.status_code in (403, 404, 500)
    assert "secret" not in res.text


# --------------------------------------------------------------------------- #
# AL-4 — the reconcile drift alarm
#
# Every emitter in this design is fail-soft, so a broken one is SILENT: it looks
# exactly like a quiet week. This endpoint is what makes fail-soft an acceptable
# trade rather than a slow leak, which means the tests that matter here are the
# ones proving the alarm actually FIRES — a reconcile that always says "healthy"
# is worse than none, because it is trusted.
# --------------------------------------------------------------------------- #


async def _approve_one(*, ref: str, customer_reply: bool = False) -> str:
    """Approve one real Instinct action in _WS. Returns its id.

    Goes through the real store rather than a fake so the comparison is against
    the same rows the dashboard's own feeds read — a reconcile validated against
    a stub proves the stub agrees with itself.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionTrigger

    instinct = stores.get_instinct_store(workspace_id=_WS)
    params = {"_customer_reply": {"widget_id": "w1"}} if customer_reply else {}
    action = await instinct.propose(
        pocket_id="pocket-1",
        title=f"ask {ref}",
        description="",
        recommendation="ok",
        trigger=ActionTrigger(type="connector", source="paw_bar:w1", reason="test"),
        category=ActionCategory.EXTERNAL,
        parameters=params,
        workspace_id=_WS,
    )
    await instinct.approve(action.id, approver="user:maya")
    return action.id


@pytest.mark.asyncio
async def test_reconcile_is_healthy_when_every_emitter_kept_up(client):
    """The baseline: sources and ledger agree, so nothing is drifting."""
    c, ledger = client
    act = await _approve_one(ref="r1")
    # AL-1's emitter already wrote the approved row through the real path; the
    # delivered row is AL-2's, emitted on a different path, so write it here.
    await ledger.append(_row(ref=act, kind=KIND_ACTION_APPROVED))

    res = await c.get("/agents/analytics/reconcile?window=30d")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["checks"]["approved"]["source"] == 1
    assert body["checks"]["approved"]["ledger"] == 1
    assert body["checks"]["approved"]["delta"] == 0
    assert body["healthy"] is True
    assert body["drifting"] == []


@pytest.mark.asyncio
async def test_a_silently_failing_emitter_shows_up_as_drift_and_is_NAMED(client):
    """The alarm's whole reason to exist.

    An approval happened and no ledger row followed — exactly what a broken
    fail-soft emitter looks like from the outside. The response must not merely
    say "unhealthy": it must name WHICH source is behind, or the owner is left
    diffing two systems by hand.
    """
    c, ledger = client

    # Simulate the failure FAITHFULLY: break the emitter's store seam so the
    # approvals happen and no rows follow. Deleting rows after the fact would
    # test the same arithmetic while proving nothing about the real path — this
    # is the actual shape of a silently-failing fail-soft emitter.
    class _ExplodingStore:
        async def append(self, row):  # noqa: ARG002
            raise RuntimeError("ledger disk is on fire")

    original = stores.get_agent_ledger_store_beside
    stores.get_agent_ledger_store_beside = lambda _p: _ExplodingStore()  # type: ignore[assignment]
    try:
        await _approve_one(ref="r1")
        await _approve_one(ref="r2")
    finally:
        stores.get_agent_ledger_store_beside = original  # type: ignore[assignment]

    assert await ledger.query(kinds=[KIND_ACTION_APPROVED]) == [], (
        "the emitter still wrote — this test is not simulating the failure"
    )

    res = await c.get("/agents/analytics/reconcile?window=30d")

    body = res.json()
    assert body["checks"]["approved"]["source"] == 2
    assert body["checks"]["approved"]["delta"] > 0, "drift was not detected"
    assert body["checks"]["approved"]["behind"] == "approved", "the alarm did not name the source"
    assert body["healthy"] is False
    assert "approved" in body["drifting"]


@pytest.mark.asyncio
async def test_delivered_only_counts_approvals_that_carry_a_customer_reply(client):
    """An always-ringing alarm is an alarm nobody hears.

    Only paw-bar decisions produce a delivered row. Comparing EVERY approval
    against deliveries would report a permanent deficit for every ordinary agent
    in the workspace and train the owner to ignore the endpoint.
    """
    c, _ledger = client
    await _approve_one(ref="plain")  # an ordinary approval — no delivery expected
    await _approve_one(ref="bar", customer_reply=True)

    body = (await c.get("/agents/analytics/reconcile?window=30d")).json()

    assert body["checks"]["approved"]["source"] == 2
    assert body["checks"]["delivered"]["source"] == 1, "a non-paw-bar approval was counted"


@pytest.mark.asyncio
async def test_a_hostile_window_is_a_refusal_not_a_server_error(client):
    """422, never 500 — including the over-large case that once escaped as one."""
    c, _ledger = client
    for window in ("nonsense", "0d", "99999999999d"):
        res = await c.get(f"/agents/analytics/reconcile?window={window}")
        assert res.status_code == 422, f"{window} → {res.status_code}"
