# tests/cloud/test_fabric_objects_source.py
# Created: 2026-06-19 (SZD-1 — fabric.objects ripple source).
#
# Smoke-proves the SZD-1 invariant for the "sovereign zero-setup discovery"
# feature: the `fabric.objects` / `fabric.query` ripple sources resolve an
# ObjectType into widget rows, and they are WORKSPACE-SCOPED — a spec resolved
# in workspace A sees only workspace A's objects, never workspace B's. The
# sources are backed by a real (temp) FabricStore so the store's own
# `workspace_id = ? OR workspace_id IS NULL` tenant guard is exercised end to
# end, not mocked away.

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

# Importing ripple_sources triggers the @register side-effects that put
# fabric.objects / fabric.query into the resolver registry (the cloud app does
# this eagerly at startup; tests must import it explicitly).
import pocketpaw_ee.cloud.ripple_sources  # noqa: F401
import pytest
from pocketpaw_ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec

from pocketpaw.fabric.store import FabricStore

WS_A = "ws-alpha"
WS_B = "ws-bravo"


async def _seed_store(db_path: Path) -> FabricStore:
    """A FabricStore with a 'Lead' type and objects in two workspaces."""
    store = FabricStore(db_path)
    lead_a = await store.define_type(name="Lead", properties=[], workspace_id=WS_A)
    lead_b = await store.define_type(name="Lead", properties=[], workspace_id=WS_B)
    await store.create_object(
        type_id=lead_a.id, properties={"name": "Acme", "status": "hot"}, workspace_id=WS_A
    )
    await store.create_object(
        type_id=lead_a.id, properties={"name": "Globex", "status": "cold"}, workspace_id=WS_A
    )
    # Workspace B owns a separate object of its own same-named type — this is
    # the row that must NOT leak into a workspace-A resolution.
    await store.create_object(
        type_id=lead_b.id, properties={"name": "Initech", "status": "hot"}, workspace_id=WS_B
    )
    return store


@pytest.mark.asyncio
async def test_fabric_objects_source_resolves_workspace_rows(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path / "fabric.db")
    ctx = ResolveCtx(workspace_id=WS_A, user_id="u1", pocket_id="p1")
    spec = {"state": {"leads": {"$source": "fabric.objects", "type_name": "Lead"}}}

    with patch("pocketpaw.stores.get_fabric_store", return_value=store):
        out = await resolve_ripple_spec(spec, ctx)

    rows = out["state"]["leads"]
    names = {r["name"] for r in rows}
    # Exactly workspace A's two Leads — Globex + Acme — and NOT workspace B's
    # Initech (the cross-tenant leak this proves is closed).
    assert names == {"Acme", "Globex"}, f"unexpected rows: {rows!r}"
    assert "Initech" not in names
    # Properties are spread to the top level alongside reserved keys.
    assert all("id" in r and "type_name" in r and "status" in r for r in rows)
    assert all(r["type_name"] == "Lead" for r in rows)


@pytest.mark.asyncio
async def test_fabric_objects_source_other_workspace_sees_only_its_own(tmp_path: Path) -> None:
    """Cross-workspace proof — the SAME spec resolved as workspace B returns
    only B's object, demonstrating the scope tracks ctx, not the spec."""
    store = await _seed_store(tmp_path / "fabric.db")
    ctx_b = ResolveCtx(workspace_id=WS_B, user_id="u2", pocket_id="p2")
    spec = {"state": {"leads": {"$source": "fabric.objects", "type_name": "Lead"}}}

    with patch("pocketpaw.stores.get_fabric_store", return_value=store):
        out = await resolve_ripple_spec(spec, ctx_b)

    names = {r["name"] for r in out["state"]["leads"]}
    assert names == {"Initech"}, f"workspace B leaked rows: {names!r}"
    assert "Acme" not in names and "Globex" not in names


@pytest.mark.asyncio
async def test_fabric_query_source_applies_filter_within_workspace(tmp_path: Path) -> None:
    """fabric.query adds a property filter and stays workspace-scoped."""
    store = await _seed_store(tmp_path / "fabric.db")
    ctx = ResolveCtx(workspace_id=WS_A, user_id="u1", pocket_id="p1")
    spec = {
        "state": {
            "hot": {
                "$source": "fabric.query",
                "type_name": "Lead",
                "filters": {"status": "hot"},
            }
        }
    }

    with patch("pocketpaw.stores.get_fabric_store", return_value=store):
        out = await resolve_ripple_spec(spec, ctx)

    names = {r["name"] for r in out["state"]["hot"]}
    # Only workspace A's hot lead (Acme). Globex is cold; Initech is workspace B.
    assert names == {"Acme"}, f"filter/scope mismatch: {names!r}"


@pytest.mark.asyncio
async def test_fabric_objects_source_no_type_returns_empty(tmp_path: Path) -> None:
    """A spec marker with neither type_id nor type_name resolves to [] (never
    raises, never returns the whole workspace)."""
    store = await _seed_store(tmp_path / "fabric.db")
    ctx = ResolveCtx(workspace_id=WS_A, user_id="u1", pocket_id="p1")
    spec = {"state": {"leads": {"$source": "fabric.objects"}}}

    with patch("pocketpaw.stores.get_fabric_store", return_value=store):
        out = await resolve_ripple_spec(spec, ctx)

    assert out["state"]["leads"] == []


@pytest.mark.asyncio
async def test_fabric_objects_source_no_workspace_returns_empty(tmp_path: Path) -> None:
    """Empty workspace context resolves to [] rather than an unscoped read."""
    store = await _seed_store(tmp_path / "fabric.db")
    ctx = ResolveCtx(workspace_id="", user_id="u1", pocket_id="p1")
    spec = {"state": {"leads": {"$source": "fabric.objects", "type_name": "Lead"}}}

    with patch("pocketpaw.stores.get_fabric_store", return_value=store):
        out = await resolve_ripple_spec(spec, ctx)

    assert out["state"]["leads"] == []
