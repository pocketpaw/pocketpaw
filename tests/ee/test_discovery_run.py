# tests/ee/test_discovery_run.py — unit tests for SZD-4 (DiscoveryRun).
#
# Created: 2026-06-19 (SZD-4 / feat/szd-4-discovery-run) — covers the
# DiscoveryRun orchestrator with the connector read MOCKED (no live network):
#   * a run against a mock connector yields an OntologyDraft (types inferred);
#   * the read goes through the workspace-scoped path
#     ``ensure_connected(name, "ws:<workspace_id>")`` (pocket-less) and NEVER
#     ``adapter.sync()`` — both asserted via the mock's recorded calls;
#   * sampling is capped per connector (the "N of M" budget);
#   * an empty connector yields an empty draft with no crash;
#   * explicit read-action overrides + a multi-connector run roll up provenance.
#
# Fully mocked — no DB / network. Run with:
#   uv run --group ee pytest tests/ee/test_discovery_run.py -q

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pocketpaw_ee.discovery import (
    DiscoveryRun,
    DiscoveryRunOptions,
    OntologyDraft,
    ReadAction,
)


# --------------------------------------------------------------------------- #
# Mock connector surface (stands in for the real adapter + registry).
# --------------------------------------------------------------------------- #
@dataclass
class _MockActionResult:
    """Mirror of pocketpaw.connectors.protocol.ActionResult (duck-typed)."""

    success: bool
    data: Any = None
    error: str | None = None
    records_affected: int = 0


@dataclass
class _MockActionSchema:
    """Mirror of pocketpaw.connectors.protocol.ActionSchema (the read-shape bits)."""

    name: str
    method: str = "GET"
    trust_level: str = "auto"


class _MockAdapter:
    """A connector adapter the orchestrator can read through.

    Records every ``execute`` call so the test can assert the read path. It has
    a ``sync`` method that BLOWS UP if called — discovery must never touch it.
    """

    def __init__(self, schemas: list[_MockActionSchema], data_by_action: dict[str, Any]) -> None:
        self._schemas = schemas
        self._data_by_action = data_by_action
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.sync_called = False

    async def actions(self) -> list[_MockActionSchema]:
        return list(self._schemas)

    async def execute(self, action: str, params: dict[str, Any]) -> _MockActionResult:
        self.execute_calls.append((action, dict(params)))
        if action not in self._data_by_action:
            return _MockActionResult(success=False, error=f"unknown action {action}")
        return _MockActionResult(success=True, data=self._data_by_action[action])

    async def sync(self, pocket_id: str) -> Any:  # pragma: no cover - must never run
        self.sync_called = True
        raise AssertionError(
            "DiscoveryRun must NOT call adapter.sync() — sync requires a pocket "
            "and returns counts, not records."
        )


class _MockRegistry:
    """A connector registry the orchestrator resolves adapters through.

    Records every ``ensure_connected(name, scope_key)`` so the test can assert
    the scope key is ``ws:<workspace_id>`` (the pocket-less #1445 path).
    """

    def __init__(self, adapters: dict[str, _MockAdapter | None]) -> None:
        self._adapters = adapters
        self.ensure_calls: list[tuple[str, str]] = []

    async def ensure_connected(self, connector_name: str, scope_key: str) -> _MockAdapter | None:
        self.ensure_calls.append((connector_name, scope_key))
        return self._adapters.get(connector_name)


def _records(n: int, prefix: str = "r") -> list[dict[str, Any]]:
    """N uniform records with a clean ``id`` key (so a primary key is inferred)."""
    return [{"id": f"{prefix}{i}", "name": f"name-{i}", "amount": i * 10} for i in range(n)]


# --------------------------------------------------------------------------- #
# A run yields an OntologyDraft via the workspace execute path.
# --------------------------------------------------------------------------- #
async def test_run_yields_draft_via_workspace_execute_path() -> None:
    adapter = _MockAdapter(
        schemas=[_MockActionSchema(name="list_invoices", method="GET", trust_level="auto")],
        data_by_action={"list_invoices": _records(3)},
    )
    registry = _MockRegistry({"billing": adapter})
    run = DiscoveryRun(registry=registry)

    draft = await run.run("ws-123", ["billing"])

    # 1. It produced a real OntologyDraft with an inferred type.
    assert isinstance(draft, OntologyDraft)
    assert not draft.is_empty
    assert len(draft.objects) == 3
    assert draft.object_types, "expected at least one inferred object type"

    # 2. The read went through ensure_connected with the WORKSPACE-scoped,
    #    pocket-less key — NOT a pocket:<id> key, NOT sync().
    assert registry.ensure_calls == [("billing", "ws:ws-123")]
    for _name, scope_key in registry.ensure_calls:
        assert scope_key.startswith("ws:"), f"expected ws-scoped key, got {scope_key!r}"
        assert not scope_key.startswith("pocket:"), "discovery must be pocket-less"
    assert adapter.sync_called is False, "discovery must never call adapter.sync()"
    assert adapter.execute_calls == [("list_invoices", {})]

    # 3. Provenance label is present ("based on N of M").
    discovery = draft.meta["discovery"]
    assert discovery["label"] == "based on 1 of 1"
    assert discovery["sampled_connectors"] == 1
    assert discovery["total_connectors"] == 1


# --------------------------------------------------------------------------- #
# Sampling is capped per connector.
# --------------------------------------------------------------------------- #
async def test_sampling_is_capped_per_connector() -> None:
    adapter = _MockAdapter(
        schemas=[_MockActionSchema(name="list_rows", method="GET", trust_level="auto")],
        data_by_action={"list_rows": _records(500)},
    )
    registry = _MockRegistry({"big": adapter})
    run = DiscoveryRun(registry=registry)

    draft = await run.run("ws-1", ["big"], DiscoveryRunOptions(sample_cap=50))

    # Only the capped number of records are sampled.
    assert len(draft.objects) == 50
    assert draft.meta["discovery"]["connectors"]["big"]["records"] == 50
    assert draft.meta["discovery"]["sample_cap"] == 50


# --------------------------------------------------------------------------- #
# Empty connector → empty draft, no crash.
# --------------------------------------------------------------------------- #
async def test_empty_connector_yields_empty_draft() -> None:
    adapter = _MockAdapter(
        schemas=[_MockActionSchema(name="list_rows", method="GET", trust_level="auto")],
        data_by_action={"list_rows": []},  # connector has no records
    )
    registry = _MockRegistry({"empty": adapter})
    run = DiscoveryRun(registry=registry)

    draft = await run.run("ws-1", ["empty"])

    assert isinstance(draft, OntologyDraft)
    assert draft.is_empty
    assert draft.meta["discovery"]["connectors"]["empty"]["status"] == "empty"
    assert draft.meta["discovery"]["label"] == "based on 1 of 1"
    # The read was still attempted through the ws path.
    assert registry.ensure_calls == [("empty", "ws:ws-1")]
    assert adapter.sync_called is False


# --------------------------------------------------------------------------- #
# Unresolvable connector is skipped gracefully.
# --------------------------------------------------------------------------- #
async def test_unresolved_connector_is_skipped() -> None:
    registry = _MockRegistry({"gone": None})  # ensure_connected returns None
    run = DiscoveryRun(registry=registry)

    draft = await run.run("ws-1", ["gone"])

    assert draft.is_empty
    assert draft.meta["discovery"]["connectors"]["gone"]["status"] == "unresolved"
    assert registry.ensure_calls == [("gone", "ws:ws-1")]


# --------------------------------------------------------------------------- #
# Mutating actions (POST / confirm) are never sampled on auto-select.
# --------------------------------------------------------------------------- #
async def test_auto_select_skips_mutating_actions() -> None:
    adapter = _MockAdapter(
        schemas=[
            _MockActionSchema(name="create_row", method="POST", trust_level="confirm"),
            _MockActionSchema(name="delete_row", method="POST", trust_level="auto"),
            _MockActionSchema(name="list_rows", method="GET", trust_level="auto"),
        ],
        data_by_action={"list_rows": _records(2), "create_row": _records(2)},
    )
    registry = _MockRegistry({"c": adapter})
    run = DiscoveryRun(registry=registry)

    await run.run("ws-1", ["c"])

    # Only the GET + auto action fired; the POST actions never ran.
    called = {a for a, _ in adapter.execute_calls}
    assert called == {"list_rows"}


# --------------------------------------------------------------------------- #
# Explicit read-action override + multi-connector "N of M" roll-up.
# --------------------------------------------------------------------------- #
async def test_explicit_read_actions_and_multi_connector_rollup() -> None:
    crm = _MockAdapter(
        schemas=[],  # no auto-discoverable schema — forces the explicit path
        data_by_action={"q_customers": _records(2, "cust"), "q_deals": _records(2, "deal")},
    )
    support = _MockAdapter(
        schemas=[_MockActionSchema(name="list_tickets", method="GET", trust_level="auto")],
        data_by_action={"list_tickets": _records(4, "tkt")},
    )
    registry = _MockRegistry({"crm": crm, "support": support, "broken": None})
    run = DiscoveryRun(registry=registry)

    opts = DiscoveryRunOptions(
        read_actions={
            "crm": [
                ReadAction(action="q_customers", type_name="Customer"),
                ReadAction(action="q_deals", type_name="Deal"),
            ],
        },
    )
    draft = await run.run("ws-9", ["crm", "support", "broken"], opts)

    # Three logical types: Customer + Deal (explicit) + list_tickets (auto).
    type_names = {ot.name for ot in draft.object_types}
    assert {"Customer", "Deal", "list_tickets"} <= type_names

    # 2 of 3 connectors actually sampled (broken was unresolved).
    discovery = draft.meta["discovery"]
    assert discovery["sampled_connectors"] == 2
    assert discovery["total_connectors"] == 3
    assert discovery["label"] == "based on 2 of 3"

    # Every read went through a ws:-scoped resolve; none used sync().
    assert all(scope == "ws:ws-9" for _n, scope in registry.ensure_calls)
    assert crm.sync_called is False
    assert support.sync_called is False


# --------------------------------------------------------------------------- #
# Permission filter degrades gracefully (allow-all when no list given).
# --------------------------------------------------------------------------- #
async def test_permission_filter_skips_disallowed_connectors() -> None:
    a = _MockAdapter(
        schemas=[_MockActionSchema(name="list_rows", trust_level="auto")],
        data_by_action={"list_rows": _records(2)},
    )
    b = _MockAdapter(
        schemas=[_MockActionSchema(name="list_rows", trust_level="auto")],
        data_by_action={"list_rows": _records(2)},
    )
    registry = _MockRegistry({"allowed": a, "denied": b})
    run = DiscoveryRun(registry=registry)

    opts = DiscoveryRunOptions(allowed_connector_ids=frozenset({"allowed"}))
    draft = await run.run("ws-1", ["allowed", "denied"], opts)

    discovery = draft.meta["discovery"]
    assert discovery["permission_enforced"] is True
    assert discovery["skipped_by_permission"] == ["denied"]
    # The denied connector was never resolved or read.
    assert registry.ensure_calls == [("allowed", "ws:ws-1")]
    assert b.execute_calls == []


# --------------------------------------------------------------------------- #
# The on-box refine pass is WIRED in F3 — opts.refine=True runs the deterministic
# digest, then routes it through the on-box refine helper (no longer raises).
# The deep sovereignty/availability assertions live in test_discovery_refine.py;
# here we just prove run() delegates to _refine.refine_draft and never raises.
# --------------------------------------------------------------------------- #
async def test_refine_pass_delegates_to_on_box_refine(monkeypatch) -> None:
    from pocketpaw_ee.discovery import _refine

    adapter = _MockAdapter(
        schemas=[_MockActionSchema(name="list_rows", trust_level="auto")],
        data_by_action={"list_rows": _records(2)},
    )
    registry = _MockRegistry({"c": adapter})
    run = DiscoveryRun(registry=registry)

    seen: dict[str, Any] = {}

    async def _fake_refine(draft: OntologyDraft, settings: Any) -> OntologyDraft:
        seen["called"] = True
        draft.meta["refine"] = "applied"
        return draft

    monkeypatch.setattr(_refine, "refine_draft", _fake_refine)

    draft = await run.run("ws-1", ["c"], DiscoveryRunOptions(refine=True))

    assert seen.get("called") is True, "refine=True must delegate to _refine.refine_draft"
    assert draft.meta["refine"] == "applied"
    # The deterministic provenance is still stamped underneath the refine pass.
    assert draft.meta["discovery"]["label"] == "based on 1 of 1"
