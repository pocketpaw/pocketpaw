# Tests for ee/fabric — ontology store (SQLite).
# Created: 2026-03-28
# Updated: 2026-06-10 (W4a — workspace-scope fabric store) — Added
#   TestWorkspaceScoping: proves the cross-tenant read leak is closed. Workspace
#   A cannot read workspace B's objects via query()/get_object(), cannot
#   enumerate B's links via list_links(), and cannot traverse into B's objects
#   via get_linked_objects(). Also covers the legacy/NULL-workspace boundary
#   (rows written without a workspace stay visible to every scoped reader) and
#   the unscoped (workspace_id=None) backward-compat path. The W0d filter tests
#   below are the regression proof that workspace scoping layered on top of the
#   property filters without disturbing them.
# Updated: 2026-06-10 (W0d) — Added TestQueryFilters covering FabricQuery.filters:
#   numeric comparison (the "rent > X" case), string equality, operator aliases,
#   combined type+property filters, and the property-name / operator validation
#   guards. These would all have silently passed (returning every object) before
#   store.query() learned to honor filters.
# Updated: 2026-06-11 (gap-housekeeping) — Added TestTypeNameUniqueIndex (a
#   concurrent ensure_type race can't leave two type rows with the same name; the
#   unique index exists and a pre-existing duplicate-name DB is de-duped on first
#   _ensure_schema) and TestUpdateObjectWorkspaceScope (update_object honours its
#   own W4a tenancy guard — a cross-tenant id writes nothing and returns None).
# Updated: 2026-06-11 (fix/fabric-stats-workspace-scope) — Added
#   TestStatsWorkspaceScoping: scoped stats() / list_types() close the LAST two
#   unscoped W4a reads. Pins the live leak (another tenant's experimental type
#   names "Lease"/"Lease2" must NOT appear in a scoped type list), proves a
#   scoped object/link/type count excludes other-tenant rows, asserts the
#   stats/query consistency invariant (scoped stats["objects"] == scoped query
#   total — the count mismatch that surfaced the bug), confirms legacy NULL rows
#   stay visible, and that workspace_id=None keeps the instance-wide behavior.
# Updated: 2026-06-13 (feat/fabric-multihop) — Added TestMultiHopPath: proves the
#   2-hop ontology join the code audit flagged is now ONE server-side query.
#   Reproduces the exact competitive-intel scenario — Deal --deal_for--> Customer
#   --competes_with--> Competitor, filtered to open deals — that previously
#   returned [] from a single query() and had to be hand-stitched as two
#   get_linked_objects calls in app code. Also covers: property filters applied
#   at the intended hop, terminal object_type constraint, per-hop tenant scoping
#   (a linked object in another workspace must not leak across a hop), reverse
#   ("in") and symmetric ("any") hop directions, and backward-compat (single-hop
#   linked_to and an empty path behave exactly as before).
# Updated: 2026-06-13 (review fixes #1465) — extended TestMultiHopPath with the
#   bounds the reviewer asked for: a 3-hop chain (depth beyond the audit's 2), a
#   walk that collapses mid-path (empty intermediate frontier returns an empty
#   result, NOT an error), a single-hop fan-out exceeding MAX_FRONTIER (clean
#   ValueError, never a SQLite bound-variable crash), and a path deeper than
#   MAX_HOPS rejected at FabricQuery construction.

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from pocketpaw.fabric.models import MAX_HOPS, FabricQuery, PathHop, PropertyDef
from pocketpaw.fabric.store import MAX_FRONTIER, FabricStore


@pytest.fixture
def store(tmp_path: Path) -> FabricStore:
    return FabricStore(tmp_path / "test.db")


class TestObjectTypes:
    @pytest.mark.asyncio
    async def test_define_and_get(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Customer",
            properties=[
                PropertyDef(name="name", type="string", required=True),
                PropertyDef(name="email", type="string"),
                PropertyDef(name="revenue", type="number"),
            ],
            icon="user",
            color="#FF6B35",
        )
        assert t.id.startswith("ot-")
        assert t.name == "Customer"

        fetched = await store.get_type(t.id)
        assert fetched is not None
        assert fetched.name == "Customer"
        assert len(fetched.properties) == 3

    @pytest.mark.asyncio
    async def test_get_by_name(self, store: FabricStore) -> None:
        await store.define_type(name="Order", properties=[])
        found = await store.get_type_by_name("order")
        assert found is not None
        assert found.name == "Order"

    @pytest.mark.asyncio
    async def test_list_types(self, store: FabricStore) -> None:
        await store.define_type(name="A", properties=[])
        await store.define_type(name="B", properties=[])
        types = await store.list_types()
        assert len(types) == 2

    @pytest.mark.asyncio
    async def test_remove_cascades(self, store: FabricStore) -> None:
        t = await store.define_type(name="Product", properties=[])
        o1 = await store.create_object(t.id, {"name": "Widget"})
        o2 = await store.create_object(t.id, {"name": "Gadget"})
        await store.link(o1.id, o2.id, "related")

        await store.remove_type(t.id)
        types = await store.list_types()
        assert len(types) == 0
        result = await store.query(FabricQuery())
        assert result.total == 0


class TestObjects:
    @pytest.mark.asyncio
    async def test_create_and_get(self, store: FabricStore) -> None:
        t = await store.define_type(name="Customer", properties=[])
        obj = await store.create_object(t.id, {"name": "Acme", "email": "hi@acme.com"})
        assert obj.id.startswith("obj-")
        assert obj.type_name == "Customer"

        fetched = await store.get_object(obj.id)
        assert fetched is not None
        assert fetched.properties["name"] == "Acme"

    @pytest.mark.asyncio
    async def test_update(self, store: FabricStore) -> None:
        t = await store.define_type(name="Customer", properties=[])
        obj = await store.create_object(t.id, {"name": "Acme", "revenue": 50000})
        updated = await store.update_object(obj.id, {"revenue": 75000})
        assert updated is not None
        assert updated.properties["revenue"] == 75000
        assert updated.properties["name"] == "Acme"

    @pytest.mark.asyncio
    async def test_source_tracking(self, store: FabricStore) -> None:
        t = await store.define_type(name="Invoice", properties=[])
        obj = await store.create_object(
            t.id, {"amount": 100}, source_connector="stripe", source_id="inv_123"
        )
        assert obj.source_connector == "stripe"
        assert obj.source_id == "inv_123"

    @pytest.mark.asyncio
    async def test_remove(self, store: FabricStore) -> None:
        t = await store.define_type(name="X", properties=[])
        obj = await store.create_object(t.id, {})
        await store.remove_object(obj.id)
        assert await store.get_object(obj.id) is None


class TestLinks:
    @pytest.mark.asyncio
    async def test_link_and_traverse(self, store: FabricStore) -> None:
        ct = await store.define_type(name="Customer", properties=[])
        ot = await store.define_type(name="Order", properties=[])

        cust = await store.create_object(ct.id, {"name": "Acme"})
        o1 = await store.create_object(ot.id, {"amount": 100})
        o2 = await store.create_object(ot.id, {"amount": 200})

        await store.link(cust.id, o1.id, "has_order")
        await store.link(cust.id, o2.id, "has_order")

        linked = await store.get_linked_objects(cust.id, "has_order")
        assert len(linked) == 2

    @pytest.mark.asyncio
    async def test_unlink(self, store: FabricStore) -> None:
        t = await store.define_type(name="X", properties=[])
        a = await store.create_object(t.id, {})
        b = await store.create_object(t.id, {})
        lnk = await store.link(a.id, b.id, "r")
        await store.unlink(lnk.id)
        linked = await store.get_linked_objects(a.id)
        assert len(linked) == 0


class TestQuery:
    @pytest.mark.asyncio
    async def test_by_type_name(self, store: FabricStore) -> None:
        ct = await store.define_type(name="Customer", properties=[])
        ot = await store.define_type(name="Order", properties=[])
        await store.create_object(ct.id, {"name": "A"})
        await store.create_object(ct.id, {"name": "B"})
        await store.create_object(ot.id, {"amount": 100})

        result = await store.query(FabricQuery(type_name="Customer"))
        assert result.total == 2

    @pytest.mark.asyncio
    async def test_by_linked(self, store: FabricStore) -> None:
        ct = await store.define_type(name="Customer", properties=[])
        ot = await store.define_type(name="Order", properties=[])
        cust = await store.create_object(ct.id, {"name": "Acme"})
        o1 = await store.create_object(ot.id, {"amount": 100})
        await store.create_object(ot.id, {"amount": 200})  # not linked
        await store.link(cust.id, o1.id, "has_order")

        result = await store.query(FabricQuery(linked_to=cust.id, link_type="has_order"))
        assert result.total == 1

    @pytest.mark.asyncio
    async def test_pagination(self, store: FabricStore) -> None:
        t = await store.define_type(name="Item", properties=[])
        for i in range(10):
            await store.create_object(t.id, {"idx": i})

        r1 = await store.query(FabricQuery(type_name="Item", limit=3, offset=0))
        assert len(r1.objects) == 3
        assert r1.total == 10

    @pytest.mark.asyncio
    async def test_stats(self, store: FabricStore) -> None:
        t = await store.define_type(name="X", properties=[])
        a = await store.create_object(t.id, {})
        b = await store.create_object(t.id, {})
        await store.link(a.id, b.id, "r")
        s = await store.stats()
        assert s == {"types": 1, "objects": 2, "links": 1}


class TestQueryFilters:
    """FabricQuery.filters must actually narrow results (W0d regression guard)."""

    async def _seed_leases(self, store: FabricStore) -> str:
        """Three leases with varied rent + status; returns the type id."""
        t = await store.define_type(
            name="Lease",
            properties=[
                PropertyDef(name="tenant", type="string"),
                PropertyDef(name="rent", type="number"),
                PropertyDef(name="status", type="string"),
            ],
        )
        await store.create_object(t.id, {"tenant": "Acme", "rent": 500, "status": "active"})
        await store.create_object(t.id, {"tenant": "Globex", "rent": 1500, "status": "active"})
        await store.create_object(t.id, {"tenant": "Initech", "rent": 3000, "status": "expired"})
        return t.id

    @pytest.mark.asyncio
    async def test_numeric_comparison_gt(self, store: FabricStore) -> None:
        # The "rent > X" case — the exact bug this task fixes.
        await self._seed_leases(store)
        result = await store.query(FabricQuery(type_name="Lease", filters={"rent": {">": 1000}}))
        assert result.total == 2
        tenants = {o.properties["tenant"] for o in result.objects}
        assert tenants == {"Globex", "Initech"}

    @pytest.mark.asyncio
    async def test_numeric_comparison_gte_and_lte(self, store: FabricStore) -> None:
        await self._seed_leases(store)
        gte = await store.query(FabricQuery(type_name="Lease", filters={"rent": {">=": 1500}}))
        assert {o.properties["tenant"] for o in gte.objects} == {"Globex", "Initech"}

        lte = await store.query(FabricQuery(type_name="Lease", filters={"rent": {"<=": 1500}}))
        assert {o.properties["tenant"] for o in lte.objects} == {"Acme", "Globex"}

    @pytest.mark.asyncio
    async def test_string_equality_scalar(self, store: FabricStore) -> None:
        await self._seed_leases(store)
        result = await store.query(FabricQuery(type_name="Lease", filters={"status": "active"}))
        assert result.total == 2
        assert all(o.properties["status"] == "active" for o in result.objects)

    @pytest.mark.asyncio
    async def test_operator_word_aliases(self, store: FabricStore) -> None:
        # Word aliases (gt/lte/...) must behave identically to their symbols.
        await self._seed_leases(store)
        gt = await store.query(FabricQuery(type_name="Lease", filters={"rent": {"gt": 1000}}))
        assert gt.total == 2

    @pytest.mark.asyncio
    async def test_not_equal(self, store: FabricStore) -> None:
        await self._seed_leases(store)
        result = await store.query(
            FabricQuery(type_name="Lease", filters={"status": {"!=": "active"}})
        )
        assert result.total == 1
        assert result.objects[0].properties["tenant"] == "Initech"

    @pytest.mark.asyncio
    async def test_combined_type_and_property_filter(self, store: FabricStore) -> None:
        # type filter AND property filters are AND-ed; a different type with a
        # matching rent must not leak in.
        await self._seed_leases(store)
        other = await store.define_type(name="Invoice", properties=[])
        await store.create_object(other.id, {"rent": 9999, "status": "active"})

        result = await store.query(
            FabricQuery(
                type_name="Lease",
                filters={"status": "active", "rent": {">": 1000}},
            )
        )
        assert result.total == 1
        assert result.objects[0].properties["tenant"] == "Globex"

    @pytest.mark.asyncio
    async def test_missing_property_excluded_from_comparison(self, store: FabricStore) -> None:
        # An object lacking the filtered property must not match a comparison.
        t = await store.define_type(name="Lease", properties=[])
        await store.create_object(t.id, {"tenant": "HasRent", "rent": 2000})
        await store.create_object(t.id, {"tenant": "NoRent"})
        result = await store.query(FabricQuery(type_name="Lease", filters={"rent": {">": 1000}}))
        assert result.total == 1
        assert result.objects[0].properties["tenant"] == "HasRent"

    @pytest.mark.asyncio
    async def test_invalid_property_name_rejected(self, store: FabricStore) -> None:
        await self._seed_leases(store)
        with pytest.raises(ValueError):
            await store.query(FabricQuery(type_name="Lease", filters={"rent') OR 1=1 --": 1}))

    @pytest.mark.asyncio
    async def test_unsupported_operator_rejected(self, store: FabricStore) -> None:
        await self._seed_leases(store)
        with pytest.raises(ValueError):
            await store.query(FabricQuery(type_name="Lease", filters={"rent": {"LIKE": 1}}))


class TestWorkspaceScoping:
    """W4a — the cross-tenant data leak is closed.

    On a shared deployment (the micro tier / an agency running multiple client
    tenants) the Fabric store is one global ``fabric.db``. Before W4a, every
    read returned every tenant's rows. These tests prove a scoped read sees only
    the caller's workspace (plus legacy NULL-workspace rows) across all four
    read paths: ``query()``, ``get_object()``, ``list_links()``, and
    ``get_linked_objects()``.
    """

    @pytest.mark.asyncio
    async def test_query_isolates_objects_by_workspace(self, store: FabricStore) -> None:
        t = await store.define_type(name="Customer", properties=[])
        await store.create_object(t.id, {"name": "A-owned"}, workspace_id="ws-a")
        await store.create_object(t.id, {"name": "B-owned"}, workspace_id="ws-b")

        # Workspace A sees only A's object — NOT B's.
        a_result = await store.query(FabricQuery(type_name="Customer"), workspace_id="ws-a")
        assert a_result.total == 1
        assert a_result.objects[0].properties["name"] == "A-owned"

        # Workspace B sees only B's object — NOT A's.
        b_result = await store.query(FabricQuery(type_name="Customer"), workspace_id="ws-b")
        assert b_result.total == 1
        assert b_result.objects[0].properties["name"] == "B-owned"

    @pytest.mark.asyncio
    async def test_get_object_returns_none_cross_tenant(self, store: FabricStore) -> None:
        t = await store.define_type(name="Secret", properties=[])
        b_obj = await store.create_object(t.id, {"name": "B-secret"}, workspace_id="ws-b")

        # Workspace A asking for B's object id by direct lookup gets nothing
        # (the router turns this into a 404 — never leaks existence).
        assert await store.get_object(b_obj.id, workspace_id="ws-a") is None
        # B itself can still read it.
        assert (await store.get_object(b_obj.id, workspace_id="ws-b")) is not None

    @pytest.mark.asyncio
    async def test_list_links_isolated_by_workspace(self, store: FabricStore) -> None:
        t = await store.define_type(name="Node", properties=[])
        a1 = await store.create_object(t.id, {"n": 1}, workspace_id="ws-a")
        a2 = await store.create_object(t.id, {"n": 2}, workspace_id="ws-a")
        b1 = await store.create_object(t.id, {"n": 3}, workspace_id="ws-b")
        b2 = await store.create_object(t.id, {"n": 4}, workspace_id="ws-b")
        await store.link(a1.id, a2.id, "rel", workspace_id="ws-a")
        await store.link(b1.id, b2.id, "rel", workspace_id="ws-b")

        a_links, a_total = await store.list_links(workspace_id="ws-a")
        assert a_total == 1
        assert a_links[0].from_object_id == a1.id

        b_links, b_total = await store.list_links(workspace_id="ws-b")
        assert b_total == 1
        assert b_links[0].from_object_id == b1.id

    @pytest.mark.asyncio
    async def test_get_linked_objects_isolated(self, store: FabricStore) -> None:
        t = await store.define_type(name="Node", properties=[])
        hub = await store.create_object(t.id, {"role": "hub"}, workspace_id="ws-a")
        a_leaf = await store.create_object(t.id, {"role": "a-leaf"}, workspace_id="ws-a")
        b_leaf = await store.create_object(t.id, {"role": "b-leaf"}, workspace_id="ws-b")
        # A link that (incorrectly) spans the tenant boundary must still not
        # surface B's object to an A-scoped traversal.
        await store.link(hub.id, a_leaf.id, "rel", workspace_id="ws-a")
        await store.link(hub.id, b_leaf.id, "rel", workspace_id="ws-a")

        linked = await store.get_linked_objects(hub.id, "rel", workspace_id="ws-a")
        roles = {o.properties["role"] for o in linked}
        assert roles == {"a-leaf"}  # b-leaf is excluded — it belongs to ws-b

    @pytest.mark.asyncio
    async def test_legacy_null_workspace_visible_to_all(self, store: FabricStore) -> None:
        # A row written before tenancy (no workspace_id) cannot be attributed to
        # one tenant after the fact, so a scoped read still sees it. This keeps
        # single-tenant deployments and pre-migration data working.
        t = await store.define_type(name="Legacy", properties=[])
        await store.create_object(t.id, {"name": "pre-tenancy"})  # NULL workspace

        a_result = await store.query(FabricQuery(type_name="Legacy"), workspace_id="ws-a")
        b_result = await store.query(FabricQuery(type_name="Legacy"), workspace_id="ws-b")
        assert a_result.total == 1
        assert b_result.total == 1

    @pytest.mark.asyncio
    async def test_unscoped_read_sees_everything(self, store: FabricStore) -> None:
        # workspace_id=None (OSS / agent-tool caller) is fully backward
        # compatible — no scoping at all.
        t = await store.define_type(name="Item", properties=[])
        await store.create_object(t.id, {"n": 1}, workspace_id="ws-a")
        await store.create_object(t.id, {"n": 2}, workspace_id="ws-b")
        await store.create_object(t.id, {"n": 3})  # NULL

        result = await store.query(FabricQuery(type_name="Item"))
        assert result.total == 3


class TestStatsWorkspaceScoping:
    """fix/fabric-stats-workspace-scope — scoped stats() and list_types().

    The last two W4a reads were left instance-wide. On a shared ``fabric.db``
    that leaked: a tenant's chat called fabric_stats and got back another
    context's experimental type names (``Lease`` / ``Lease2``), and the global
    object count (13) disagreed with what the workspace-scoped query actually
    saw (11). These tests pin both: scoped counts mirror ``query()`` exactly,
    and a scoped type list never names a type only another tenant has rows for.

    Subtlety being guarded: ``fabric_object_types`` has NO workspace column —
    type DEFINITIONS are global. So a scoped type list cannot come from the
    type table; it must be derived from types with at least one object row
    VISIBLE to the workspace.
    """

    @pytest.mark.asyncio
    async def test_stats_objects_excludes_other_workspace(self, store: FabricStore) -> None:
        t = await store.define_type(name="Customer", properties=[])
        await store.create_object(t.id, {"name": "A1"}, workspace_id="ws-a")
        await store.create_object(t.id, {"name": "A2"}, workspace_id="ws-a")
        await store.create_object(t.id, {"name": "B1"}, workspace_id="ws-b")

        a_stats = await store.stats(workspace_id="ws-a")
        assert a_stats["objects"] == 2  # B1 is excluded
        b_stats = await store.stats(workspace_id="ws-b")
        assert b_stats["objects"] == 1

    @pytest.mark.asyncio
    async def test_stats_links_excludes_other_workspace(self, store: FabricStore) -> None:
        t = await store.define_type(name="Node", properties=[])
        a1 = await store.create_object(t.id, {"n": 1}, workspace_id="ws-a")
        a2 = await store.create_object(t.id, {"n": 2}, workspace_id="ws-a")
        b1 = await store.create_object(t.id, {"n": 3}, workspace_id="ws-b")
        b2 = await store.create_object(t.id, {"n": 4}, workspace_id="ws-b")
        await store.link(a1.id, a2.id, "rel", workspace_id="ws-a")
        await store.link(b1.id, b2.id, "rel", workspace_id="ws-b")

        assert (await store.stats(workspace_id="ws-a"))["links"] == 1
        assert (await store.stats(workspace_id="ws-b"))["links"] == 1

    @pytest.mark.asyncio
    async def test_scoped_type_list_excludes_other_tenant_type_names(
        self, store: FabricStore
    ) -> None:
        # The exact live leak: ws-b defines "Lease" / "Lease2" and only B has
        # rows of them. ws-a, which only models "Customer", must NOT see those
        # type names — even though the type DEFINITIONS are global.
        customer = await store.define_type(name="Customer", properties=[])
        lease = await store.define_type(name="Lease", properties=[])
        lease2 = await store.define_type(name="Lease2", properties=[])
        await store.create_object(customer.id, {"name": "A1"}, workspace_id="ws-a")
        await store.create_object(lease.id, {"tenant": "B-co"}, workspace_id="ws-b")
        await store.create_object(lease2.id, {"tenant": "B-co"}, workspace_id="ws-b")

        a_types = {t.name for t in await store.list_types(workspace_id="ws-a")}
        assert a_types == {"Customer"}
        assert "Lease" not in a_types
        assert "Lease2" not in a_types

        b_types = {t.name for t in await store.list_types(workspace_id="ws-b")}
        assert b_types == {"Lease", "Lease2"}

    @pytest.mark.asyncio
    async def test_stats_types_count_matches_scoped_type_list(self, store: FabricStore) -> None:
        # stats["types"] is the count of types VISIBLE to the workspace — it
        # must equal len(list_types(workspace_id)), not the global type count.
        customer = await store.define_type(name="Customer", properties=[])
        lease = await store.define_type(name="Lease", properties=[])
        await store.create_object(customer.id, {"name": "A1"}, workspace_id="ws-a")
        await store.create_object(lease.id, {"tenant": "B-co"}, workspace_id="ws-b")

        a_stats = await store.stats(workspace_id="ws-a")
        a_types = await store.list_types(workspace_id="ws-a")
        assert a_stats["types"] == len(a_types) == 1  # NOT 2 (the global count)

    @pytest.mark.asyncio
    async def test_stats_objects_consistent_with_query_total(self, store: FabricStore) -> None:
        # The invariant that surfaced the bug: scoped stats and scoped query
        # must agree on the object count. Mixed-tenant + legacy NULL rows.
        t = await store.define_type(name="Customer", properties=[])
        await store.create_object(t.id, {"name": "A1"}, workspace_id="ws-a")
        await store.create_object(t.id, {"name": "A2"}, workspace_id="ws-a")
        await store.create_object(t.id, {"name": "B1"}, workspace_id="ws-b")
        await store.create_object(t.id, {"name": "legacy"})  # NULL workspace

        a_stats = await store.stats(workspace_id="ws-a")
        a_query = await store.query(FabricQuery(), workspace_id="ws-a")
        assert a_stats["objects"] == a_query.total == 3  # A1, A2, legacy — not B1

    @pytest.mark.asyncio
    async def test_legacy_null_type_visible_to_all_scoped_readers(self, store: FabricStore) -> None:
        # A type whose only rows predate tenancy (NULL workspace) stays visible
        # to every scoped reader — matching the query()-side legacy rule.
        legacy_type = await store.define_type(name="Legacy", properties=[])
        await store.create_object(legacy_type.id, {"name": "pre-tenancy"})  # NULL

        for ws in ("ws-a", "ws-b"):
            names = {t.name for t in await store.list_types(workspace_id=ws)}
            assert "Legacy" in names
            assert (await store.stats(workspace_id=ws))["objects"] == 1

    @pytest.mark.asyncio
    async def test_unscoped_stats_is_instance_wide(self, store: FabricStore) -> None:
        # workspace_id=None keeps the original instance-wide behavior for OSS /
        # registry-tool / single-tenant callers (full backward-compat).
        customer = await store.define_type(name="Customer", properties=[])
        lease = await store.define_type(name="Lease", properties=[])
        await store.create_object(customer.id, {"name": "A1"}, workspace_id="ws-a")
        await store.create_object(lease.id, {"tenant": "B-co"}, workspace_id="ws-b")

        s = await store.stats()
        assert s["objects"] == 2
        assert s["types"] == 2  # both defined types
        names = {t.name for t in await store.list_types()}
        assert names == {"Customer", "Lease"}

    @pytest.mark.asyncio
    async def test_scoped_type_with_no_visible_rows_is_omitted(self, store: FabricStore) -> None:
        # A defined type with zero visible rows (even the caller's own empty
        # type) is omitted, and reappears the moment a visible object exists.
        empty = await store.define_type(name="Empty", properties=[])
        assert {t.name for t in await store.list_types(workspace_id="ws-a")} == set()

        await store.create_object(empty.id, {"k": "v"}, workspace_id="ws-a")
        assert {t.name for t in await store.list_types(workspace_id="ws-a")} == {"Empty"}


class TestTypeNameUniqueIndex:
    """A UNIQUE index on fabric_object_types(name) closes the ensure_type race."""

    @pytest.mark.asyncio
    async def test_concurrent_same_name_defines_one_type(self, store: FabricStore) -> None:
        # Two concurrent define_type calls for the same name must not leave two
        # type rows. The UNIQUE index makes the second INSERT fail rather than
        # silently splitting the same logical type across two ids.
        results = await asyncio.gather(
            store.define_type(name="Customer", properties=[]),
            store.define_type(name="Customer", properties=[]),
            return_exceptions=True,
        )
        # At least one succeeded; any second one either errored or was rejected.
        ok = [r for r in results if not isinstance(r, Exception)]
        assert ok, "at least one define_type must succeed"

        types = [t for t in await store.list_types() if t.name == "Customer"]
        assert len(types) == 1, "the unique index must prevent a duplicate type row"

    @pytest.mark.asyncio
    async def test_unique_index_exists(self, store: FabricStore) -> None:
        await store.define_type(name="Anything", properties=[])  # forces _ensure_schema
        async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND tbl_name='fabric_object_types'"
            ) as cur:
                names = {row["name"] async for row in cur}
        assert "idx_object_types_name_unique" in names

    @pytest.mark.asyncio
    async def test_preexisting_duplicate_names_are_deduped(self, tmp_path: Path) -> None:
        # Simulate a pre-this-change DB that already holds duplicate-name type
        # rows (the race could fire before the unique index existed). _ensure_schema
        # must de-dup defensively and STILL create the index — never crash.
        db_path = tmp_path / "dup.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE fabric_object_types ("
                " id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',"
                " icon TEXT DEFAULT 'box', color TEXT DEFAULT '#0A84FF',"
                " properties_schema TEXT DEFAULT '[]',"
                " created_at TEXT DEFAULT (datetime('now')),"
                " updated_at TEXT DEFAULT (datetime('now')))"
            )
            await db.execute(
                "CREATE TABLE fabric_objects ("
                " id TEXT PRIMARY KEY, type_id TEXT NOT NULL, type_name TEXT DEFAULT '',"
                " properties TEXT NOT NULL DEFAULT '{}', source_connector TEXT, source_id TEXT,"
                " created_at TEXT DEFAULT (datetime('now')),"
                " updated_at TEXT DEFAULT (datetime('now')))"
            )
            # Two type rows with the same name; an object bound to the LOSER id.
            await db.execute(
                "INSERT INTO fabric_object_types (id, name) VALUES ('ot-keep', 'Customer')"
            )
            await db.execute(
                "INSERT INTO fabric_object_types (id, name) VALUES ('ot-dup', 'Customer')"
            )
            await db.execute(
                "INSERT INTO fabric_objects (id, type_id, properties)"
                " VALUES ('obj-1', 'ot-dup', '{}')"
            )
            await db.commit()

        store = FabricStore(db_path)
        # First read triggers _ensure_schema → de-dup + index creation.
        types = [t for t in await store.list_types() if t.name == "Customer"]
        assert len(types) == 1, "duplicate type rows must be collapsed to one survivor"
        assert types[0].id == "ot-keep", "the lowest-rowid survivor is kept"

        # The orphan object was re-homed onto the survivor, not dropped.
        obj = await store.get_object("obj-1")
        assert obj is not None
        assert obj.type_id == "ot-keep"


class TestUpdateObjectWorkspaceScope:
    """update_object carries its own W4a tenancy guard (gap-housekeeping)."""

    @pytest.mark.asyncio
    async def test_update_is_workspace_scoped(self, store: FabricStore) -> None:
        t = await store.define_type(name="Lease", properties=[])
        b_obj = await store.create_object(t.id, {"rent": 100}, workspace_id="ws-b")

        # Workspace A cannot update B's object — returns None and writes nothing.
        result = await store.update_object(b_obj.id, {"rent": 999}, workspace_id="ws-a")
        assert result is None
        unchanged = await store.get_object(b_obj.id, workspace_id="ws-b")
        assert unchanged is not None
        assert unchanged.properties["rent"] == 100

    @pytest.mark.asyncio
    async def test_owner_can_update_in_scope(self, store: FabricStore) -> None:
        t = await store.define_type(name="Lease", properties=[])
        b_obj = await store.create_object(t.id, {"rent": 100}, workspace_id="ws-b")
        updated = await store.update_object(b_obj.id, {"rent": 250}, workspace_id="ws-b")
        assert updated is not None
        assert updated.properties["rent"] == 250

    @pytest.mark.asyncio
    async def test_legacy_null_workspace_updatable_by_scoped_caller(
        self, store: FabricStore
    ) -> None:
        # A legacy NULL-workspace row stays writable by any scoped caller, matching
        # the read-side `workspace_id = ? OR workspace_id IS NULL` semantics.
        t = await store.define_type(name="Lease", properties=[])
        legacy = await store.create_object(t.id, {"rent": 100})  # NULL workspace
        updated = await store.update_object(legacy.id, {"rent": 175}, workspace_id="ws-a")
        assert updated is not None
        assert updated.properties["rent"] == 175


class TestMultiHopPath:
    """Multi-hop / path traversal — the 2-hop ontology join in ONE query.

    The code audit found that ``FabricStore.query()`` could only do ONE hop, so
    the competitive-intel question "which open Deals link to a Customer that
    ``competes_with`` a Competitor" returned [] as a single query and had to be
    hand-stitched as two separate ``get_linked_objects`` calls in app code.
    These tests pin the join as a single server-side query. The seed graph:

        Deal(Acme deal, stage=open)   --deal_for-->   Customer(Acme)
        Deal(Beta deal, stage=open)   --deal_for-->   Customer(Beta)
        Deal(Won deal,  stage=won)    --deal_for-->   Customer(Acme)
        Customer(Acme)  --competes_with-->  Competitor(Rival Inc)
        (Customer(Beta) competes with nobody)

    The 2-hop join from Competitor back to open Deals (or from Deals out to a
    competing Customer) must return exactly the Acme open deal.
    """

    async def _seed(self, store: FabricStore, workspace_id: str | None = None):
        """Build the audit graph. Returns a dict of the key objects/types."""
        deal_t = await store.define_type(
            name="Deal",
            properties=[
                PropertyDef(name="name", type="string"),
                PropertyDef(name="stage", type="string"),
            ],
        )
        cust_t = await store.define_type(name="Customer", properties=[])
        comp_t = await store.define_type(name="Competitor", properties=[])

        acme = await store.create_object(cust_t.id, {"name": "Acme"}, workspace_id=workspace_id)
        beta = await store.create_object(cust_t.id, {"name": "Beta"}, workspace_id=workspace_id)
        rival = await store.create_object(
            comp_t.id, {"name": "Rival Inc"}, workspace_id=workspace_id
        )

        acme_deal = await store.create_object(
            deal_t.id, {"name": "Acme deal", "stage": "open"}, workspace_id=workspace_id
        )
        beta_deal = await store.create_object(
            deal_t.id, {"name": "Beta deal", "stage": "open"}, workspace_id=workspace_id
        )
        won_deal = await store.create_object(
            deal_t.id, {"name": "Won deal", "stage": "won"}, workspace_id=workspace_id
        )

        # Deal --deal_for--> Customer (forward / "out")
        await store.link(acme_deal.id, acme.id, "deal_for", workspace_id=workspace_id)
        await store.link(beta_deal.id, beta.id, "deal_for", workspace_id=workspace_id)
        await store.link(won_deal.id, acme.id, "deal_for", workspace_id=workspace_id)
        # Customer --competes_with--> Competitor (forward / "out")
        await store.link(acme.id, rival.id, "competes_with", workspace_id=workspace_id)

        return {
            "deal_t": deal_t,
            "cust_t": cust_t,
            "comp_t": comp_t,
            "acme": acme,
            "beta": beta,
            "rival": rival,
            "acme_deal": acme_deal,
            "beta_deal": beta_deal,
            "won_deal": won_deal,
        }

    @pytest.mark.asyncio
    async def test_audit_scenario_two_hop_join_in_one_query(self, store: FabricStore) -> None:
        """THE audit scenario: start at the Competitor, walk back to the OPEN
        Deals whose Customer competes_with it — exactly the Acme open deal.

        This is the query that returned [] before multi-hop. It walks:
          Competitor(Rival) <--competes_with-- Customer <--deal_for-- Deal(open)
        Each backward edge is a reverse ("in") hop; the terminal hop pins the
        Deal type + stage=open filter.
        """
        g = await self._seed(store)

        result = await store.query(
            FabricQuery(
                linked_to=g["rival"].id,
                path=[
                    # Competitor -> the Customers that compete_with it (reverse).
                    PathHop(link_type="competes_with", object_type="Customer", direction="in"),
                    # Customer -> the Deals that are deal_for it (reverse), open only.
                    PathHop(
                        link_type="deal_for",
                        object_type="Deal",
                        direction="in",
                        filters={"stage": "open"},
                    ),
                ],
            )
        )

        names = {o.properties["name"] for o in result.objects}
        assert names == {"Acme deal"}
        assert result.total == 1
        # The won deal links to the same Customer but is filtered out by stage.
        assert "Won deal" not in names
        # The Beta deal's customer competes with nobody, so it never appears.
        assert "Beta deal" not in names

    @pytest.mark.asyncio
    async def test_old_code_single_query_returns_empty(self, store: FabricStore) -> None:
        """Documents the gap: the single-hop query CANNOT express the join.

        ``linked_to=rival, link_type=competes_with`` reaches the Customer only
        — never the Deal — so asking for Deals via that one hop is empty. This
        is the exact shortfall the audit hit before ``path`` existed.
        """
        g = await self._seed(store)

        one_hop = await store.query(
            FabricQuery(
                type_name="Deal",
                linked_to=g["rival"].id,
                link_type="competes_with",
            )
        )
        # One hop from the Competitor lands on the Customer, which is not a Deal.
        assert one_hop.total == 0

    @pytest.mark.asyncio
    async def test_forward_direction_from_deals_out(self, store: FabricStore) -> None:
        """The same join read the other way: start at the open Deals, walk OUT
        to a Customer, then OUT to a Competitor — keep only deals that reach one.

        Returns the terminal Competitor here (the path's last hop is Competitor).
        Proves forward ("out") traversal and that the START filter (open deals)
        plus terminal type both apply.
        """
        await self._seed(store)

        result = await store.query(
            FabricQuery(
                type_name="Deal",
                filters={"stage": "open"},
                # linked_to omitted -> start frontier is every object matching the
                # top-level type/filters (open Deals), then walk the path out.
                path=[
                    PathHop(link_type="deal_for", object_type="Customer", direction="out"),
                    PathHop(link_type="competes_with", object_type="Competitor", direction="out"),
                ],
            )
        )
        # Only Acme's open deal reaches a Competitor; terminal objects are the
        # Competitor(s) reached -> Rival Inc, once.
        names = {o.properties["name"] for o in result.objects}
        assert names == {"Rival Inc"}

    @pytest.mark.asyncio
    async def test_property_filter_applies_at_intended_hop(self, store: FabricStore) -> None:
        """Dropping the stage=open filter lets the won deal through too — proof
        the filter is what excludes it, applied at the terminal (Deal) hop."""
        g = await self._seed(store)

        no_filter = await store.query(
            FabricQuery(
                linked_to=g["rival"].id,
                path=[
                    PathHop(link_type="competes_with", object_type="Customer", direction="in"),
                    PathHop(link_type="deal_for", object_type="Deal", direction="in"),
                ],
            )
        )
        names = {o.properties["name"] for o in no_filter.objects}
        assert names == {"Acme deal", "Won deal"}  # both Acme deals, no stage filter

    @pytest.mark.asyncio
    async def test_terminal_object_type_constrains_result(self, store: FabricStore) -> None:
        """Without the terminal object_type, a hop returns whatever is on the
        other end; with it, only matching-type objects survive."""
        g = await self._seed(store)

        # Hop once from the Competitor back along competes_with — reaches the
        # Customer. Constrain to Customer: present. Constrain to Deal: empty.
        as_customer = await store.query(
            FabricQuery(
                linked_to=g["rival"].id,
                path=[PathHop(link_type="competes_with", object_type="Customer", direction="in")],
            )
        )
        assert {o.properties["name"] for o in as_customer.objects} == {"Acme"}

        as_deal = await store.query(
            FabricQuery(
                linked_to=g["rival"].id,
                path=[PathHop(link_type="competes_with", object_type="Deal", direction="in")],
            )
        )
        assert as_deal.total == 0

    @pytest.mark.asyncio
    async def test_tenant_scoping_holds_across_hops(self, store: FabricStore) -> None:
        """A linked object in another workspace must not leak across a hop.

        ws-a owns the full chain. ws-b owns a Competitor that an A-owned link
        (mis)points at. An A-scoped 2-hop query must never surface ws-b's
        objects, and a B-scoped query must not see ws-a's deal.
        """
        g = await self._seed(store, workspace_id="ws-a")

        # A cross-tenant Competitor + a link from A's Customer into it. Even
        # though the LINK is A-owned, the terminal object belongs to ws-b.
        b_rival = await store.create_object(
            g["comp_t"].id, {"name": "B Rival"}, workspace_id="ws-b"
        )
        await store.link(g["acme"].id, b_rival.id, "competes_with", workspace_id="ws-a")

        # A-scoped: walk Deal(open) --deal_for--> Customer --competes_with-->
        # Competitor. b_rival must be excluded; only Rival Inc (ws-a) survives.
        a_result = await store.query(
            FabricQuery(
                type_name="Deal",
                filters={"stage": "open"},
                path=[
                    PathHop(link_type="deal_for", object_type="Customer", direction="out"),
                    PathHop(link_type="competes_with", object_type="Competitor", direction="out"),
                ],
            ),
            workspace_id="ws-a",
        )
        assert {o.properties["name"] for o in a_result.objects} == {"Rival Inc"}
        assert "B Rival" not in {o.properties["name"] for o in a_result.objects}

        # B-scoped: B owns no Deal/Customer, so the same path yields nothing.
        b_result = await store.query(
            FabricQuery(
                type_name="Deal",
                filters={"stage": "open"},
                path=[
                    PathHop(link_type="deal_for", object_type="Customer", direction="out"),
                    PathHop(link_type="competes_with", object_type="Competitor", direction="out"),
                ],
            ),
            workspace_id="ws-b",
        )
        assert b_result.total == 0

    @pytest.mark.asyncio
    async def test_any_direction_matches_either_way(self, store: FabricStore) -> None:
        """``direction="any"`` traverses a link regardless of stored direction —
        the symmetric semantics of the legacy single-hop ``linked_to``."""
        g = await self._seed(store)

        # competes_with was stored Customer->Competitor. From the Competitor,
        # an "any" hop still reaches the Customer.
        result = await store.query(
            FabricQuery(
                linked_to=g["rival"].id,
                path=[PathHop(link_type="competes_with", object_type="Customer", direction="any")],
            )
        )
        assert {o.properties["name"] for o in result.objects} == {"Acme"}

    @pytest.mark.asyncio
    async def test_empty_path_is_backward_compatible(self, store: FabricStore) -> None:
        """An empty ``path`` (default) leaves single-hop behavior untouched."""
        g = await self._seed(store)

        # Legacy single-hop: customers linked to the rival via competes_with.
        legacy = await store.query(FabricQuery(linked_to=g["rival"].id, link_type="competes_with"))
        assert {o.properties["name"] for o in legacy.objects} == {"Acme"}

    @pytest.mark.asyncio
    async def test_three_hop_path(self, store: FabricStore) -> None:
        """A 3-hop chain resolves end to end in one query.

        Region <--in_region-- Competitor <--competes_with-- Customer
        <--deal_for-- Deal(open). Start at the Region, walk back three reverse
        hops to the open Deal(s). Exercises depth beyond the audit's 2 hops.
        """
        g = await self._seed(store)
        region_t = await store.define_type(name="Region", properties=[])
        emea = await store.create_object(region_t.id, {"name": "EMEA"})
        # Competitor(Rival Inc) --in_region--> Region(EMEA)
        await store.link(g["rival"].id, emea.id, "in_region")

        result = await store.query(
            FabricQuery(
                linked_to=emea.id,
                path=[
                    PathHop(link_type="in_region", object_type="Competitor", direction="in"),
                    PathHop(link_type="competes_with", object_type="Customer", direction="in"),
                    PathHop(
                        link_type="deal_for",
                        object_type="Deal",
                        direction="in",
                        filters={"stage": "open"},
                    ),
                ],
            )
        )
        assert {o.properties["name"] for o in result.objects} == {"Acme deal"}

    @pytest.mark.asyncio
    async def test_empty_intermediate_frontier_returns_empty_not_error(
        self, store: FabricStore
    ) -> None:
        """A walk that collapses mid-path (a hop reaches nothing) returns an
        empty result, NOT an error — the dead end is a normal outcome."""
        g = await self._seed(store)

        # Hop 1 reaches the Customer (Acme). Hop 2 follows a link_type that no
        # object has -> the frontier empties. Hop 3 must not run / error.
        result = await store.query(
            FabricQuery(
                linked_to=g["rival"].id,
                path=[
                    PathHop(link_type="competes_with", object_type="Customer", direction="in"),
                    PathHop(link_type="no_such_link", direction="in"),
                    PathHop(link_type="deal_for", object_type="Deal", direction="in"),
                ],
            )
        )
        assert result.objects == []
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_fan_out_exceeding_max_frontier_raises_clean_value_error(
        self, store: FabricStore
    ) -> None:
        """A single hop that fans out past MAX_FRONTIER raises a clear ValueError
        rather than crashing SQLite on the bound-variable limit."""
        hub_t = await store.define_type(name="Hub", properties=[])
        leaf_t = await store.define_type(name="Leaf", properties=[])
        hub = await store.create_object(hub_t.id, {"name": "hub"})
        # Link the hub out to MAX_FRONTIER + 1 leaves on one link_type.
        for i in range(MAX_FRONTIER + 1):
            leaf = await store.create_object(leaf_t.id, {"i": i})
            await store.link(hub.id, leaf.id, "has_leaf")

        with pytest.raises(ValueError, match="exceeding the cap"):
            await store.query(
                FabricQuery(
                    linked_to=hub.id,
                    path=[PathHop(link_type="has_leaf", object_type="Leaf", direction="out")],
                )
            )

    @pytest.mark.asyncio
    async def test_path_deeper_than_max_hops_rejected(self) -> None:
        """FabricQuery rejects a path deeper than MAX_HOPS at construction —
        before any DB work — with a clear ValueError."""
        with pytest.raises(ValueError, match="maximum is"):
            FabricQuery(path=[PathHop(link_type="r") for _ in range(MAX_HOPS + 1)])
