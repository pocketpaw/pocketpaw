# tests/cloud/test_pocket_create_gate.py — the gated _pocket_create proposal type
# (SZD-5b), a tenancy gate over a proposed starter Pocket.
#
# Created: 2026-06-19 (SZD-5b — _pocket_create Instinct proposal type).
#
# What this pins:
#   * propose_pocket — blob shape (schema 1, pocket_spec, SEPARATE top-level
#     workspace_id + user_id, correlation_id, summary), tenancy (workspace + owner
#     required), name requirement, AND the security invariant: tenancy/owner are
#     NOT inside the editable pocket_spec.
#   * execute_approved_pocket_create against the REAL pockets.service.create
#     (mongomock Beanie):
#       - happy path: a Pocket is created in the workspace, owned correctly, action
#         EXECUTED, structured outcome (pocket_id/name) back-written.
#       - model_validate is enforced: a rippleSpec camelCase-alias spec resolves; a
#         structurally invalid spec (blank name) FAILS cleanly (no Pocket created),
#         not silently.
#       - schema mismatch: a stale schema blob → FAILED, no Pocket created.
#       - idempotency: re-approve / re-invoke does NOT double-create.
#   * the 4-PATH cross-workspace tenancy gate through the REAL router:
#       - approve / reject (single) AND bulk-approve / bulk-reject all 403 a
#         cross-workspace caller — a missing guard on ANY of the four is a leak.
#   * the MANDATORY production-path chain test: propose → approve via the REAL
#     router → executor creates the Pocket → walk the decision chain and assert
#     EXACTLY agent.proposed → human.corrected → decision.completed (one terminal),
#     and the reject path's router-owned close (executor never runs).
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The instinct store is
# patched to a tmp-file InstinctStore; the Pocket create runs against the shared
# mongomock Beanie ``mongo_db`` fixture so the create is real but isolated. The
# integration tests wire a fresh on-disk journal + DecisionGraph (same fixture
# shape as tests/cloud/test_fabric_objects_gate.py) and drive the router over a
# TestClient with stubbed auth deps.

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

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
from pocketpaw_ee.cloud.pocket_proposals import executor as pc_executor  # noqa: E402
from pocketpaw_ee.cloud.pocket_proposals import propose as pc_propose  # noqa: E402
from pocketpaw_ee.cloud.pockets import service as pockets_service  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    """Stable AUTH_SECRET so the pockets create path's token machinery is quiet."""
    monkeypatch.setenv("AUTH_SECRET", "pocket-create-gate-test-secret")


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired everywhere the gate reads it
    (the propose helper + the executor both lazy-import
    ``pocketpaw.stores.get_instinct_store``)."""
    st = InstinctStore(tmp_path / "instinct_pocket_create_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


# A canonical rippleSpec staged on the proposal. The camelCase alias ``rippleSpec``
# is exercised by the model_validate test.
_RIPPLE_SPEC = {
    "version": "1.0",
    "root": {"id": "root", "type": "container", "children": []},
}


async def _propose(store, **overrides) -> str:
    kwargs: dict[str, Any] = dict(
        workspace_id="w1",
        user_id="u1",
        ripple_spec=_RIPPLE_SPEC,
        name="Starter dashboard",
    )
    kwargs.update(overrides)
    return await pc_propose.propose_pocket(**kwargs)


# ---------------------------------------------------------------------------
# propose — blob shape + tenancy + the un-editable-tenancy security invariant
# ---------------------------------------------------------------------------


async def test_propose_builds_pocket_create_blob(store):
    """propose_pocket files an Action carrying a well-formed schema-1
    ``_pocket_create`` blob — pocket_spec, SEPARATE top-level workspace_id +
    user_id, correlation_id, summary."""
    action_id = await _propose(store)
    action = await store.get_action(action_id)
    assert action is not None
    assert action.status == ActionStatus.PENDING

    blob = action.parameters["_pocket_create"]
    assert blob["kind"] == "pocket_create"
    assert blob["schema"] == 1
    # Tenancy + owner are SEPARATE top-level blob fields.
    assert blob["workspace_id"] == "w1"
    assert blob["user_id"] == "u1"
    # The staged spec carries name + the camelCase rippleSpec alias.
    spec = blob["pocket_spec"]
    assert spec["name"] == "Starter dashboard"
    assert spec["rippleSpec"] == _RIPPLE_SPEC
    assert blob["correlation_id"]
    assert blob["summary"]


async def test_propose_tenancy_not_in_editable_spec(store):
    """SECURITY — workspace_id / user_id are NEVER inside the editable pocket_spec
    (the correction flow can edit pocket_spec; it must not be able to move tenancy
    or owner). Even a caller passing them via ``extra`` cannot smuggle them in."""
    action_id = await _propose(
        store,
        extra={"workspace_id": "w-EVIL", "user_id": "u-EVIL", "description": "ok"},
    )
    blob = (await store.get_action(action_id)).parameters["_pocket_create"]
    spec = blob["pocket_spec"]
    assert "workspace_id" not in spec
    assert "user_id" not in spec
    assert "workspace" not in spec
    assert "owner" not in spec
    # The legitimate extra field rode through.
    assert spec["description"] == "ok"
    # The real tenancy/owner are unchanged on the top-level fields.
    assert blob["workspace_id"] == "w1"
    assert blob["user_id"] == "u1"


async def test_propose_requires_workspace(store):
    """A propose with no workspace_id is rejected — a Pocket create with no tenant
    to scope it to is a tenancy hole."""
    with pytest.raises(ValueError, match="workspace_id"):
        await pc_propose.propose_pocket(
            workspace_id="", user_id="u1", name="x", ripple_spec=_RIPPLE_SPEC
        )


async def test_propose_requires_owner(store):
    """A propose with no user_id (owner) is rejected."""
    with pytest.raises(ValueError, match="user_id"):
        await pc_propose.propose_pocket(
            workspace_id="w1", user_id="", name="x", ripple_spec=_RIPPLE_SPEC
        )


async def test_propose_requires_name(store):
    """A propose with a blank name is rejected — nothing to create."""
    with pytest.raises(ValueError, match="name"):
        await pc_propose.propose_pocket(
            workspace_id="w1", user_id="u1", name="  ", ripple_spec=_RIPPLE_SPEC
        )


# ---------------------------------------------------------------------------
# executor — happy path, model_validate, schema, idempotency (real Beanie create)
# ---------------------------------------------------------------------------


async def _propose_and_approve(store, **overrides) -> Any:
    action_id = await _propose(store, **overrides)
    return await store.approve(action_id, approver="u1")


async def test_executor_happy_path_creates_pocket(store, mongo_db):
    """On approve the executor creates the Pocket via pockets.service.create
    (workspace-scoped, owned by the blob's top-level user_id), marks EXECUTED, and
    back-writes the structured outcome carrying the created pocket id + name."""
    approved = await _propose_and_approve(store)
    await pc_executor.execute_approved_pocket_create(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.EXECUTED
    outcome = final.parameters["_pocket_create"]["outcome"]
    assert outcome["status"] == "executed"
    assert outcome["name"] == "Starter dashboard"
    assert outcome["pocket_id"]
    assert "executed_at" in outcome

    # The Pocket exists in w1, owned by u1.
    wire = await pockets_service.get(outcome["pocket_id"], "u1")
    assert wire["name"] == "Starter dashboard"
    assert wire["workspace"] == "w1"
    assert wire["owner"] == "u1"


async def test_executor_model_validate_resolves_camelcase_alias(store, mongo_db):
    """The staged spec uses the camelCase ``rippleSpec`` alias; model_validate at
    the entry to the create path resolves it (populate_by_name=True) so the spec
    materialises with the rippleSpec attached."""
    approved = await _propose_and_approve(store)
    await pc_executor.execute_approved_pocket_create(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.EXECUTED
    pocket_id = final.parameters["_pocket_create"]["outcome"]["pocket_id"]
    wire = await pockets_service.get(pocket_id, "u1")
    # The rippleSpec round-tripped onto the created pocket (normalized).
    assert wire.get("rippleSpec") is not None


async def test_executor_invalid_spec_fails_cleanly(store, mongo_db, monkeypatch):
    """A structurally invalid staged spec (blank name violates the DTO's
    min_length=1) FAILS the action cleanly at model_validate — NOT a silent
    skip, NOT a crash — and no Pocket is created."""
    approved = await _propose_and_approve(store)
    # Corrupt the staged spec to an invalid CreatePocketRequest (name too short).
    # The propose helper rejects a blank name, so we tamper the persisted blob
    # directly to simulate a malformed/edited spec reaching the executor.
    approved.parameters["_pocket_create"]["pocket_spec"]["name"] = ""

    before_count = await _count_pockets("w1")
    await pc_executor.execute_approved_pocket_create(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "invalid" in str(final.error).lower()
    # Nothing was created.
    assert await _count_pockets("w1") == before_count


async def test_executor_schema_mismatch_refuses(store, mongo_db):
    """A stale blob with an incompatible schema version → FAILED, no Pocket
    created."""
    approved = await _propose_and_approve(store)
    approved.parameters["_pocket_create"]["schema"] = 999  # incompatible

    before_count = await _count_pockets("w1")
    await pc_executor.execute_approved_pocket_create(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "schema" in str(final.error).lower()
    assert await _count_pockets("w1") == before_count


async def test_executor_missing_workspace_refuses(store, mongo_db):
    """A blob whose top-level workspace_id is empty → FAILED, no Pocket created
    (belt-and-braces behind the router gate)."""
    approved = await _propose_and_approve(store)
    approved.parameters["_pocket_create"]["workspace_id"] = ""

    before_count = await _count_pockets("w1")
    await pc_executor.execute_approved_pocket_create(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "workspace" in str(final.error).lower()
    assert await _count_pockets("w1") == before_count


async def test_executor_idempotent_never_reruns(store, mongo_db):
    """Re-invoking the executor on an already-EXECUTED Action short-circuits before
    any create (the idempotency guard) — re-approve / retry never double-creates."""
    approved = await _propose_and_approve(store)
    await pc_executor.execute_approved_pocket_create(approved)
    count_after_first = await _count_pockets("w1")
    assert count_after_first == 1

    reloaded = await store.get_action(approved.id)
    await pc_executor.execute_approved_pocket_create(reloaded)
    # No additional pocket — the guard skipped the re-run.
    assert await _count_pockets("w1") == count_after_first


async def test_executor_no_blob_is_noop(store, mongo_db):
    """An Action with no ``_pocket_create`` blob is a clean no-op."""
    from pocketpaw.instinct.models import ActionTrigger

    action = await store.propose(
        pocket_id="w1",
        title="not pocket-create",
        description="",
        recommendation="",
        trigger=ActionTrigger(type="agent", source="x", reason="y"),
    )
    approved = await store.approve(action.id, approver="u1")
    before_count = await _count_pockets("w1")
    await pc_executor.execute_approved_pocket_create(approved)
    assert await _count_pockets("w1") == before_count


async def _count_pockets(workspace_id: str) -> int:
    """Count the pockets in a workspace via the service list path."""
    wires = await pockets_service.list_pockets(workspace_id, "u1")
    return len(wires)


# ---------------------------------------------------------------------------
# Integration fixtures — journal + decision graph + router client
# ---------------------------------------------------------------------------


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


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "u1", workspace_id: str = "w1") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _make_client(user: _FakeUser, monkeypatch) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app)


def _events_by_correlation(journal, correlation_id: UUID) -> list:
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


# ---------------------------------------------------------------------------
# MANDATORY production-path chain tests
# ---------------------------------------------------------------------------


async def test_production_path_approve_runs_executor_one_terminal(
    store, mongo_db, journal, graph, monkeypatch
):
    """propose → approve through the REAL router → the executor creates the Pocket
    → walk the decision chain and assert EXACTLY agent.proposed → human.corrected →
    decision.completed (ONE terminal, executor owns the close)."""
    action_id = await _propose(store)
    blob = (await store.get_action(action_id)).parameters["_pocket_create"]
    corr = UUID(blob["correlation_id"])

    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 200, resp.text

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED
    # The Pocket landed in w1, owned by u1.
    pocket_id = final.parameters["_pocket_create"]["outcome"]["pocket_id"]
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["workspace"] == "w1"
    assert wire["owner"] == "u1"

    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions
    assert actions.count("decision.completed") == 1
    terminal = chain[-1]
    assert (terminal.payload or {})["passed"] is True
    assert (terminal.payload or {})["action_outcome"] == "landed"
    hc = chain[1]
    assert terminal.causation_id == hc.id


async def test_production_path_reject_router_closes_executor_never_runs(
    store, mongo_db, journal, graph, monkeypatch
):
    """Reject path: the ROUTER emits human.corrected + decision.completed
    (rejected); the executor NEVER runs (no Pocket created)."""
    action_id = await _propose(store)
    blob = (await store.get_action(action_id)).parameters["_pocket_create"]
    corr = UUID(blob["correlation_id"])

    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "not now"})
    assert resp.status_code == 200, resp.text

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.REJECTED
    # NO Pocket was created on reject.
    assert await _count_pockets("w1") == 0

    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions
    assert actions.count("decision.completed") == 1
    terminal = chain[-1]
    assert (terminal.payload or {})["passed"] is False
    assert (terminal.payload or {})["action_outcome"] == "rejected"


# ---------------------------------------------------------------------------
# 4-PATH cross-workspace tenancy gate — a missing guard on ANY path is a leak.
# ---------------------------------------------------------------------------


async def test_cross_workspace_single_approve_forbidden(
    store, mongo_db, journal, graph, monkeypatch
):
    """PATH 1/4 — single approve: a caller in w1 cannot approve a w-OTHER
    Pocket-create Action."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING
    # Nothing created in either workspace.
    assert await _count_pockets("w-OTHER") == 0
    assert await _count_pockets("w1") == 0


async def test_cross_workspace_single_reject_forbidden(
    store, mongo_db, journal, graph, monkeypatch
):
    """PATH 2/4 — single reject: a caller in w1 cannot reject a w-OTHER
    Pocket-create Action (asymmetric tenant scope is no tenant scope)."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "no"})
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING


async def test_cross_workspace_bulk_approve_forbidden(store, mongo_db, journal, graph, monkeypatch):
    """PATH 3/4 — bulk approve: a single cross-workspace item 403s the whole batch
    before any mutation."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post("/instinct/actions/bulk-approve", json={"ids": [action_id]})
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING
    assert await _count_pockets("w-OTHER") == 0


async def test_cross_workspace_bulk_reject_forbidden(store, mongo_db, journal, graph, monkeypatch):
    """PATH 4/4 — bulk reject: a single cross-workspace item 403s the whole batch
    before any mutation."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post("/instinct/actions/bulk-reject", json={"ids": [action_id], "reason": "no"})
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING
