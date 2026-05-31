# tests/ee/test_fabric_storage.py
# Created: 2026-05-28 (feat/wave-4c-fabric-registry) — RED-first tests
# for ``WorkspaceFabricStore``, the EE-side workspace-scoped SQLite
# store that backs the concrete ``FabricRegistry`` Protocol
# implementation. The store registers entity types, properties, and
# links for the Wave 4b ``tier: registered`` lint contract. Coverage:
# round-trip, cascade delete, workspace isolation, and a smoke test
# wiring the populated store through ``WorkspaceFabricRegistry`` into
# the Wave 4b validator against ``lease-renewal-v2.yaml``.
"""Tests for ``pocketpaw_ee.fabric.storage.WorkspaceFabricStore``.

The store is the write-side; ``WorkspaceFabricRegistry`` (covered in
``test_fabric_registry.py``) is the read-side Protocol implementation.
Both live under ``ee/pocketpaw_ee/fabric/``. The split mirrors the
Beanie domain/service pattern adapted to SQLite — service writes, view
reads.

Storage shape (locked in this PR):

* Single shared SQLite file (default ``~/.pocketpaw/fabric_registry.db``).
* Every row carries a ``workspace`` column; every read filters on it.
* Three tables — ``fabric_entity_types``, ``fabric_entity_properties``,
  ``fabric_registry_links``.

The store is sync (stdlib ``sqlite3``); callers wrap in
``asyncio.to_thread`` if they need an async surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pocketpaw_ee.fabric import WorkspaceFabricRegistry, WorkspaceFabricStore

from pocketpaw.bundled_templates import (
    PocketTemplate,
    validate_template_with_registry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> WorkspaceFabricStore:
    """Fresh store backed by a tmp-path SQLite file. Each test gets its
    own DB — implicit teardown when ``tmp_path`` unwinds."""
    return WorkspaceFabricStore(tmp_path / "fabric_registry.db")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_register_and_list_entity_types(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-a", "Tenant")
    store.register_entity_type("ws-a", "Property")
    assert sorted(store.list_entity_types("ws-a")) == ["Lease", "Property", "Tenant"]


def test_entity_exists_true_after_register(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    assert store.entity_exists("ws-a", "Lease") is True
    assert store.entity_exists("ws-a", "Ghost") is False


def test_register_entity_type_is_idempotent(store: WorkspaceFabricStore) -> None:
    """Re-registering an existing type is a no-op (INSERT OR IGNORE)."""
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-a", "Lease")
    assert store.list_entity_types("ws-a") == ["Lease"]


def test_register_and_get_properties(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_property("ws-a", "Lease", "expiry_date", "date")
    store.register_property("ws-a", "Lease", "rent_current", "number")
    store.register_property("ws-a", "Lease", "rent_proposed", "number")
    assert store.get_properties("ws-a", "Lease") == {
        "expiry_date",
        "rent_current",
        "rent_proposed",
    }


def test_register_property_replaces_on_conflict(store: WorkspaceFabricStore) -> None:
    """Re-registering the same property updates its type (INSERT OR
    REPLACE) rather than failing."""
    store.register_entity_type("ws-a", "Lease")
    store.register_property("ws-a", "Lease", "expiry_date", "date")
    store.register_property("ws-a", "Lease", "expiry_date", "string")
    assert store.get_properties("ws-a", "Lease") == {"expiry_date"}


def test_get_properties_for_unknown_entity_returns_empty(
    store: WorkspaceFabricStore,
) -> None:
    assert store.get_properties("ws-a", "Ghost") == set()


def test_register_and_link_exists(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-a", "Tenant")
    store.register_link("ws-a", "lease_tenant", "Lease", "Tenant")
    assert store.link_exists("ws-a", "lease_tenant", "Lease", "Tenant") is True
    # Direction matters — reverse is not implied.
    assert store.link_exists("ws-a", "lease_tenant", "Tenant", "Lease") is False
    # Different link name on the same pair → not registered.
    assert store.link_exists("ws-a", "other_name", "Lease", "Tenant") is False


def test_register_link_is_idempotent(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-a", "Tenant")
    store.register_link("ws-a", "lease_tenant", "Lease", "Tenant")
    store.register_link("ws-a", "lease_tenant", "Lease", "Tenant")
    # Sanity — single row, still exists.
    assert store.link_exists("ws-a", "lease_tenant", "Lease", "Tenant") is True


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


def test_entity_types_isolated_by_workspace(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-b", "Invoice")

    assert store.list_entity_types("ws-a") == ["Lease"]
    assert store.list_entity_types("ws-b") == ["Invoice"]
    assert store.entity_exists("ws-a", "Invoice") is False
    assert store.entity_exists("ws-b", "Lease") is False


def test_properties_isolated_by_workspace(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-b", "Lease")  # same name, different ws
    store.register_property("ws-a", "Lease", "expiry_date", "date")
    store.register_property("ws-b", "Lease", "renewal_id", "string")

    assert store.get_properties("ws-a", "Lease") == {"expiry_date"}
    assert store.get_properties("ws-b", "Lease") == {"renewal_id"}


def test_links_isolated_by_workspace(store: WorkspaceFabricStore) -> None:
    for ws in ("ws-a", "ws-b"):
        store.register_entity_type(ws, "Lease")
        store.register_entity_type(ws, "Tenant")
    store.register_link("ws-a", "lease_tenant", "Lease", "Tenant")

    assert store.link_exists("ws-a", "lease_tenant", "Lease", "Tenant") is True
    assert store.link_exists("ws-b", "lease_tenant", "Lease", "Tenant") is False


# ---------------------------------------------------------------------------
# Cascade delete
# ---------------------------------------------------------------------------


def test_delete_entity_type_cascades_to_properties(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_property("ws-a", "Lease", "expiry_date", "date")
    store.register_property("ws-a", "Lease", "rent_current", "number")

    store.delete_entity_type("ws-a", "Lease")

    assert store.entity_exists("ws-a", "Lease") is False
    assert store.get_properties("ws-a", "Lease") == set()


def test_delete_entity_type_cascades_to_links(store: WorkspaceFabricStore) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-a", "Tenant")
    store.register_entity_type("ws-a", "Property")
    store.register_link("ws-a", "lease_tenant", "Lease", "Tenant")
    store.register_link("ws-a", "lease_property", "Lease", "Property")
    # Link pointing INTO the deleted type also drops.
    store.register_link("ws-a", "tenant_to_lease", "Tenant", "Lease")

    store.delete_entity_type("ws-a", "Lease")

    assert store.link_exists("ws-a", "lease_tenant", "Lease", "Tenant") is False
    assert store.link_exists("ws-a", "lease_property", "Lease", "Property") is False
    assert store.link_exists("ws-a", "tenant_to_lease", "Tenant", "Lease") is False


def test_delete_entity_type_does_not_touch_other_workspace(
    store: WorkspaceFabricStore,
) -> None:
    store.register_entity_type("ws-a", "Lease")
    store.register_entity_type("ws-b", "Lease")
    store.register_property("ws-a", "Lease", "x", "string")
    store.register_property("ws-b", "Lease", "y", "string")

    store.delete_entity_type("ws-a", "Lease")

    assert store.entity_exists("ws-b", "Lease") is True
    assert store.get_properties("ws-b", "Lease") == {"y"}


# ---------------------------------------------------------------------------
# Persistence — restart on the same file recovers state
# ---------------------------------------------------------------------------


def test_store_persists_across_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "fabric_registry.db"
    s1 = WorkspaceFabricStore(db_path)
    s1.register_entity_type("ws-a", "Lease")
    s1.register_property("ws-a", "Lease", "expiry_date", "date")
    s1.register_link("ws-a", "lease_tenant", "Lease", "Tenant")

    s2 = WorkspaceFabricStore(db_path)
    assert s2.entity_exists("ws-a", "Lease") is True
    assert s2.get_properties("ws-a", "Lease") == {"expiry_date"}
    assert s2.link_exists("ws-a", "lease_tenant", "Lease", "Tenant") is True


# ---------------------------------------------------------------------------
# Wave 4b integration smoke — wired through WorkspaceFabricRegistry
# ---------------------------------------------------------------------------


def test_populated_store_lints_lease_fixture_clean(store: WorkspaceFabricStore) -> None:
    """Populate the store with the entity types + links the lease
    fixture references and run the Wave 4b validator. The expected
    result is zero Fabric errors — proving the concrete EE registry
    satisfies the same contract the JSON mock does."""
    workspace = "ws-property-mgmt"
    for name in ("Lease", "Tenant", "Property"):
        store.register_entity_type(workspace, name)
    for prop in ("expiry_date", "rent_current", "rent_proposed", "renewal_stage"):
        store.register_property(workspace, "Lease", prop, "string")
    for prop in ("name", "email", "late_payment_count_12mo"):
        store.register_property(workspace, "Tenant", prop, "string")
    store.register_link(workspace, "lease_tenant", "Lease", "Tenant")
    store.register_link(workspace, "lease_property", "Lease", "Property")

    registry = WorkspaceFabricRegistry(store=store, workspace_id=workspace)

    lease_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "templates" / "lease-renewal-v2.yaml"
    )
    template = PocketTemplate.model_validate(yaml.safe_load(lease_path.read_text()))
    errors = validate_template_with_registry(template, registry)
    assert errors == []
