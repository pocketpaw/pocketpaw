# tests/ee/test_fabric_registry.py
# Created: 2026-05-28 (feat/wave-4c-fabric-registry) — RED-first tests
# for ``WorkspaceFabricRegistry``, the concrete EE-side implementation
# of the ``pocketpaw.bundled_templates.FabricRegistry`` Protocol that
# PR 2g defined. The registry is a thin read-side wrapper over
# ``WorkspaceFabricStore`` bound to a single workspace; mutations go
# through the store directly. Coverage: per-method behaviour against a
# populated store, empty-store defaults, workspace isolation, and
# runtime Protocol conformance.
"""Tests for ``pocketpaw_ee.fabric.registry.WorkspaceFabricRegistry``.

The registry satisfies the :class:`FabricRegistry` Protocol declared in
``pocketpaw.bundled_templates.fabric_registry`` and feeds the Wave 4b
lint path (``validate_template_with_registry``) and the runtime
``FabricResolver``. Each registry instance is bound to a single
``workspace_id``; the underlying store handles the multi-tenant table
filter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.fabric import WorkspaceFabricRegistry, WorkspaceFabricStore

from pocketpaw.bundled_templates import FabricRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> WorkspaceFabricStore:
    return WorkspaceFabricStore(tmp_path / "fabric_registry.db")


@pytest.fixture()
def populated_store(store: WorkspaceFabricStore) -> WorkspaceFabricStore:
    """Pre-seed two workspaces. ``ws-a`` carries the lease ontology;
    ``ws-b`` carries a distinct invoice ontology so isolation tests have
    something concrete to assert against."""
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-a", "Tenant")
    store.register_entity_type("ws-a", "Property")
    store.register_property("ws-a", "Lease", "expiry_date", "date")
    store.register_property("ws-a", "Lease", "rent_current", "number")
    store.register_property("ws-a", "Tenant", "name", "string")
    store.register_link("ws-a", "lease_tenant", "Lease", "Tenant")
    store.register_link("ws-a", "lease_property", "Lease", "Property")

    store.register_entity_type("ws-b", "Invoice")
    store.register_entity_type("ws-b", "Customer")
    store.register_property("ws-b", "Invoice", "due_date", "date")
    store.register_link("ws-b", "invoice_customer", "Invoice", "Customer")
    return store


# ---------------------------------------------------------------------------
# Protocol surface — populated workspace
# ---------------------------------------------------------------------------


def test_entity_type_exists_returns_true_for_known(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    assert reg.entity_type_exists("Lease") is True
    assert reg.entity_type_exists("Tenant") is True


def test_entity_type_exists_returns_false_for_unknown(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    assert reg.entity_type_exists("Ghost") is False


def test_link_exists_returns_true_for_registered(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    assert reg.link_exists("Lease", "Tenant", "lease_tenant") is True
    assert reg.link_exists("Lease", "Property", "lease_property") is True


def test_link_exists_returns_false_for_unregistered(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    # Wrong name.
    assert reg.link_exists("Lease", "Tenant", "wrong_link") is False
    # Wrong direction.
    assert reg.link_exists("Tenant", "Lease", "lease_tenant") is False
    # Unknown endpoint.
    assert reg.link_exists("Ghost", "Tenant", "lease_tenant") is False


def test_get_entity_properties_returns_declared_props(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    assert reg.get_entity_properties("Lease") == {"expiry_date", "rent_current"}
    assert reg.get_entity_properties("Tenant") == {"name"}


def test_get_entity_properties_for_unknown_returns_empty(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    assert reg.get_entity_properties("Ghost") == set()


# ---------------------------------------------------------------------------
# Empty store
# ---------------------------------------------------------------------------


def test_empty_store_reports_no_entities(store: WorkspaceFabricStore) -> None:
    reg = WorkspaceFabricRegistry(store=store, workspace_id="ws-empty")
    assert reg.entity_type_exists("Anything") is False
    assert reg.link_exists("A", "B", "link") is False
    assert reg.get_entity_properties("X") == set()


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


def test_registry_bound_to_workspace_a_does_not_see_workspace_b(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg_a = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    # ws-a has Lease but not Invoice.
    assert reg_a.entity_type_exists("Lease") is True
    assert reg_a.entity_type_exists("Invoice") is False
    assert reg_a.link_exists("Invoice", "Customer", "invoice_customer") is False
    assert reg_a.get_entity_properties("Invoice") == set()


def test_registry_bound_to_workspace_b_does_not_see_workspace_a(
    populated_store: WorkspaceFabricStore,
) -> None:
    reg_b = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-b")
    assert reg_b.entity_type_exists("Invoice") is True
    assert reg_b.entity_type_exists("Lease") is False
    assert reg_b.link_exists("Lease", "Tenant", "lease_tenant") is False
    assert reg_b.get_entity_properties("Lease") == set()


def test_registries_for_different_workspaces_share_store(
    populated_store: WorkspaceFabricStore,
) -> None:
    """Two registries sharing the same store but bound to different
    workspaces return disjoint views — the store does the filtering."""
    reg_a = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    reg_b = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-b")
    assert reg_a.entity_type_exists("Lease") is True
    assert reg_b.entity_type_exists("Lease") is False
    assert reg_a.entity_type_exists("Invoice") is False
    assert reg_b.entity_type_exists("Invoice") is True


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_runtime_protocol_check(store: WorkspaceFabricStore) -> None:
    """``isinstance(reg, FabricRegistry)`` must succeed at runtime so the
    EE wiring can assert the binding without importing
    ``WorkspaceFabricRegistry`` directly."""
    reg = WorkspaceFabricRegistry(store=store, workspace_id="ws-a")
    assert isinstance(reg, FabricRegistry)


def test_get_entity_properties_returns_independent_copy(
    populated_store: WorkspaceFabricStore,
) -> None:
    """Mutating the returned set must not affect the store's internal
    state — matches the contract of
    :class:`JSONFileFabricRegistry.get_entity_properties`."""
    reg = WorkspaceFabricRegistry(store=populated_store, workspace_id="ws-a")
    props = reg.get_entity_properties("Lease")
    props.add("ghost_property")
    assert reg.get_entity_properties("Lease") == {"expiry_date", "rent_current"}
