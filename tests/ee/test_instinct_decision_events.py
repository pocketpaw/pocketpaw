# tests/ee/test_instinct_decision_events.py
# Created: 2026-05-26 (RFC 09 Slice 3 — feat/rfc-09-slice-3-instinct-emits)
#
# Pins the chain-forming event emits Instinct adds in Slice 3 plus the
# touch-time cross-workspace reject security fix on
# ``reject_action`` / ``bulk_reject_actions``. Covers:
#
#   1. ``policy.evaluated(passed=False, reason="parked_for_human_approval")``
#      fires from ``instinct_bridge.propose_pocket_write`` after
#      ``store.propose`` succeeds; the persisted blob's
#      ``parked_policy_event_id`` field is populated with the emitted
#      event id so the eventual ``human.corrected`` can chain its
#      ``causation_id`` back to the policy event.
#   2. ``approve_action`` emits ``human.corrected(disposition=accepted)``
#      with the right actor + causation; the bridge owns the chain
#      close so no second ``decision.completed`` fires from the router.
#   3. ``reject_action`` emits ``human.corrected(disposition=rejected)``
#      + ``decision.completed(passed=False, action_outcome="rejected")``.
#   4. ``bulk_approve_actions`` fires per-item ``human.corrected``
#      emits matching the loop the bridge runs.
#   5. ``bulk_reject_actions`` fires per-item ``human.corrected`` +
#      ``decision.completed(rejected)`` emits AND closes the cross-
#      workspace reject security gap (mirror of bulk-approve BLOCKER 1).
#   6. Causation chain wired end-to-end —
#      ``policy.evaluated.id == human.corrected.causation_id`` for an
#      approve flow rooted at ``agent.proposed``.
#   7. ``reject_action`` cross-workspace 403 — Forbidden + no chain
#      events emitted.
#   8. ``reject_action`` same-workspace happy path under tenant
#      isolation.
#   9. End-to-end happy path via ``DecisionGraph.find`` —
#      parked → approved chain folds into a Decision row with
#      approvers populated, ``instinct_policy_passed=False`` (last-seen
#      policy event; the approve path does NOT emit a follow-up
#      ``policy.evaluated(passed=True)`` per the Slice 3 brief), and
#      ``outcome`` either None or non-rejected.
#
# Fixture strategy mirrors ``test_pocket_runtime_decision_events.py``
# (journal + DecisionGraph wired into the lazy global lookup, fresh
# tmp_path per test) AND ``test_instinct_approval_security.py``
# (router-level TestClient with stubbed auth deps + a
# ``CloudError``-aware app so a ``Forbidden`` maps to 403). The two
# pieces compose: the router writes via the journal+projection set up
# in the autouse fixture; the assertions read events out of the
# journal and Decisions out of the graph store.

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.pockets import instinct_bridge as instinct_bridge_module  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

TRIGGER = {"type": "agent", "source": "claude", "reason": "slice 3 test"}


# ---------------------------------------------------------------------------
# Fixtures — journal + projection wiring (same shape as Slice 2 tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    """Fresh on-disk journal wired into the lazy
    ``pocketpaw.journal_dep.get_journal`` lookup. The
    ``journal_writer.record_decision_event`` helper resolves the journal
    through that dep so the same singleton drives both production code
    and tests."""
    j = open_journal(tmp_path / "journal.db")
    journal_dep.reset_journal_cache()
    original = journal_dep._cached_journal

    def _stub() -> object:
        return j

    journal_dep._cached_journal = _stub  # type: ignore[assignment]
    yield j
    journal_dep._cached_journal = original  # type: ignore[assignment]
    journal_dep.reset_journal_cache()
    j.close()


@pytest.fixture
def graph(tmp_path: Path) -> DecisionGraph:
    """Fresh DecisionGraph + decisions.db plumbed in as the process-
    global singleton. ``reset_projection_for_tests`` clears the prior
    singleton so this test's set_db_path takes effect."""
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    """Fresh Instinct SQLite store per test. The router lazy-imports
    ``_store`` through ``get_instinct_store``; tests patch the indirection
    so propose and approve / reject share state."""
    return InstinctStore(tmp_path / "instinct.db")


# ---------------------------------------------------------------------------
# Fixtures — router-level TestClient with seeded auth + CloudError handler
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Duck-typed user. ``id`` is the authenticated identity; the audit
    actor + chain-event actor resolve to THIS, not request-body fields."""

    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _make_client(
    router_store: InstinctStore,
    user: _FakeUser,
    monkeypatch,
) -> TestClient:
    """Build a TestClient over the Instinct router with auth deps
    stubbed and a ``CloudError`` handler registered so a ``Forbidden``
    maps to 403 (not a 500). Same pattern as the security test file."""
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app)


def _pocket_write_params(
    *,
    workspace_id: str,
    correlation_id: UUID | None = None,
    parked_policy_event_id: str | None = None,
    outcome: str | None = "renewal_completed",
) -> dict[str, Any]:
    """Build a schema-2 parked pocket-write blob.

    Tests that need a stable ``correlation_id`` to query the journal /
    graph by chain id pass one; ``None`` lets the bridge / propose path
    leave it None (those tests usually drive ``propose_pocket_write``
    directly and supply the id via the ``_park`` shape instead).
    """
    return {
        "_pocket_write": {
            "schema": 2,
            "action": "mark_renewed",
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {"rent": 2000},
            "idempotency_key": "idem-xyz",
            "outcome": outcome,
            "workspace_id": workspace_id,
            "requested_by": "requester-9",
            "correlation_id": str(correlation_id) if correlation_id else None,
            "parked_policy_event_id": parked_policy_event_id,
        }
    }


def _propose(
    client: TestClient,
    *,
    pocket_id: str,
    title: str,
    parameters: dict[str, Any] | None = None,
) -> str:
    """Seed a pending Action over HTTP and return its id."""
    payload: dict[str, Any] = {"pocket_id": pocket_id, "title": title, "trigger": TRIGGER}
    if parameters is not None:
        payload["parameters"] = parameters
    resp = client.post("/instinct/actions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _events(journal, action: str) -> list:
    return [e for e in journal.replay_from(0) if e.action == action]


def _events_by_correlation(journal, correlation_id: UUID) -> list:
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


def _seed_pocket(workspace_id: str = "ws-A", pocket_id: str = "pocket-A") -> dict[str, Any]:
    return {
        "_id": pocket_id,
        "workspace": workspace_id,
        "owner": "user-A",
        "name": f"Pocket {pocket_id}",
    }


# ---------------------------------------------------------------------------
# 1. propose_pocket_write — policy.evaluated emit + parked_policy_event_id wire
# ---------------------------------------------------------------------------


async def test_propose_emits_policy_evaluated_and_persists_event_id(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """RFC 09 Slice 3 Producer 2 (a) — after ``store.propose`` succeeds,
    the bridge emits ``policy.evaluated(passed=False, reason=
    "parked_for_human_approval")`` and writes the emitted event id back
    onto the parked blob's ``parked_policy_event_id`` field so the
    eventual ``human.corrected`` can chain its ``causation_id`` back."""
    corr = uuid4()
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: router_store)

    park_blob = {
        "action": "mark_renewed",
        "method": "POST",
        "path": "/leases/42/renew",
        "params": {"rent": 2000},
        "idempotency_key": "idem-xyz",
        "outcome": "renewal_completed",
        "correlation_id": str(corr),
    }
    action_id = await instinct_bridge_module.propose_pocket_write(
        pocket=_seed_pocket("w1", "p1"),
        backend_config=None,
        parked_write=park_blob,
        requested_by="u_alice",
    )

    # Exactly one policy.evaluated event landed under this correlation
    # with the right payload shape.
    policy_events = _events(journal, "policy.evaluated")
    assert len(policy_events) == 1
    pe = policy_events[0]
    assert pe.correlation_id == corr
    payload = pe.payload or {}
    assert payload["passed"] is False
    assert payload["reason"] == "parked_for_human_approval"
    assert payload["action_id"] == action_id
    # System-side emit — actor is the Instinct subsystem, not the agent.
    assert pe.actor.kind == "system"
    assert "workspace:w1" in pe.scope
    assert "pocket:p1" in pe.scope

    # The parked blob persisted back to the Instinct store carries the
    # event id under parked_policy_event_id (Slice 3 wire).
    stored = await router_store.get_action(action_id)
    assert stored is not None
    blob = stored.parameters.get("_pocket_write")
    assert isinstance(blob, dict)
    assert blob["parked_policy_event_id"] == str(pe.id)
    # The correlation_id round-trips intact.
    assert blob["correlation_id"] == str(corr)


async def test_propose_without_correlation_id_skips_policy_emit(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """A parked blob with no correlation_id (a future code path that
    parks without minting one) skips the policy.evaluated emit — the
    Slice 4 abandon sweeper will eventually close the chain. The
    propose call still succeeds."""
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: router_store)

    park_blob = {
        "action": "mark_renewed",
        "method": "POST",
        "path": "/leases/42/renew",
        "params": {},
        "idempotency_key": None,
        "outcome": None,
        # Slice 2 always populates this; defensive coverage for the
        # missing-id case.
        "correlation_id": None,
    }
    action_id = await instinct_bridge_module.propose_pocket_write(
        pocket=_seed_pocket("w1", "p1"),
        backend_config=None,
        parked_write=park_blob,
        requested_by="u_alice",
    )

    # No policy.evaluated landed; the Action is still durably stored.
    assert _events(journal, "policy.evaluated") == []
    stored = await router_store.get_action(action_id)
    assert stored is not None
    blob = stored.parameters.get("_pocket_write")
    assert isinstance(blob, dict)
    assert blob["parked_policy_event_id"] is None


# ---------------------------------------------------------------------------
# 2. approve_action — human.corrected emit, no double decision.completed
# ---------------------------------------------------------------------------


async def test_approve_emits_human_corrected_and_does_not_double_emit_completed(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """The approve path emits exactly one ``human.corrected``. The bridge
    owns the chain close (its ``execute_approved_write`` emits
    ``decision.completed`` from ``_emit_bridge_chain_close``) — the
    router MUST NOT emit a second ``decision.completed`` itself. We
    stub the bridge so the captain's note about which emit owns the
    close is what's exercised."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(router_store, user, monkeypatch)

    # Stub out the bridge execution — we only care about what the
    # router itself emits on this test. A `_noop_execute` covers the
    # post-approval re-entry without firing the bridge's own emit.
    async def _noop_execute(_action):
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _noop_execute,
    )

    corr = uuid4()
    parked_policy_event_id = str(uuid4())
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_id = _propose(
            client,
            pocket_id="pocket-A",
            title="ws-A approve",
            parameters=_pocket_write_params(
                workspace_id="ws-A",
                correlation_id=corr,
                parked_policy_event_id=parked_policy_event_id,
            ),
        )
        resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert resp.status_code == 200, resp.text

    # Exactly one human.corrected event landed on the approve, with
    # disposition=accepted (no edits) and the user as actor.
    hc = _events(journal, "human.corrected")
    assert len(hc) == 1
    assert hc[0].correlation_id == corr
    payload = hc[0].payload or {}
    assert payload["disposition"] == "accepted"
    assert payload["action_id"] == action_id
    assert hc[0].actor.kind == "user"
    assert hc[0].actor.id == "user:user-A"
    # causation_id chains back to the parked policy event id.
    assert hc[0].causation_id == UUID(parked_policy_event_id)

    # The router does NOT emit decision.completed (bridge would).
    assert _events(journal, "decision.completed") == []


async def test_approve_with_edits_emits_human_corrected_disposition_edited(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """When the approver edits the proposal before approving, the
    chain event records ``disposition=edited`` and the correction's
    context summary lands on the ``note`` field."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(router_store, user, monkeypatch)

    async def _noop_execute(_action):
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _noop_execute,
    )

    corr = uuid4()
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_id = _propose(
            client,
            pocket_id="pocket-A",
            title="ws-A edit-approve",
            parameters=_pocket_write_params(workspace_id="ws-A", correlation_id=corr),
        )
        # Edit the title on approve; the router computes a Correction.
        resp = client.post(
            f"/instinct/actions/{action_id}/approve",
            json={"title": "ws-A edit-approve (revised)"},
        )
        assert resp.status_code == 200, resp.text

    hc = _events(journal, "human.corrected")
    assert len(hc) == 1
    payload = hc[0].payload or {}
    assert payload["disposition"] == "edited"
    assert "note" in payload  # the context_summary lands here


# ---------------------------------------------------------------------------
# 3. reject_action — human.corrected + decision.completed(rejected)
# ---------------------------------------------------------------------------


async def test_reject_emits_human_corrected_then_decision_completed_rejected(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """The reject path emits both the human action and the chain
    terminal — the bridge is never invoked on reject so the router
    owns the close. Order matters for the narrator: the
    ``human.corrected`` event lands before the
    ``decision.completed`` event."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(router_store, user, monkeypatch)
    corr = uuid4()
    parked_policy_event_id = str(uuid4())

    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_id = _propose(
            client,
            pocket_id="pocket-A",
            title="ws-A reject",
            parameters=_pocket_write_params(
                workspace_id="ws-A",
                correlation_id=corr,
                parked_policy_event_id=parked_policy_event_id,
            ),
        )
        resp = client.post(
            f"/instinct/actions/{action_id}/reject",
            json={"reason": "too risky"},
        )
        assert resp.status_code == 200, resp.text

    # Both events landed under this correlation, in order.
    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    assert actions == ["human.corrected", "decision.completed"], actions

    hc = chain[0]
    assert (hc.payload or {})["disposition"] == "rejected"
    assert (hc.payload or {}).get("note") == "too risky"
    assert hc.causation_id == UUID(parked_policy_event_id)
    assert hc.actor.kind == "user"
    assert hc.actor.id == "user:user-A"

    dc = chain[1]
    assert (dc.payload or {})["passed"] is False
    assert (dc.payload or {})["action_outcome"] == "rejected"
    assert (dc.payload or {})["reason"] == "too risky"


# ---------------------------------------------------------------------------
# 4. bulk_approve_actions — per-item human.corrected
# ---------------------------------------------------------------------------


async def test_bulk_approve_emits_human_corrected_per_item(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """The bulk-approve endpoint loops over the approved Actions and
    emits ``human.corrected(disposition=accepted)`` per item with the
    right correlation_id per item. The bridge is stubbed so this test
    isolates the router-side emit count."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(router_store, user, monkeypatch)

    async def _noop_execute(_action):
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _noop_execute,
    )

    corrs = [uuid4(), uuid4(), uuid4()]
    parked_policy_event_ids = [str(uuid4()) for _ in corrs]
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_ids = []
        for idx, corr in enumerate(corrs):
            aid = _propose(
                client,
                pocket_id="pocket-A",
                title=f"bulk approve #{idx}",
                parameters=_pocket_write_params(
                    workspace_id="ws-A",
                    correlation_id=corr,
                    parked_policy_event_id=parked_policy_event_ids[idx],
                ),
            )
            action_ids.append(aid)

        resp = client.post(
            "/instinct/actions/bulk-approve",
            json={"ids": action_ids, "note": "ship it"},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["affected"]) == 3

    # Exactly 3 human.corrected events, one per item, each with the
    # matching correlation_id and causation_id.
    hc = _events(journal, "human.corrected")
    assert len(hc) == 3

    by_corr = {e.correlation_id: e for e in hc}
    for idx, corr in enumerate(corrs):
        assert corr in by_corr, f"missing human.corrected for {corr}"
        ev = by_corr[corr]
        payload = ev.payload or {}
        assert payload["disposition"] == "accepted"
        assert payload.get("note") == "ship it"
        assert ev.causation_id == UUID(parked_policy_event_ids[idx])

    # Bridge owns the close — no decision.completed from the router.
    assert _events(journal, "decision.completed") == []


# ---------------------------------------------------------------------------
# 5. bulk_reject_actions — per-item human.corrected + decision.completed
# ---------------------------------------------------------------------------


async def test_bulk_reject_emits_human_corrected_and_completed_per_item(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """The bulk-reject endpoint adds a per-item emit loop in Slice 3
    (the underlying ``store.bulk_reject`` already iterated per item
    for audit; the router now also iterates for chain emits). Three
    rejected items produce three human.corrected + three
    decision.completed(rejected) events."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(router_store, user, monkeypatch)

    corrs = [uuid4(), uuid4(), uuid4()]
    parked_policy_event_ids = [str(uuid4()) for _ in corrs]
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        action_ids = []
        for idx, corr in enumerate(corrs):
            aid = _propose(
                client,
                pocket_id="pocket-A",
                title=f"bulk reject #{idx}",
                parameters=_pocket_write_params(
                    workspace_id="ws-A",
                    correlation_id=corr,
                    parked_policy_event_id=parked_policy_event_ids[idx],
                ),
            )
            action_ids.append(aid)

        resp = client.post(
            "/instinct/actions/bulk-reject",
            json={"ids": action_ids, "reason": "scope drift"},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["affected"]) == 3

    hc = _events(journal, "human.corrected")
    dc = _events(journal, "decision.completed")
    assert len(hc) == 3
    assert len(dc) == 3
    for ev in hc:
        payload = ev.payload or {}
        assert payload["disposition"] == "rejected"
        assert payload.get("note") == "scope drift"
    for ev in dc:
        payload = ev.payload or {}
        assert payload["passed"] is False
        assert payload["action_outcome"] == "rejected"
        assert payload["reason"] == "scope drift"


# ---------------------------------------------------------------------------
# 6. End-to-end causation chain — propose → approve threads cause-arrows
# ---------------------------------------------------------------------------


async def test_full_chain_causation_wires_policy_to_human(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """Exercise propose → approve and assert the full causation walk:
    ``policy.evaluated(parked, passed=False) → human.corrected →
    policy.evaluated(approve_per_row, passed=True)`` — Slice 4 (c)
    follow-up adds the third event so ``Decision.instinct_policy_passed``
    flips True on approved chains (chain symmetry, Captain Decision 12).
    The bridge is stubbed so the chain only contains the router-side +
    propose-side emits this test cares about."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(router_store, user, monkeypatch)
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: router_store)

    async def _noop_execute(_action):
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _noop_execute,
    )

    corr = uuid4()
    # Drive propose through the bridge (not the router) so the
    # policy.evaluated emit fires and parked_policy_event_id is wired
    # onto the persisted blob — exactly the production code path the
    # chat agent's instinct-pending flow takes.
    park_blob = {
        "action": "mark_renewed",
        "method": "POST",
        "path": "/leases/42/renew",
        "params": {"rent": 2000},
        "idempotency_key": "idem-xyz",
        "outcome": "renewal_completed",
        "correlation_id": str(corr),
    }
    action_id = await instinct_bridge_module.propose_pocket_write(
        pocket=_seed_pocket("ws-A", "pocket-A"),
        backend_config=None,
        parked_write=park_blob,
        requested_by="user-A",
    )

    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert resp.status_code == 200, resp.text

    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    # Slice 4 adds the approve-side policy.evaluated emit; the chain
    # now reads policy(parked, fail) → human → policy(approve_per_row,
    # pass).
    assert actions == [
        "policy.evaluated",
        "human.corrected",
        "policy.evaluated",
    ], actions

    parked_policy_event = chain[0]
    human_event = chain[1]
    approve_policy_event = chain[2]
    # human.corrected chains back to the parked policy.evaluated
    # (causation_id from the schema-2 _pocket_write blob).
    assert human_event.causation_id == parked_policy_event.id
    # The approve-side policy.evaluated chains back to the human event
    # — the human action causes the new policy evaluation.
    assert approve_policy_event.causation_id == human_event.id
    # Payload sanity — Slice 4 fills policy="approve_per_row",
    # passed=True so the projection's last-seen fold flips
    # instinct_policy_passed True.
    assert approve_policy_event.payload["policy"] == "approve_per_row"
    assert approve_policy_event.payload["passed"] is True
    assert "approved by user:" in approve_policy_event.payload["reason"]


# ---------------------------------------------------------------------------
# 7. Security — cross-workspace reject is 403 with NO events emitted
# ---------------------------------------------------------------------------


class TestRejectCrossWorkspaceSecurity:
    def test_single_reject_of_foreign_workspace_pocket_write_is_403(
        self, journal, graph: DecisionGraph, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A ws-A approver POSTing /reject on an action whose parked
        write belongs to ws-B is forbidden — the action stays pending
        and no chain events fire (the security check runs BEFORE the
        emits, mirror of bulk-approve's BLOCKER 1)."""
        user = _FakeUser("user-A", "ws-A")
        client = _make_client(router_store, user, monkeypatch)

        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-B",
                title="ws-B write",
                parameters=_pocket_write_params(workspace_id="ws-B", correlation_id=uuid4()),
            )
            resp = client.post(
                f"/instinct/actions/{action_id}/reject",
                json={"reason": "nope"},
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"

        # No chain events fired — the security check fails BEFORE any
        # emit attempt.
        assert _events(journal, "human.corrected") == []
        assert _events(journal, "decision.completed") == []

    def test_single_reject_of_own_workspace_pocket_write_succeeds(
        self, journal, graph: DecisionGraph, router_store: InstinctStore, monkeypatch
    ) -> None:
        """The same-tenant happy path — a ws-A approver may reject a
        ws-A parked write; both chain events land."""
        user = _FakeUser("user-A", "ws-A")
        client = _make_client(router_store, user, monkeypatch)
        corr = uuid4()

        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose(
                client,
                pocket_id="pocket-A",
                title="ws-A write",
                parameters=_pocket_write_params(workspace_id="ws-A", correlation_id=corr),
            )
            resp = client.post(
                f"/instinct/actions/{action_id}/reject",
                json={"reason": "ok"},
            )
            assert resp.status_code == 200, resp.text

        chain = _events_by_correlation(journal, corr)
        actions = [e.action for e in chain]
        assert actions == ["human.corrected", "decision.completed"]

    def test_bulk_reject_of_foreign_workspace_pocket_write_is_403(
        self, journal, graph: DecisionGraph, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A ws-A approver bulk-rejecting a list containing a ws-B
        parked write is forbidden — and no action flips, no chain
        events fire (whole-batch-fails semantics mirror bulk-approve)."""
        user = _FakeUser("user-A", "ws-A")
        client = _make_client(router_store, user, monkeypatch)

        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose(
                client,
                pocket_id="pocket-A",
                title="ws-A write",
                parameters=_pocket_write_params(workspace_id="ws-A", correlation_id=uuid4()),
            )
            foreign = _propose(
                client,
                pocket_id="pocket-B",
                title="ws-B write",
                parameters=_pocket_write_params(workspace_id="ws-B", correlation_id=uuid4()),
            )
            resp = client.post(
                "/instinct/actions/bulk-reject",
                json={"ids": [own, foreign], "reason": "batch nope"},
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"

        # No chain events for either item — the cross-workspace check
        # raises BEFORE store.bulk_reject runs.
        assert _events(journal, "human.corrected") == []
        assert _events(journal, "decision.completed") == []


# ---------------------------------------------------------------------------
# 8. End-to-end happy path via DecisionGraph.find — full producer set
# ---------------------------------------------------------------------------


async def test_full_chain_via_graph_with_real_proposed_event(
    monkeypatch, journal, graph: DecisionGraph, router_store: InstinctStore
):
    """End-to-end with a real ``agent.proposed`` event from
    ``action_executor.run_action`` opening the chain. Exercises the
    full producer set: action_executor → bridge → router approve →
    bridge close. Asserts:

      * the cause-arrow chain
        ``agent.proposed → policy.evaluated → human.corrected → decision.completed``
        holds,
      * the Decision row is queryable via ``DecisionGraph.find``,
      * the approver attribution is present + the chain is NOT marked
        rejected (the terminal ``decision.completed(passed=True)``
        overrides the ``policy.evaluated(passed=False)`` parked state).

    The outcomes-side emit (``emit_pocket_outcome``) is no-opped by
    swallowing the AssertionError it raises when the EventBus isn't
    initialised — those tests have their own coverage; this test is
    about the chain folding."""
    import httpx
    from pocketpaw_ee.cloud.pockets import action_executor
    from pocketpaw_ee.cloud.pockets.action_executor import run_action

    user = _FakeUser("user-A", "ws-A")
    client = _make_client(router_store, user, monkeypatch)
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: router_store)

    def _fake_getaddrinfo(host, *_args, **_kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo)

    async def _get_creds(_workspace_id, _pocket_id):
        return (
            "https://api.example.com",
            "none",
            None,
            "",
            [
                {"method": "POST", "path_pattern": "/leases/*/renew"},
            ],
            None,
        )

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
        _get_creds,
    )

    real_client = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": 1}))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _client_factory)
    action_executor._action_log.clear()

    # The bridge's success path calls ``outcomes_service.emit_pocket_outcome``
    # which needs the EventBus initialised — out of scope for this
    # test. Stub it as a no-op so the chain close itself is what's
    # under test.
    from pocketpaw_ee.cloud.outcomes import service as outcomes_service

    async def _noop_emit(**_kwargs):
        return None

    monkeypatch.setattr(outcomes_service, "emit_pocket_outcome", _noop_emit)

    corr = uuid4()
    # First entry — the executor mints / accepts correlation_id and
    # fires agent.proposed. Then it returns instinct_pending with
    # _park carrying correlation_id.
    park_result = await run_action(
        workspace_id="ws-A",
        pocket_id="pocket-A",
        user_id="user-A",
        action="mark_renewed",
        raw_action={
            "kind": "write_binding",
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {},
            "requires_instinct": True,
            "outcome": "renewal_completed",
        },
        path="/leases/42/renew",
        params={"rent": 2000},
        base_url="https://api.example.com",
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=[{"method": "POST", "path_pattern": "/leases/*/renew"}],
        correlation_id=corr,
    )
    assert park_result["code"] == "instinct_pending"

    # Bridge stores the parked Action; this also fires the parked
    # policy.evaluated and back-writes parked_policy_event_id.
    action_id = await instinct_bridge_module.propose_pocket_write(
        pocket=_seed_pocket("ws-A", "pocket-A"),
        backend_config=None,
        parked_write=park_result["_park"],
        requested_by="user-A",
    )

    # Approve over HTTP — fires human.corrected then re-enters
    # run_action via the bridge, which closes the chain with
    # decision.completed.
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert resp.status_code == 200, resp.text

    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    # Expected order (Slice 4): agent.proposed (executor) →
    # policy.evaluated (bridge propose, parked) → human.corrected
    # (router approve) → policy.evaluated (router approve, Slice 4
    # chain-symmetry emit, passed=True) → decision.completed (bridge
    # close on re-entry success).
    assert actions == [
        "agent.proposed",
        "policy.evaluated",
        "human.corrected",
        "policy.evaluated",
        "decision.completed",
    ], actions

    proposed, parked_policy, human, approve_policy, completed = chain
    # The full cause-arrow chain — each event's causation_id (when
    # present) points back at the previous emit's id.
    assert proposed.causation_id is None  # the chain origin
    # bridge's parked policy.evaluated doesn't currently chain a
    # causation_id (Slice 2 bridge doesn't pass one).
    assert human.causation_id == parked_policy.id
    # Slice 4 — the approve-side policy.evaluated chains back to the
    # human.corrected event.
    assert approve_policy.causation_id == human.id
    # decision.completed from the bridge does not set causation_id —
    # the chain still folds correctly via correlation_id.

    # The folded Decision row exists and is queryable via the graph.
    assert graph.store.count() == 1
    decisions = await graph.find()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.correlation_id == corr
    assert decision.intent.startswith("POST /leases/42/renew")
    assert decision.pocket_id == "pocket-A"
    # The human approver is attached.
    assert len(decision.approvers) == 1
    assert decision.approvers[0].actor.kind == "user"
    assert decision.approvers[0].actor.id == "user:user-A"
    # Slice 4 (c) — the approve-side ``policy.evaluated(passed=True)``
    # is now emitted by the router; the projection's ``_fold_policy``
    # keeps the LAST policy event, so the approved chain reads
    # ``instinct_policy="approve_per_row"`` + ``instinct_policy_passed=
    # True``. Pre-Slice-4 this read False because only the parked
    # passed=False event was visible. The chain is NOT marked rejected
    # because the terminal ``decision.completed`` payload's
    # ``passed=True`` overrides the policy state (projection.py:
    # ``_close_chain``). The outcome stays None — no rejection, no
    # late attach happened in this test.
    assert decision.instinct_policy == "approve_per_row"
    assert decision.instinct_policy_passed is True
    assert decision.outcome is None or decision.outcome.status != "rejected"
