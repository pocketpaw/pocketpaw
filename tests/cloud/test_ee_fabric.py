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

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from pocketpaw.fabric.models import FabricQuery, PropertyDef
from pocketpaw.fabric.store import FabricStore


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
