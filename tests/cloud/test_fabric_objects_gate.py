# tests/cloud/test_fabric_objects_gate.py — the gated _fabric_objects proposal
# type (SZD-5a), a tenancy gate over a proposed Fabric ontology.
#
# Created: 2026-06-19 (SZD-5a — _fabric_objects Instinct proposal type).
#
# What this pins:
#   * propose_fabric_objects — blob shape (schema 1, object_types, objects,
#     links, workspace_id, correlation_id, summary), tenancy (workspace
#     required), object-count requirement.
#   * execute_approved_fabric_objects against a REAL tmp FabricStore:
#       - happy path: objects materialised via ingest_records, types defined,
#         links created, action EXECUTED, structured outcome back-written.
#       - dedup: re-approve (or two proposals asserting the same
#         (source_connector, source_id)) does NOT duplicate objects OR links.
#       - schema mismatch: a stale schema blob → FAILED, no Fabric write.
#   * the 4-PATH cross-workspace tenancy gate through the REAL router:
#       - approve / reject (single) AND bulk-approve / bulk-reject all 403 a
#         cross-workspace caller — a missing guard on ANY of the four is a leak.
#   * the MANDATORY production-path chain test: propose → approve via the REAL
#     router → executor materialises objects → walk the decision chain and assert
#     EXACTLY agent.proposed → human.corrected → decision.completed (one
#     terminal), and the reject path's router-owned close (executor never runs).
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The instinct store is
# patched to a tmp-file InstinctStore; the FABRIC store is patched to a tmp-file
# FabricStore so the writes are real but isolated. The integration tests wire a
# fresh on-disk journal + DecisionGraph (same fixture shape as
# tests/cloud/test_external_action_gate.py) and drive the router over a
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
from pocketpaw_ee.cloud.fabric_proposals import executor as fo_executor  # noqa: E402
from pocketpaw_ee.cloud.fabric_proposals import propose as fo_propose  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.fabric.store import FabricStore  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired everywhere the gate reads it
    (the propose helper + the executor both lazy-import
    ``pocketpaw.stores.get_instinct_store``)."""
    st = InstinctStore(tmp_path / "instinct_fabric_objects_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.fixture
def fabric(tmp_path: Path, monkeypatch) -> FabricStore:
    """Isolated FabricStore on a tmp file, wired into ``get_fabric_store`` so the
    executor writes real (but isolated) typed objects + links."""
    fs = FabricStore(tmp_path / "fabric_objects_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda: fs)
    return fs


# A canonical proposal payload reused across tests.
_OBJECT_TYPES = [
    {
        "type_name": "Customer",
        "description": "A discovered customer",
        "properties": [
            {"name": "name", "type": "string"},
            {"name": "email", "type": "string"},
        ],
    },
    {
        "type_name": "Order",
        "properties": [{"name": "total", "type": "number"}],
    },
]
_OBJECTS = [
    {
        "type_name": "Customer",
        "properties": {"name": "Acme Inc", "email": "ops@acme.test"},
        "source_connector": "crm",
        "source_id": "cust-1",
    },
    {
        "type_name": "Order",
        "properties": {"total": 42},
        "source_connector": "billing",
        "source_id": "ord-9",
    },
]
_LINKS = [
    {
        "from": {"source_connector": "billing", "source_id": "ord-9"},
        "to": {"source_connector": "crm", "source_id": "cust-1"},
        "link_type": "placed_by",
    }
]


async def _propose(store, **overrides) -> str:
    kwargs: dict[str, Any] = dict(
        workspace_id="w1",
        objects=_OBJECTS,
        object_types=_OBJECT_TYPES,
        links=_LINKS,
        requested_by="u1",
    )
    kwargs.update(overrides)
    return await fo_propose.propose_fabric_objects(**kwargs)


# ---------------------------------------------------------------------------
# propose — blob shape + tenancy
# ---------------------------------------------------------------------------


async def test_propose_builds_fabric_objects_blob(store):
    """propose_fabric_objects files an Action carrying a well-formed schema-1
    ``_fabric_objects`` blob — object_types, objects, links, workspace_id,
    correlation_id, summary."""
    action_id = await _propose(store)
    action = await store.get_action(action_id)
    assert action is not None
    assert action.status == ActionStatus.PENDING

    blob = action.parameters["_fabric_objects"]
    assert blob["kind"] == "fabric_objects"
    assert blob["schema"] == 1
    assert blob["workspace_id"] == "w1"
    assert len(blob["object_types"]) == 2
    assert len(blob["objects"]) == 2
    assert len(blob["links"]) == 1
    assert blob["objects"][0]["source_connector"] == "crm"
    assert blob["objects"][0]["source_id"] == "cust-1"
    assert blob["requested_by"] == "u1"
    assert blob["correlation_id"]
    assert blob["summary"]


async def test_propose_requires_workspace(store):
    """A propose with no workspace_id is rejected — a Fabric write with no tenant
    to scope it to is a tenancy hole."""
    with pytest.raises(ValueError, match="workspace_id"):
        await fo_propose.propose_fabric_objects(
            workspace_id="",
            objects=_OBJECTS,
            requested_by="u1",
        )


async def test_propose_requires_objects(store):
    """A propose with no usable objects is rejected — nothing to materialise."""
    with pytest.raises(ValueError, match="object"):
        await fo_propose.propose_fabric_objects(
            workspace_id="w1",
            objects=[],
            requested_by="u1",
        )


# ---------------------------------------------------------------------------
# executor — happy path, dedup, schema
# ---------------------------------------------------------------------------


async def _propose_and_approve(store, **overrides) -> Any:
    action_id = await _propose(store, **overrides)
    return await store.approve(action_id, approver="u1")


async def test_executor_happy_path_materialises_objects(store, fabric):
    """On approve the executor materialises the proposed objects via
    ingest_records (workspace-scoped), defines the types, creates the link, marks
    EXECUTED, and back-writes the structured outcome."""
    approved = await _propose_and_approve(store)
    await fo_executor.execute_approved_fabric_objects(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.EXECUTED
    outcome = final.parameters["_fabric_objects"]["outcome"]
    assert outcome["status"] == "executed"
    assert outcome["created"] == 2
    assert outcome["updated"] == 0
    assert outcome["links_created"] == 1
    assert "executed_at" in outcome

    # The objects exist in Fabric, workspace-scoped, with provenance.
    cust = await fabric.get_object_by_source(
        source_connector="crm", source_id="cust-1", workspace_id="w1"
    )
    assert cust is not None
    assert cust.properties["name"] == "Acme Inc"
    assert cust.properties["email"] == "ops@acme.test"
    ord_obj = await fabric.get_object_by_source(
        source_connector="billing", source_id="ord-9", workspace_id="w1"
    )
    assert ord_obj is not None
    assert ord_obj.properties["total"] == 42

    # The types were defined in w1.
    types = await fabric.list_types(workspace_id="w1")
    type_names = {t.name for t in types}
    assert {"Customer", "Order"} <= type_names

    # The link exists, workspace-scoped.
    links, total = await fabric.list_links(
        from_id=ord_obj.id, to_id=cust.id, link_type="placed_by", workspace_id="w1"
    )
    assert total == 1


async def test_executor_reapprove_does_not_duplicate(store, fabric):
    """Re-running the executor (idempotency-guard bypassed by re-proposing the
    SAME objects in a SECOND action) does NOT duplicate objects OR links — the
    ingest dedup keys on (source_connector, source_id) and the link dedup on
    (from, to, link_type)."""
    # First proposal — materialise.
    approved1 = await _propose_and_approve(store)
    await fo_executor.execute_approved_fabric_objects(approved1)

    # Second proposal asserting the SAME objects + link.
    approved2 = await _propose_and_approve(store)
    await fo_executor.execute_approved_fabric_objects(approved2)

    final2 = await store.get_action(approved2.id)
    out2 = final2.parameters["_fabric_objects"]["outcome"]
    # Second run UPDATES the existing objects (no new create) and creates NO new
    # link (the (from,to,link_type) already exists).
    assert out2["created"] == 0
    assert out2["updated"] == 2
    assert out2["links_created"] == 0

    # Exactly ONE object per type — no duplicates split across two type_ids or
    # two rows.
    from pocketpaw.fabric.models import FabricQuery

    cust_result = await fabric.query(FabricQuery(type_name="Customer"), workspace_id="w1")
    ord_result = await fabric.query(FabricQuery(type_name="Order"), workspace_id="w1")
    assert len(cust_result.objects) == 1
    assert len(ord_result.objects) == 1

    ord_obj = await fabric.get_object_by_source(
        source_connector="billing", source_id="ord-9", workspace_id="w1"
    )
    cust = await fabric.get_object_by_source(
        source_connector="crm", source_id="cust-1", workspace_id="w1"
    )
    _links, total = await fabric.list_links(
        from_id=ord_obj.id, to_id=cust.id, link_type="placed_by", workspace_id="w1"
    )
    assert total == 1  # still ONE link


async def test_executor_idempotent_never_reruns(store, fabric):
    """Re-invoking the executor on an already-EXECUTED Action short-circuits
    before any Fabric write (the idempotency guard)."""
    approved = await _propose_and_approve(store)
    await fo_executor.execute_approved_fabric_objects(approved)
    types_after_first = await fabric.list_types(workspace_id="w1")

    reloaded = await store.get_action(approved.id)
    await fo_executor.execute_approved_fabric_objects(reloaded)
    types_after_second = await fabric.list_types(workspace_id="w1")
    # No additional type rows — the guard skipped the re-run.
    assert len(types_after_second) == len(types_after_first)


async def test_executor_schema_mismatch_refuses(store, fabric):
    """A stale blob with an incompatible schema version → FAILED, no Fabric
    write."""
    approved = await _propose_and_approve(store)
    approved.parameters["_fabric_objects"]["schema"] = 999  # incompatible

    await fo_executor.execute_approved_fabric_objects(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "schema" in str(final.error).lower()
    # Nothing was written.
    types = await fabric.list_types(workspace_id="w1")
    assert types == []


async def test_executor_no_blob_is_noop(store, fabric):
    """An Action with no ``_fabric_objects`` blob is a clean no-op."""
    from pocketpaw.instinct.models import ActionTrigger

    action = await store.propose(
        pocket_id="w1",
        title="not fabric",
        description="",
        recommendation="",
        trigger=ActionTrigger(type="agent", source="x", reason="y"),
    )
    approved = await store.approve(action.id, approver="u1")
    await fo_executor.execute_approved_fabric_objects(approved)
    types = await fabric.list_types(workspace_id="w1")
    assert types == []


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
    store, fabric, journal, graph, monkeypatch
):
    """propose → approve through the REAL router → the executor materialises the
    objects → walk the decision chain and assert EXACTLY agent.proposed →
    human.corrected → decision.completed (ONE terminal, executor owns the close)."""
    action_id = await _propose(store)
    blob = (await store.get_action(action_id)).parameters["_fabric_objects"]
    corr = UUID(blob["correlation_id"])

    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 200, resp.text

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED
    # The objects landed in Fabric.
    assert (
        await fabric.get_object_by_source(
            source_connector="crm", source_id="cust-1", workspace_id="w1"
        )
    ) is not None

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
    store, fabric, journal, graph, monkeypatch
):
    """Reject path: the ROUTER emits human.corrected + decision.completed
    (rejected); the executor NEVER runs (no Fabric write)."""
    action_id = await _propose(store)
    blob = (await store.get_action(action_id)).parameters["_fabric_objects"]
    corr = UUID(blob["correlation_id"])

    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "not now"})
    assert resp.status_code == 200, resp.text

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.REJECTED
    # NO Fabric write happened on reject.
    assert (
        await fabric.get_object_by_source(
            source_connector="crm", source_id="cust-1", workspace_id="w1"
        )
    ) is None
    types = await fabric.list_types(workspace_id="w1")
    assert types == []

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


async def test_cross_workspace_single_approve_forbidden(store, fabric, journal, graph, monkeypatch):
    """PATH 1/4 — single approve: a caller in w1 cannot approve a w-OTHER
    Fabric-objects Action."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING
    # Nothing written.
    assert await fabric.list_types(workspace_id="w-OTHER") == []


async def test_cross_workspace_single_reject_forbidden(store, fabric, journal, graph, monkeypatch):
    """PATH 2/4 — single reject: a caller in w1 cannot reject a w-OTHER
    Fabric-objects Action (asymmetric tenant scope is no tenant scope)."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "no"})
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING


async def test_cross_workspace_bulk_approve_forbidden(store, fabric, journal, graph, monkeypatch):
    """PATH 3/4 — bulk approve: a single cross-workspace item 403s the whole
    batch before any mutation."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post("/instinct/actions/bulk-approve", json={"ids": [action_id]})
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING
    assert await fabric.list_types(workspace_id="w-OTHER") == []


async def test_cross_workspace_bulk_reject_forbidden(store, fabric, journal, graph, monkeypatch):
    """PATH 4/4 — bulk reject: a single cross-workspace item 403s the whole
    batch before any mutation."""
    action_id = await _propose(store, workspace_id="w-OTHER")
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)

    resp = client.post("/instinct/actions/bulk-reject", json={"ids": [action_id], "reason": "no"})
    assert resp.status_code == 403, resp.text
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING
