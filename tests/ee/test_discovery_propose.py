# tests/ee/test_discovery_propose.py — SZD-6 integration: wire DiscoveryRun → the
# two gated proposals + the starter-Pocket assembler + supersede-on-rerun.
#
# Created: 2026-06-19 (SZD-6 / feat/szd-6-integration). Covers the integration
# entry point ``run_discovery_and_propose`` end to end, with the connector read
# MOCKED (no live network) and real-but-isolated InstinctStore + FabricStore +
# mongomock Beanie:
#
#   * a run against a mock connector files TWO proposals (a _fabric_objects pair +
#     a _pocket_create pair) — both PENDING, both tagged with a shared discovery
#     run marker;
#   * approving BOTH (fabric via the executor, pocket via the executor) creates
#     the Fabric objects AND a Pocket whose spec references the fabric.objects
#     source for each discovered type;
#   * a SECOND run supersedes the first run's still-open pair (the first two
#     actions go REJECTED — superseded, not duplicated — and a fresh pair opens);
#   * a low-confidence keyless type is SKIPPED from materialisation and FLAGGED in
#     the result (no crash, not staged);
#   * the assembler binds one fabric.objects widget per high-confidence type.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_discovery_propose.py -q

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.discovery import (  # noqa: E402
    DiscoveryRun,
    DiscoveryRunOptions,
    OntologyDraft,
    ReadAction,
    assemble_discovery_pocket,
    run_discovery_and_propose,
)
from pocketpaw_ee.discovery.orchestrate import (  # noqa: E402
    _draft_to_fabric_proposal_kwargs,
    _find_discovery_marker,
)

from pocketpaw.fabric.store import FabricStore  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Mock connector surface (same shape as tests/ee/test_discovery_run.py).
# ---------------------------------------------------------------------------


@dataclass
class _MockActionResult:
    success: bool
    data: Any = None
    error: str | None = None
    records_affected: int = 0


@dataclass
class _MockActionSchema:
    name: str
    method: str = "GET"
    trust_level: str = "auto"


class _MockAdapter:
    def __init__(self, schemas: list[_MockActionSchema], data_by_action: dict[str, Any]) -> None:
        self._schemas = schemas
        self._data_by_action = data_by_action

    async def actions(self) -> list[_MockActionSchema]:
        return list(self._schemas)

    async def execute(self, action: str, params: dict[str, Any]) -> _MockActionResult:
        if action not in self._data_by_action:
            return _MockActionResult(success=False, error=f"unknown action {action}")
        return _MockActionResult(success=True, data=self._data_by_action[action])


class _MockRegistry:
    def __init__(self, adapters: dict[str, _MockAdapter | None]) -> None:
        self._adapters = adapters

    async def ensure_connected(self, connector_name: str, scope_key: str) -> _MockAdapter | None:
        return self._adapters.get(connector_name)


# Clean, id-keyed records → a HIGH key_confidence type the gate materialises.
def _customers(n: int) -> list[dict[str, Any]]:
    return [{"id": f"cust-{i}", "name": f"Customer {i}", "tier": "gold"} for i in range(n)]


# Records with only repeated, non-unique fields → a KEYLESS low-confidence type
# the gate skips + flags.
def _events(n: int) -> list[dict[str, Any]]:
    return [{"category": "click", "page": "home"} for _ in range(n)]


def _mock_run(adapters: dict[str, _MockAdapter | None]) -> DiscoveryRun:
    return DiscoveryRun(registry=_MockRegistry(adapters))


def _opts_for(read_actions: dict[str, list[ReadAction]]) -> DiscoveryRunOptions:
    return DiscoveryRunOptions(read_actions=read_actions)


# ---------------------------------------------------------------------------
# Fixtures — isolated stores wired into the lazy get_*_store seams.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    """Stable AUTH_SECRET so the pockets create path's token machinery is quiet."""
    monkeypatch.setenv("AUTH_SECRET", "discovery-propose-test-secret")


@pytest.fixture(autouse=True)
def recording_bus():
    """Install an inert recording EventBus so service ``emit()`` calls don't raise.

    The pockets create path (exercised by the approve test) emits ``PocketCreated``
    via ``_core.realtime.emit.emit`` which asserts a bus is set. ``tests/cloud``
    has an autouse RecordingBus fixture for this; ``tests/ee`` doesn't, so we mint
    a minimal one here. Inert by design — tests assert on store state, not events.
    """
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler: Any) -> None:  # noqa: ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]
    yield bus_mod._bus
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_discovery_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.fixture
def fabric(tmp_path: Path, monkeypatch) -> FabricStore:
    fs = FabricStore(tmp_path / "fabric_discovery_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda: fs)
    return fs


# ---------------------------------------------------------------------------
# The assembler binds one fabric.objects widget per high-confidence type.
# ---------------------------------------------------------------------------


def test_assemble_discovery_pocket_binds_fabric_objects_source() -> None:
    """The starter-Pocket spec binds one fabric.objects state source per
    materialised type, and the widget reads its rows from that state key."""
    draft = OntologyDraft.model_validate(
        {
            "object_types": [
                {"name": "Customer", "source_id_field": "id", "key_confidence": 0.95},
                {"name": "Order", "source_id_field": "id", "key_confidence": 0.9},
            ],
            "objects": [],
            "links": [],
        }
    )
    spec = assemble_discovery_pocket(draft, ["Customer", "Order"])

    assert spec["version"] == "1.0"
    # One fabric.objects state source per type, bound to that type by name.
    assert spec["state"]["rows_Customer"] == {
        "$source": "fabric.objects",
        "type_name": "Customer",
    }
    assert spec["state"]["rows_Order"] == {
        "$source": "fabric.objects",
        "type_name": "Order",
    }
    # One widget per type, each reading the matching state key.
    widget_ids = {c["id"] for c in spec["root"]["children"]}
    assert widget_ids == {"table_Customer", "table_Order"}
    rows_bindings = {c["props"]["rows"] for c in spec["root"]["children"]}
    assert rows_bindings == {"$state.rows_Customer", "$state.rows_Order"}


def test_assemble_discovery_pocket_skips_keyless_types() -> None:
    """A stand-alone assemble call applies the key-confidence gate itself —
    keyless / low-confidence types get no widget."""
    draft = OntologyDraft.model_validate(
        {
            "object_types": [
                {"name": "Customer", "source_id_field": "id", "key_confidence": 0.95},
                {"name": "Event", "source_id_field": "", "key_confidence": 0.1},
            ],
        }
    )
    spec = assemble_discovery_pocket(draft)  # no explicit materialised list
    state_keys = set(spec["state"].keys())
    assert state_keys == {"rows_Customer"}, state_keys


# ---------------------------------------------------------------------------
# A run files TWO proposals, both PENDING, both discovery-tagged.
# ---------------------------------------------------------------------------


async def test_run_files_two_tagged_pending_proposals(store, fabric):
    adapters = {
        "crm": _MockAdapter(
            schemas=[],
            data_by_action={"list_customers": _customers(3)},
        ),
    }
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # TWO proposals filed.
    assert result.fabric_objects_action_id is not None
    assert result.pocket_action_id is not None
    assert result.materialised_types == ["Customer"]
    assert result.skipped_types == {}
    assert result.superseded_action_ids == []  # first run supersedes nothing

    fabric_action = await store.get_action(result.fabric_objects_action_id)
    pocket_action = await store.get_action(result.pocket_action_id)
    assert fabric_action.status == ActionStatus.PENDING
    assert pocket_action.status == ActionStatus.PENDING

    # Both blobs carry the SHARED discovery-run marker (same run_id, distinct roles).
    fo_marker = _find_discovery_marker(fabric_action.parameters)
    pc_marker = _find_discovery_marker(pocket_action.parameters)
    assert fo_marker is not None and pc_marker is not None
    assert fo_marker["run_id"] == pc_marker["run_id"] == result.run_id
    assert fo_marker["role"] == "fabric_objects"
    assert pc_marker["role"] == "pocket_create"

    # The fabric proposal staged the 3 customers with a stable synthetic dedup key.
    fo_blob = fabric_action.parameters["_fabric_objects"]
    assert len(fo_blob["objects"]) == 3
    assert all(o["source_connector"] == "discovery:Customer" for o in fo_blob["objects"])
    assert {o["source_id"] for o in fo_blob["objects"]} == {"cust-0", "cust-1", "cust-2"}

    # The pocket proposal staged a spec binding the fabric.objects source.
    spec = pocket_action.parameters["_pocket_create"]["pocket_spec"]["rippleSpec"]
    assert spec["state"]["rows_Customer"] == {
        "$source": "fabric.objects",
        "type_name": "Customer",
    }


# ---------------------------------------------------------------------------
# A low-confidence keyless type is skipped + flagged (no crash).
# ---------------------------------------------------------------------------


async def test_low_confidence_keyless_type_skipped_and_flagged(store, fabric):
    adapters = {
        "mixed": _MockAdapter(
            schemas=[],
            data_by_action={
                "list_customers": _customers(3),  # keyed → materialised
                "list_events": _events(4),  # keyless → skipped + flagged
            },
        ),
    }
    opts = _opts_for(
        {
            "mixed": [
                ReadAction(action="list_customers", type_name="Customer"),
                ReadAction(action="list_events", type_name="Event"),
            ]
        }
    )

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["mixed"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # Customer materialised; Event skipped + flagged with a human reason.
    assert result.materialised_types == ["Customer"]
    assert "Event" in result.skipped_types
    assert "key" in result.skipped_types["Event"].lower()

    # The fabric proposal carries ONLY the Customer objects (no keyless Event row).
    fo_blob = (await store.get_action(result.fabric_objects_action_id)).parameters[
        "_fabric_objects"
    ]
    staged_types = {o["type_name"] for o in fo_blob["objects"]}
    assert staged_types == {"Customer"}

    # The starter pocket binds ONLY the Customer source.
    spec = (await store.get_action(result.pocket_action_id)).parameters["_pocket_create"][
        "pocket_spec"
    ]["rippleSpec"]
    assert set(spec["state"].keys()) == {"rows_Customer"}


# ---------------------------------------------------------------------------
# Approving BOTH proposals materialises the objects + creates the bound pocket.
# ---------------------------------------------------------------------------


async def test_approving_both_creates_objects_and_bound_pocket(store, fabric, beanie_test_db):
    from pocketpaw_ee.cloud.fabric_proposals import executor as fo_executor
    from pocketpaw_ee.cloud.pocket_proposals import executor as pc_executor
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    adapters = {
        "crm": _MockAdapter(
            schemas=[],
            data_by_action={"list_customers": _customers(2)},
        ),
    }
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # Approve + execute the FABRIC proposal → objects land in Fabric.
    approved_fo = await store.approve(result.fabric_objects_action_id, approver="u1")
    await fo_executor.execute_approved_fabric_objects(approved_fo)
    final_fo = await store.get_action(result.fabric_objects_action_id)
    assert final_fo.status == ActionStatus.EXECUTED

    from pocketpaw.fabric.models import FabricQuery

    cust_rows = await fabric.query(FabricQuery(type_name="Customer"), workspace_id="w1")
    assert len(cust_rows.objects) == 2
    assert {o.source_id for o in cust_rows.objects} == {"cust-0", "cust-1"}

    # Approve + execute the POCKET proposal → a Pocket whose spec references the
    # fabric.objects source for the discovered type is created.
    approved_pc = await store.approve(result.pocket_action_id, approver="u1")
    await pc_executor.execute_approved_pocket_create(approved_pc)
    final_pc = await store.get_action(result.pocket_action_id)
    assert final_pc.status == ActionStatus.EXECUTED, final_pc.error

    pocket_id = final_pc.parameters["_pocket_create"]["outcome"]["pocket_id"]
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["workspace"] == "w1"
    # pockets.service.get RESOLVES the $source marker into live Fabric rows on
    # read — so the created pocket's Customer state binding now carries the two
    # discovered objects we just materialised. This is the end-to-end proof: the
    # starter pocket's fabric.objects source surfaces the discovered Fabric data.
    spec = wire.get("rippleSpec") or wire.get("ripple_spec") or {}
    resolved_rows = spec.get("state", {}).get("rows_Customer")
    assert isinstance(resolved_rows, list), f"expected resolved rows, got {resolved_rows!r}"
    assert {r["source_id"] for r in resolved_rows} == {"cust-0", "cust-1"}
    assert all(r["type_name"] == "Customer" for r in resolved_rows)
    # The raw spec stored on the doc still carries the verbatim marker (resolution
    # happens on read, not at persistence) — confirm the binding is durable.
    raw_pocket_blob = final_pc.parameters["_pocket_create"]["pocket_spec"]["rippleSpec"]
    assert raw_pocket_blob["state"]["rows_Customer"] == {
        "$source": "fabric.objects",
        "type_name": "Customer",
    }


# ---------------------------------------------------------------------------
# A second run SUPERSEDES the first run's still-open pair (no duplicate stack).
# ---------------------------------------------------------------------------


async def test_second_run_supersedes_prior_open_pair(store, fabric):
    adapters = {
        "crm": _MockAdapter(schemas=[], data_by_action={"list_customers": _customers(2)}),
    }
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    first = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )
    # Both first-run proposals are PENDING.
    assert (await store.get_action(first.fabric_objects_action_id)).status == ActionStatus.PENDING
    assert (await store.get_action(first.pocket_action_id)).status == ActionStatus.PENDING

    second = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # The first run's pair was superseded (REJECTED), not left stacked.
    assert set(second.superseded_action_ids) == {
        first.fabric_objects_action_id,
        first.pocket_action_id,
    }
    first_fo = await store.get_action(first.fabric_objects_action_id)
    first_pc = await store.get_action(first.pocket_action_id)
    assert first_fo.status == ActionStatus.REJECTED
    assert first_pc.status == ActionStatus.REJECTED
    assert "superseded" in (first_fo.rejected_reason or "").lower()

    # The SECOND run's pair is the only open pair now.
    second_fo = await store.get_action(second.fabric_objects_action_id)
    second_pc = await store.get_action(second.pocket_action_id)
    assert second_fo.status == ActionStatus.PENDING
    assert second_pc.status == ActionStatus.PENDING

    pending = await store.list_actions(
        pocket_id="w1", status=ActionStatus.PENDING, workspace_id="w1"
    )
    pending_ids = {a.id for a in pending}
    assert pending_ids == {
        second.fabric_objects_action_id,
        second.pocket_action_id,
    }


# ---------------------------------------------------------------------------
# Tenancy validation at entry.
# ---------------------------------------------------------------------------


async def test_requires_workspace_and_user(store, fabric):
    with pytest.raises(ValueError, match="workspace_id"):
        await run_discovery_and_propose(
            workspace_id="", user_id="u1", connector_ids=[], discovery_run=_mock_run({})
        )
    with pytest.raises(ValueError, match="user_id"):
        await run_discovery_and_propose(
            workspace_id="w1", user_id="", connector_ids=[], discovery_run=_mock_run({})
        )


# ---------------------------------------------------------------------------
# An empty draft (no high-confidence objects) files NO proposals but still
# supersedes the prior open pair.
# ---------------------------------------------------------------------------


async def test_empty_draft_files_no_proposals(store, fabric):
    adapters = {"empty": _MockAdapter(schemas=[], data_by_action={"list_rows": []})}
    opts = _opts_for({"empty": [ReadAction(action="list_rows", type_name="Thing")]})

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["empty"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    assert result.fabric_objects_action_id is None
    assert result.pocket_action_id is None
    assert result.materialised_types == []
    # No PENDING discovery proposals were filed.
    pending = await store.list_actions(
        pocket_id="w1", status=ActionStatus.PENDING, workspace_id="w1"
    )
    assert pending == []


# ---------------------------------------------------------------------------
# The draft → fabric-proposal mapping carries links between materialised types.
# ---------------------------------------------------------------------------


def test_draft_to_fabric_mapping_keeps_links_between_materialised_types() -> None:
    """Links between two high-confidence types survive the mapping, with both
    endpoints rewritten to the synthetic discovery connector namespace."""
    from pocketpaw_ee.discovery.digester import StructuredShapeDigester

    grouped = {
        "Invoice": [
            {"id": f"inv-{i}", "amount": i * 10, "customer_id": f"cust-{i % 2}"} for i in range(4)
        ],
        "Customer": [{"id": f"cust-{i}", "name": f"C{i}"} for i in range(2)],
    }
    draft = StructuredShapeDigester().digest(grouped, {})
    object_types, objects, links, materialised, skipped = _draft_to_fabric_proposal_kwargs(draft)

    assert set(materialised) == {"Invoice", "Customer"}
    assert skipped == {}
    assert links, "expected an inferred Invoice→Customer link to survive mapping"
    for link in links:
        assert link["from"]["source_connector"] == "discovery:Invoice"
        assert link["to"]["source_connector"] == "discovery:Customer"
        assert link["from"]["source_id"] and link["to"]["source_id"]
        assert link["link_type"]
