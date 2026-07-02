# tests/ee/test_szd2_e2e.py — S2-E1: the slice-2 backend end-to-end test.
#
# Created: 2026-06-20 (S2-E1 / feat/szd-slice2-discovery). This is the headline
# proof of slice 2: a SINGLE discovery run over UNSTRUCTURED tenant exhaust
# (ticket / email / chat bodies) produces THREE governed Instinct proposals —
#
#   1. ``_fabric_objects`` — the ontology the KbCompileDigester compiled on-box
#      (>=2 typed object types + >=1 concept-cooccurrence link) staged for Fabric;
#   2. ``_pocket_create`` — a starter dashboard bound to the ``fabric.objects``
#      source for each discovered type;
#   3. ``_instinct_rule`` — a governed rule the RuleDigester reverse-engineered
#      from the workspace's Instinct correction exhaust;
#
# all carrying the SAME ``run_id`` with distinct roles. The test then APPROVES all
# three through their real executors, asserts each reaches ``EXECUTED``, and proves
# materialisation: the Fabric objects exist (``FabricStore.query``, workspace
# scoped), the starter Pocket's ``fabric.objects`` ``$source`` RESOLVES to the
# discovered rows on read, and the governed rule LANDED via
# ``rules.service.get_active_rules``.
#
# SOVEREIGNTY (mandatory, the slice's headline guarantee): the ``_kb`` subprocess
# seam is mocked with an in-memory fake that RECORDS every argv. After the whole
# run the test asserts ``kb ingest`` and ``kb build`` were NEVER invoked — the two
# commands that POST raw tenant text to Anthropic (kb.go:1349). No tenant exhaust
# left the box; the entire ontology was compiled keyless + on-box.
#
# Backed by ``beanie_test_db`` (mongomock) + isolated InstinctStore / FabricStore
# via the lazy ``get_*_store`` seams + an inert recording bus (cloned from
# test_discovery_propose.py). The connector read is mocked (no live network); the
# kb-go binary is mocked (no subprocess, deterministic, sovereignty-checkable).
#
# Run with:
#   uv run --group ee pytest tests/ee/test_szd2_e2e.py -q

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.discovery import (  # noqa: E402
    DiscoveryRun,
    DiscoveryRunOptions,
    ReadAction,
    run_discovery_and_propose,
)
from pocketpaw_ee.discovery.kb_compile import KbCompileDigester  # noqa: E402
from pocketpaw_ee.discovery.orchestrate import _find_discovery_marker  # noqa: E402

from pocketpaw.fabric.store import FabricStore  # noqa: E402
from pocketpaw.instinct.correction import Correction, CorrectionPatch  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Mock connector surface (same shape as tests/ee/test_discovery_propose.py).
# For UNSTRUCTURED discovery each read action returns a list of TEXT bodies (the
# exhaust) rather than typed record dicts — the KbCompileDigester compiles them.
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


# ---------------------------------------------------------------------------
# The UNSTRUCTURED exhaust — raw ticket / email / chat bodies, no record shape.
# These are the text blobs the KbCompileDigester compiles on-box into articles.
# ---------------------------------------------------------------------------

_TICKET_BODIES = [
    "Customer cannot log in after a failed payment — billing lock triggered.",
    "Account locked following a declined card; the user is stuck at the login screen.",
    "Login lockout reported again after a billing failure on renewal.",
]

_REFUND_BODIES = [
    "Refund requested on invoice 12 — billing dispute, customer wants money back.",
    "The customer is asking for a refund tied to a duplicate billing charge.",
]


# ---------------------------------------------------------------------------
# The CANNED kb-go output. The mock `_kb` serves these CATEGORIZED articles back
# (the shape `kb show --json` returns): two object types (SupportTicket,
# RefundRequest) sharing the "billing" concept → a cross-type DraftLink. The
# article `id` is the natural primary key (key_confidence >= 0.8), so both types
# are MATERIALISABLE (pass the KEY_CONFIDENCE_FLOOR gate in orchestrate).
# ---------------------------------------------------------------------------

_COMPILED_ARTICLES = [
    {
        "id": "tkt-1",
        "title": "Login lockout after billing failure",
        "summary": "Customer cannot log in; billing lock triggered by a failed payment.",
        "concepts": ["billing", "login"],
        "categories": ["SupportTicket"],
    },
    {
        "id": "tkt-2",
        "title": "Account locked after declined card",
        "summary": "Declined card left the account locked at the login screen.",
        "concepts": ["billing", "login"],
        "categories": ["SupportTicket"],
    },
    {
        "id": "ref-1",
        "title": "Refund requested on invoice 12",
        "summary": "Refund request tied to a billing dispute on a duplicate charge.",
        "concepts": ["billing", "refund"],
        "categories": ["RefundRequest"],
    },
]

# kb graph: concept nodes + edges so concept co-occurrence yields a cross-type
# link. "billing" is shared across SupportTicket + RefundRequest articles.
_GRAPH_NODES = [
    {"id": "c0", "label": "billing", "kind": "concept", "size": 3},
    {"id": "c1", "label": "login", "kind": "concept", "size": 2},
    {"id": "c2", "label": "refund", "kind": "concept", "size": 1},
]
_GRAPH_EDGES = [
    {"source": "c0", "target": "c1", "weight": 2},
    {"source": "c0", "target": "c2", "weight": 1},
]


def _install_fake_kb(
    monkeypatch,
    *,
    articles: list[dict],
    nodes: list[dict],
    edges: list[dict],
) -> list[tuple[str, ...]]:
    """Replace the `_kb` subprocess seam with an in-memory, sovereignty-checkable
    fake that RECORDS every call's argv.

    Mirrors the fake in test_kb_compile_digester.py: the keyless on-box commands
    (``convo`` / ``accept`` / ``list`` / ``show`` / ``graph``) are served from an
    in-memory store keyed by ``--scope``; ``ingest`` / ``build`` (the off-box
    Anthropic-POSTing commands) are NEVER expected — the returned ``calls`` list
    is the evidence the sovereignty assertion checks at the end of the run.
    """
    calls: list[tuple[str, ...]] = []
    store: dict[str, list[dict]] = {}

    def _scope_of(args: tuple[str, ...]) -> str:
        if "--scope" in args:
            return args[args.index("--scope") + 1]
        return "default"

    def _fake_kb(*args, input_text=None, timeout=120):  # noqa: ARG001
        calls.append(args)
        scope = _scope_of(args)
        if args[0] in ("convo", "accept"):
            store.setdefault(scope, [])
            for a in articles:
                if a not in store[scope]:
                    store[scope].append(a)
            return {"articles": len(articles), "accepted": len(articles)}
        if args[0] == "list":
            return [
                {"id": a["id"], "title": a.get("title", ""), "summary": a.get("summary", "")}
                for a in store.get(scope, [])
            ]
        if args[0] == "show":
            article_id = args[1]
            for a in store.get(scope, []):
                if a["id"] == article_id:
                    return dict(a)
            return {}
        if args[0] == "graph":
            return {"scope": scope, "nodes": nodes, "edges": edges}
        return {}

    monkeypatch.setattr("pocketpaw_ee.discovery.kb_compile._kb", _fake_kb)
    return calls


# ---------------------------------------------------------------------------
# Correction exhaust — the rules-discovery signal. Three corrections on the SAME
# path with a single dominant ``after`` value clear the recurrence threshold AND
# carry a constant target, so the RuleDigester emits one high-confidence draft.
# Corrections anchor on ``pocket_id == workspace_id`` (the discovery non-pocket
# convention). Mirrors test_discovery_rules_propose.py.
# ---------------------------------------------------------------------------


def _strong_correction(workspace_id: str, idx: int) -> Correction:
    return Correction(
        action_id=f"act-{idx}",
        pocket_id=workspace_id,
        actor="u1",
        patches=[CorrectionPatch(path="category", before="normal", after="escalated")],
        context_summary=f"raised category #{idx}",
        action_title=f"Ticket #{idx}",
    )


async def _seed_corrections(store: InstinctStore, corrections: list[Correction]) -> None:
    for correction in corrections:
        await store.record_correction(correction)


# ---------------------------------------------------------------------------
# Fixtures — isolated stores + inert bus (cloned from test_discovery_propose.py).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "szd2-e2e-test-secret")


@pytest.fixture(autouse=True)
def recording_bus():
    """Inert recording EventBus so service ``emit()`` calls don't raise."""
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
    st = InstinctStore(tmp_path / "instinct_szd2_e2e.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.fixture
def fabric(tmp_path: Path, monkeypatch) -> FabricStore:
    fs = FabricStore(tmp_path / "fabric_szd2_e2e.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda *a, **k: fs)
    return fs


# ---------------------------------------------------------------------------
# The slice-2 end-to-end test.
# ---------------------------------------------------------------------------


async def test_szd2_unstructured_exhaust_to_three_governed_proposals(
    store, fabric, beanie_test_db, monkeypatch
):
    """UNSTRUCTURED exhaust → THREE proposals (ontology + starter pocket + governed
    rule) → approve all → discovered data + rule materialise — with a sovereignty
    assertion that no tenant exhaust ever left the box.

    This single test is the S2-E1 backend gate: it exercises the whole slice-2
    pipeline through the real ``run_discovery_and_propose`` orchestrator and the
    real apply-on-approve executors, with ONLY the two external seams faked (the
    connector read and the kb-go subprocess).
    """
    from pocketpaw_ee.cloud.fabric_proposals import executor as fo_executor
    from pocketpaw_ee.cloud.instinct_rule_proposals import (
        INSTINCT_RULE_PARAM_KEY,
        execute_approved_instinct_rule,
    )
    from pocketpaw_ee.cloud.pocket_proposals import executor as pc_executor
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.rules import service as rules_service

    from pocketpaw.fabric.models import FabricQuery

    workspace_id = "w1"
    user_id = "u1"

    # ── BUILD ITEM 1: mock the `_kb` seam → unstructured text compiles on-box into
    #    CATEGORIZED articles → a rich OntologyDraft (2 typed types + a link). The
    #    fake records every argv for the sovereignty assertion.
    kb_calls = _install_fake_kb(
        monkeypatch,
        articles=_COMPILED_ARTICLES,
        nodes=_GRAPH_NODES,
        edges=_GRAPH_EDGES,
    )

    # ── BUILD ITEM 2: seed correction exhaust so rules-discovery has a signal to
    #    mine (3x the same path with a constant target → one high-confidence rule).
    await _seed_corrections(store, [_strong_correction(workspace_id, i) for i in range(3)])

    # ── BUILD ITEM 3: run discovery with the KbCompileDigester over a mock adapter
    #    that returns the UNSTRUCTURED text bodies (grouped by read type label).
    adapters = {
        "support": _MockAdapter(
            schemas=[],
            data_by_action={
                "list_tickets": _TICKET_BODIES,
                "list_refunds": _REFUND_BODIES,
            },
        ),
    }
    opts = DiscoveryRunOptions(
        read_actions={
            "support": [
                ReadAction(action="list_tickets", type_name="tickets"),
                ReadAction(action="list_refunds", type_name="refunds"),
            ]
        }
    )
    discovery_run = DiscoveryRun(
        registry=_MockRegistry(adapters),
        digester=KbCompileDigester(),
    )

    result = await run_discovery_and_propose(
        workspace_id=workspace_id,
        user_id=user_id,
        connector_ids=["support"],
        opts=opts,
        discovery_run=discovery_run,
    )

    # ── BUILD ITEM 4: THREE PENDING proposals, each carrying the shared run_id with
    #    its distinct role.
    assert result.fabric_objects_action_id is not None, "expected a _fabric_objects proposal"
    assert result.pocket_action_id is not None, "expected a _pocket_create proposal"
    assert result.instinct_action_ids, "expected at least one _instinct_rule proposal"
    rule_action_id = result.instinct_action_ids[0]

    fabric_action = await store.get_action(result.fabric_objects_action_id)
    pocket_action = await store.get_action(result.pocket_action_id)
    rule_action = await store.get_action(rule_action_id)
    assert fabric_action.status == ActionStatus.PENDING
    assert pocket_action.status == ActionStatus.PENDING
    assert rule_action.status == ActionStatus.PENDING

    # The three proposal kinds are present, distinct, and share ONE run_id.
    assert "_fabric_objects" in fabric_action.parameters
    assert "_pocket_create" in pocket_action.parameters
    assert INSTINCT_RULE_PARAM_KEY in rule_action.parameters

    fo_marker = _find_discovery_marker(fabric_action.parameters)
    pc_marker = _find_discovery_marker(pocket_action.parameters)
    ir_marker = _find_discovery_marker(rule_action.parameters)
    assert fo_marker is not None and pc_marker is not None and ir_marker is not None
    assert fo_marker["run_id"] == pc_marker["run_id"] == ir_marker["run_id"] == result.run_id
    assert fo_marker["role"] == "fabric_objects"
    assert pc_marker["role"] == "pocket_create"
    assert ir_marker["role"] == "instinct_rules"

    # The compiled ontology carried >=2 typed object types + >=1 link into the
    # fabric proposal (the KbCompileDigester output materialised through the gate).
    fo_blob = fabric_action.parameters["_fabric_objects"]
    staged_types = {ot["type_name"] for ot in fo_blob["object_types"]}
    assert {"SupportTicket", "RefundRequest"} <= staged_types
    assert fo_blob.get("links"), "expected a concept-cooccurrence link in the ontology"

    # ── BUILD ITEM 5: APPROVE all three → real executors → each reaches EXECUTED.
    approved_fo = await store.approve(result.fabric_objects_action_id, approver=user_id)
    await fo_executor.execute_approved_fabric_objects(approved_fo)
    final_fo = await store.get_action(result.fabric_objects_action_id)
    assert final_fo.status == ActionStatus.EXECUTED, final_fo.error

    approved_pc = await store.approve(result.pocket_action_id, approver=user_id)
    await pc_executor.execute_approved_pocket_create(approved_pc)
    final_pc = await store.get_action(result.pocket_action_id)
    assert final_pc.status == ActionStatus.EXECUTED, final_pc.error

    approved_ir = await store.approve(rule_action_id, approver=user_id)
    await execute_approved_instinct_rule(approved_ir)
    final_ir = await store.get_action(rule_action_id)
    assert final_ir.status == ActionStatus.EXECUTED, final_ir.error

    # ── BUILD ITEM 6a: the discovered Fabric objects EXIST (workspace-scoped query).
    tickets = await fabric.query(FabricQuery(type_name="SupportTicket"), workspace_id=workspace_id)
    refunds = await fabric.query(FabricQuery(type_name="RefundRequest"), workspace_id=workspace_id)
    assert {o.source_id for o in tickets.objects} == {"tkt-1", "tkt-2"}
    assert {o.source_id for o in refunds.objects} == {"ref-1"}
    assert all(o.type_name == "SupportTicket" for o in tickets.objects)

    # ── BUILD ITEM 6b: the starter Pocket's fabric.objects $source RESOLVES to the
    #    discovered rows on read (pockets.service.get replaces the marker live).
    pocket_id = final_pc.parameters["_pocket_create"]["outcome"]["pocket_id"]
    wire = await pockets_service.get(pocket_id, user_id)
    assert wire["workspace"] == workspace_id
    spec = wire.get("rippleSpec") or wire.get("ripple_spec") or {}
    resolved_tickets = spec.get("state", {}).get("rows_SupportTicket")
    assert isinstance(resolved_tickets, list), f"expected resolved rows, got {resolved_tickets!r}"
    assert {r["source_id"] for r in resolved_tickets} == {"tkt-1", "tkt-2"}
    assert all(r["type_name"] == "SupportTicket" for r in resolved_tickets)
    # The raw stored spec still carries the verbatim $source marker (resolution is
    # on read, not at persistence) — the binding is durable across reloads.
    raw_spec = final_pc.parameters["_pocket_create"]["pocket_spec"]["rippleSpec"]
    assert raw_spec["state"]["rows_SupportTicket"] == {
        "$source": "fabric.objects",
        "type_name": "SupportTicket",
    }

    # ── BUILD ITEM 6c: the governed rule LANDED via the slice-2 read seam, scoped
    #    to the workspace, with the reverse-engineered when/action.
    active = await rules_service.get_active_rules(workspace_id)
    assert len(active) == 1, active
    landed = active[0]
    assert landed["workspace_id"] == workspace_id
    assert landed["owner_user_id"] == user_id
    assert landed["scope"]["workspace_id"] == workspace_id
    assert landed["status"] == "active"
    # The rule's CEL trigger was reverse-engineered from the corrected ``category``
    # path, and its action is a valid gate disposition.
    assert "category" in landed["when"]
    assert landed["action"] in ("require_approval", "notify", "block")

    # ── BUILD ITEM 7: SOVEREIGNTY ASSERTION (mandatory) — across the WHOLE run the
    #    off-box, Anthropic-POSTing kb commands were NEVER invoked. No tenant
    #    exhaust left the box; the entire ontology was compiled keyless + on-box.
    assert kb_calls, "expected the digester to drive the kb seam at least once"
    assert all(c[0] not in ("ingest", "build") for c in kb_calls), (
        "sovereignty violation: KbCompileDigester invoked an off-box kb command "
        f"(ingest/build) — argv seen: {[c[0] for c in kb_calls]}"
    )
    # and at least one KEYLESS on-box compile actually ran.
    assert any(c[0] in ("convo", "accept") for c in kb_calls)
