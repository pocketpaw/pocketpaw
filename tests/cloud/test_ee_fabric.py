# Tests for ee/fabric — ontology store (SQLite).
# Created: 2026-03-28

from __future__ import annotations

from pathlib import Path

import pytest

from ee.fabric.models import FabricQuery, PropertyDef
from ee.fabric.store import FabricStore


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
