# tests/ee/test_approve_policy_evaluated_emit.py
# Created: 2026-05-26 (RFC 09 Slice 4 — feat/rfc-09-slice-4-reconciler)
#
# Pins the chain-symmetry follow-up (Captain Decision 12). On the
# approve / bulk-approve path the router now emits an additional
# ``policy.evaluated(passed=True, policy="approve_per_row")`` AFTER
# the ``human.corrected`` event and BEFORE the bridge fires its chain
# close. The projection's ``_fold_policy`` keeps the LAST policy event
# before close, so this flips ``Decision.instinct_policy_passed`` from
# the parked ``False`` to ``True``. Reject chains stay False.
#
# Tests:
#   1. Single-approve emits the True policy.evaluated; the projection
#      folds it and the Decision row reads ``instinct_policy=
#      "approve_per_row"`` + ``instinct_policy_passed=True``.
#   2. Reject still leaves the chain with the parked False policy →
#      Decision.instinct_policy_passed=False.
#   3. Bulk-approve fires the True emit per item.
#   4. The full chain via DecisionGraph.find traces correctly through
#      the new event.

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
from pocketpaw_ee.instinct.router import router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402


@pytest.fixture
def journal(tmp_path: Path):
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
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def instinct_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "instinct.db")


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _make_client(store: InstinctStore, user: _FakeUser, monkeypatch) -> TestClient:
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
    correlation_id: UUID,
    parked_policy_event_id: str | None = None,
) -> dict:
    return {
        "_pocket_write": {
            "schema": 2,
            "action": "mark_renewed",
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {"rent": 2000},
            "idempotency_key": "idem-xyz",
            "outcome": "renewal_completed",
            "workspace_id": workspace_id,
            "requested_by": "requester-9",
            "correlation_id": str(correlation_id),
            "parked_policy_event_id": parked_policy_event_id,
        }
    }


def _propose(
    client: TestClient,
    *,
    pocket_id: str,
    title: str,
    parameters: dict[str, Any],
) -> str:
    body = {
        "pocket_id": pocket_id,
        "title": title,
        "description": "",
        "recommendation": "",
        "trigger": {"type": "agent", "source": "claude", "reason": "test"},
        "parameters": parameters,
    }
    resp = client.post("/instinct/actions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _events(journal, action: str | None = None) -> list:
    out = []
    for e in journal.replay_from(0):
        if action is None or e.action == action:
            out.append(e)
    return out


async def test_approve_emits_passed_true_policy_evaluated(
    monkeypatch, journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """The approve path now emits ``policy.evaluated(passed=True,
    policy="approve_per_row")`` after the ``human.corrected`` event.
    """
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(instinct_store, user, monkeypatch)

    async def _noop_execute(_action):
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _noop_execute,
    )

    corr = uuid4()
    parked_policy_event_id = str(uuid4())
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        action_id = _propose(
            client,
            pocket_id="pocket-A",
            title="approve test",
            parameters=_pocket_write_params(
                workspace_id="ws-A",
                correlation_id=corr,
                parked_policy_event_id=parked_policy_event_id,
            ),
        )
        resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert resp.status_code == 200, resp.text

    policy_events = _events(journal, "policy.evaluated")
    # One policy.evaluated event landed (the approve-side emit) — the
    # parked-side emit only fires when the bridge drives propose. This
    # test calls the router's POST /actions to seed the row.
    assert len(policy_events) == 1
    pe = policy_events[0]
    payload = pe.payload or {}
    assert payload["policy"] == "approve_per_row"
    assert payload["passed"] is True
    assert "approved by user:user-A" in payload["reason"]
    assert payload["action_id"] == action_id
    assert pe.actor.kind == "user"
    assert pe.actor.id == "user:user-A"


async def test_reject_emits_no_passed_true_policy_evaluated(
    monkeypatch, journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """The reject path does NOT emit the approve-side
    ``policy.evaluated(passed=True)``. The chain's last-seen policy
    state stays the parked ``passed=False`` (or absent for direct router
    propose), keeping ``Decision.instinct_policy_passed=False``."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(instinct_store, user, monkeypatch)

    corr = uuid4()
    parked_policy_event_id = str(uuid4())
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        action_id = _propose(
            client,
            pocket_id="pocket-A",
            title="reject test",
            parameters=_pocket_write_params(
                workspace_id="ws-A",
                correlation_id=corr,
                parked_policy_event_id=parked_policy_event_id,
            ),
        )
        resp = client.post(
            f"/instinct/actions/{action_id}/reject",
            json={"reason": "not now"},
        )
        assert resp.status_code == 200, resp.text

    # No policy.evaluated emit on the reject path.
    assert _events(journal, "policy.evaluated") == []


async def test_bulk_approve_emits_passed_true_per_item(
    monkeypatch, journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """Each item in a bulk-approve flips the policy.evaluated true
    emit. N items → N approve-side policy events."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(instinct_store, user, monkeypatch)

    async def _noop_execute(_action):
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _noop_execute,
    )

    corrs = [uuid4() for _ in range(3)]
    ids: list[str] = []
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        for i, corr in enumerate(corrs):
            ids.append(
                _propose(
                    client,
                    pocket_id=f"pocket-{i}",
                    title=f"bulk approve {i}",
                    parameters=_pocket_write_params(
                        workspace_id="ws-A",
                        correlation_id=corr,
                        parked_policy_event_id=str(uuid4()),
                    ),
                )
            )
        resp = client.post(
            "/instinct/actions/bulk-approve",
            json={"ids": ids, "approver": "ignored"},
        )
        assert resp.status_code == 200, resp.text

    policy_events = _events(journal, "policy.evaluated")
    assert len(policy_events) == 3
    # Each event maps to a unique correlation_id (the bulk loop fires
    # one per item).
    assert {pe.correlation_id for pe in policy_events} == set(corrs)
    for pe in policy_events:
        assert (pe.payload or {})["policy"] == "approve_per_row"
        assert (pe.payload or {})["passed"] is True


async def test_approve_chain_order_human_then_policy_evaluated(
    monkeypatch, journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """The approve path's emit order is ``human.corrected`` first, then
    ``policy.evaluated(passed=True)`` — so the projection sees the
    last-policy = passed=True at close time."""
    user = _FakeUser("user-A", "ws-A")
    client = _make_client(instinct_store, user, monkeypatch)

    async def _noop_execute(_action):
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _noop_execute,
    )

    corr = uuid4()
    parked_policy_event_id = str(uuid4())
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        action_id = _propose(
            client,
            pocket_id="pocket-A",
            title="order test",
            parameters=_pocket_write_params(
                workspace_id="ws-A",
                correlation_id=corr,
                parked_policy_event_id=parked_policy_event_id,
            ),
        )
        resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert resp.status_code == 200, resp.text

    chain = [e for e in journal.replay_from(0) if e.correlation_id == corr]
    actions = [e.action for e in chain]
    assert actions == ["human.corrected", "policy.evaluated"], actions
    # The approve-side policy.evaluated chains back to the
    # human.corrected event.
    assert chain[1].causation_id == chain[0].id
