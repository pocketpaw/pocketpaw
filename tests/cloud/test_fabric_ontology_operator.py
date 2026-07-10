# test_fabric_ontology_operator.py — the ontology-operator-ux backend.
# Created: 2026-07-10 (feat/ontology-operator-ux) — covers the three backend
# criteria that make the Fabric ontology operable by a non-engineer:
#   1. AUTHORING endpoints (/fabric/schema/*): create a typed object type, add a
#      property, declare a link type, and the GET /fabric/schema list reflects
#      them. Tenant-scoped and ADMIN-gated (fabric.admin) — a member is denied
#      (403) and an unauthenticated caller gets 401.
#   2. WRITE-TIME TYPE ENFORCEMENT: a wrong-typed property is rejected (422 over
#      HTTP, FabricTypeError at the store) and a valid one accepted; a link that
#      violates the declared link schema (unknown type, or wrong endpoints) is
#      rejected, a matching one accepted.
#   3. SCHEMA VERSIONING: update_type bumps the version, a property rename
#      migrates existing objects, an additive property with a default is
#      backfilled, and a property dropped from the schema leaves its orphaned key
#      on existing objects (the documented deferred-removal behaviour).
#
# The store-level tests drive the real FabricStore against a tmp SQLite file; the
# HTTP tests wire the real EE router + real RBAC (fabric.read/write/admin) with an
# isolated per-workspace store, spying on behaviour rather than mocking the seam
# under test.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pocketpaw_ee.fabric.router as fabric_router_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license

from pocketpaw.fabric.models import FabricTypeError, PropertyDef
from pocketpaw.fabric.store import FabricStore

# ---------------------------------------------------------------------------
# Store-level: write-time type enforcement
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> FabricStore:
    return FabricStore(tmp_path / "test.db")


class TestWriteTimeTypeEnforcement:
    @pytest.mark.asyncio
    async def test_rejects_wrong_typed_property_on_create(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Customer",
            properties=[
                PropertyDef(name="name", type="string"),
                PropertyDef(name="revenue", type="number"),
            ],
        )
        with pytest.raises(FabricTypeError):
            await store.create_object(t.id, {"name": "Acme", "revenue": "not-a-number"})

    @pytest.mark.asyncio
    async def test_accepts_valid_typed_property_on_create(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Customer",
            properties=[
                PropertyDef(name="name", type="string"),
                PropertyDef(name="revenue", type="number"),
            ],
        )
        obj = await store.create_object(t.id, {"name": "Acme", "revenue": 75000})
        assert obj.properties["revenue"] == 75000

    @pytest.mark.asyncio
    async def test_numeric_string_is_coerced_ok(self, store: FabricStore) -> None:
        # A connector often ships a number as a string; enforcement must stay
        # lenient enough not to reject "75000" for a number field.
        t = await store.define_type(
            name="Customer", properties=[PropertyDef(name="revenue", type="number")]
        )
        obj = await store.create_object(t.id, {"revenue": "75000"})
        assert obj.properties["revenue"] == "75000"

    @pytest.mark.asyncio
    async def test_enum_membership_enforced(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Deal",
            properties=[
                PropertyDef(name="stage", type="enum", enum_values=["open", "won", "lost"]),
            ],
        )
        with pytest.raises(FabricTypeError):
            await store.create_object(t.id, {"stage": "banana"})
        ok = await store.create_object(t.id, {"stage": "won"})
        assert ok.properties["stage"] == "won"

    @pytest.mark.asyncio
    async def test_undeclared_property_passes_through(self, store: FabricStore) -> None:
        # The schema is open/additive: an extra key that the type does not declare
        # is not a type clash and must be allowed.
        t = await store.define_type(
            name="Customer", properties=[PropertyDef(name="name", type="string")]
        )
        obj = await store.create_object(t.id, {"name": "Acme", "note": {"any": "shape"}})
        assert obj.properties["note"] == {"any": "shape"}

    @pytest.mark.asyncio
    async def test_empty_schema_is_no_op(self, store: FabricStore) -> None:
        # A type with no declared properties enforces nothing (backward compat).
        t = await store.define_type(name="Freeform", properties=[])
        obj = await store.create_object(t.id, {"whatever": 123})
        assert obj.properties["whatever"] == 123

    @pytest.mark.asyncio
    async def test_update_validates_provided_delta(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Customer", properties=[PropertyDef(name="revenue", type="number")]
        )
        obj = await store.create_object(t.id, {"revenue": 100})
        with pytest.raises(FabricTypeError):
            await store.update_object(obj.id, {"revenue": "still-not-a-number"})
        # A valid update goes through.
        updated = await store.update_object(obj.id, {"revenue": 200})
        assert updated is not None
        assert updated.properties["revenue"] == 200


# ---------------------------------------------------------------------------
# Store-level: schema versioning + non-destructive migration
# ---------------------------------------------------------------------------


class TestSchemaVersioning:
    @pytest.mark.asyncio
    async def test_new_type_starts_at_version_one(self, store: FabricStore) -> None:
        t = await store.define_type(name="Customer", properties=[])
        assert t.version == 1
        fetched = await store.get_type(t.id)
        assert fetched is not None
        assert fetched.version == 1

    @pytest.mark.asyncio
    async def test_update_bumps_version(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Customer", properties=[PropertyDef(name="name", type="string")]
        )
        updated = await store.update_type(
            t.id, properties=[PropertyDef(name="name", type="string")]
        )
        assert updated is not None
        assert updated.version == 2
        assert (await store.get_type(t.id)).version == 2  # persisted

    @pytest.mark.asyncio
    async def test_rename_migrates_existing_objects(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Customer", properties=[PropertyDef(name="revenue", type="number")]
        )
        obj = await store.create_object(t.id, {"revenue": 500})
        updated = await store.update_type(
            t.id,
            properties=[PropertyDef(name="annual_revenue", type="number")],
            renames={"revenue": "annual_revenue"},
        )
        assert updated is not None
        assert updated.version == 2
        assert [p.name for p in updated.properties] == ["annual_revenue"]
        # The existing object's key was moved, value preserved.
        migrated = await store.get_object(obj.id)
        assert migrated is not None
        assert "revenue" not in migrated.properties
        assert migrated.properties["annual_revenue"] == 500

    @pytest.mark.asyncio
    async def test_additive_property_with_default_is_backfilled(self, store: FabricStore) -> None:
        t = await store.define_type(
            name="Customer", properties=[PropertyDef(name="name", type="string")]
        )
        obj = await store.create_object(t.id, {"name": "Acme"})
        updated = await store.update_type(
            t.id,
            properties=[
                PropertyDef(name="name", type="string"),
                PropertyDef(name="tier", type="string", default="standard"),
            ],
        )
        assert updated is not None
        assert updated.version == 2
        migrated = await store.get_object(obj.id)
        assert migrated is not None
        assert migrated.properties["tier"] == "standard"  # backfilled onto the old object

    @pytest.mark.asyncio
    async def test_dropped_property_leaves_orphaned_key(self, store: FabricStore) -> None:
        # DEFERRED destructive removal: dropping a property from the schema does
        # NOT scrub the key from existing objects — it is left orphaned. This is
        # the documented behaviour asserted here so a future purge is a
        # deliberate, separate change.
        t = await store.define_type(
            name="Customer",
            properties=[
                PropertyDef(name="name", type="string"),
                PropertyDef(name="legacy_code", type="string"),
            ],
        )
        obj = await store.create_object(t.id, {"name": "Acme", "legacy_code": "XYZ"})
        updated = await store.update_type(
            t.id, properties=[PropertyDef(name="name", type="string")]
        )
        assert updated is not None
        assert [p.name for p in updated.properties] == ["name"]  # declaration dropped
        orphaned = await store.get_object(obj.id)
        assert orphaned is not None
        assert orphaned.properties["legacy_code"] == "XYZ"  # data survives, orphaned

    @pytest.mark.asyncio
    async def test_update_unknown_type_returns_none(self, store: FabricStore) -> None:
        assert await store.update_type("ot-nope", properties=[]) is None

    @pytest.mark.asyncio
    async def test_update_type_workspace_scoped(self, store: FabricStore) -> None:
        # A type owned by ws-b cannot be updated from a ws-a-scoped call.
        b_type = await store.define_type(name="Secret", properties=[], workspace_id="ws-b")
        assert await store.update_type(b_type.id, properties=[], workspace_id="ws-a") is None
        # Its owner can.
        updated = await store.update_type(b_type.id, properties=[], workspace_id="ws-b")
        assert updated is not None
        assert updated.version == 2


# ---------------------------------------------------------------------------
# HTTP: authoring endpoints, admin gate, and write-time enforcement over the wire
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, role: str, workspace_id: str = "ws-test") -> None:
        self.id = f"user-{role}"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role=role)]


def _build_client(tmp_path: Path, monkeypatch, *, role: str | None) -> TestClient:
    """An isolated app with the real fabric router + real RBAC.

    ``role`` sets the fake user's workspace role (``admin`` / ``member``). When
    ``role`` is ``None`` no auth override is installed, so an unauthenticated
    request hits the real fastapi-users dependency and gets a 401.
    """
    import pocketpaw.stores as stores

    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    stores.reset_store_caches()

    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(fabric_router_module.router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    if role is not None:
        user = _FakeUser(role=role)
        app.dependency_overrides[current_active_user] = lambda: user
        app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def admin_client(tmp_path: Path, monkeypatch) -> TestClient:
    return _build_client(tmp_path, monkeypatch, role="admin")


@pytest.fixture
def member_client(tmp_path: Path, monkeypatch) -> TestClient:
    return _build_client(tmp_path, monkeypatch, role="member")


class TestAuthoringEndpoints:
    def test_create_type_add_property_and_schema_reflects_them(
        self, admin_client: TestClient
    ) -> None:
        # Create a typed object type.
        resp = admin_client.post(
            "/api/v1/fabric/schema/types",
            json={
                "name": "Customer",
                "properties": [{"name": "name", "type": "string"}],
            },
        )
        assert resp.status_code == 201, resp.text
        type_id = resp.json()["id"]
        assert resp.json()["version"] == 1

        # Add a property (additive) — bumps the version.
        add = admin_client.post(
            f"/api/v1/fabric/schema/types/{type_id}/properties",
            json={"property": {"name": "revenue", "type": "number"}},
        )
        assert add.status_code == 200, add.text
        assert add.json()["version"] == 2
        assert {p["name"] for p in add.json()["properties"]} == {"name", "revenue"}

        # Declare a link type.
        link = admin_client.post(
            "/api/v1/fabric/schema/link-types",
            json={"name": "has_order", "from_type": "Customer", "to_type": "Order"},
        )
        assert link.status_code == 201, link.text

        # The schema list reflects the object type (with properties) + link type.
        schema = admin_client.get("/api/v1/fabric/schema")
        assert schema.status_code == 200, schema.text
        body = schema.json()
        names = {t["name"] for t in body["object_types"]}
        assert "Customer" in names
        customer = next(t for t in body["object_types"] if t["name"] == "Customer")
        assert {p["name"] for p in customer["properties"]} == {"name", "revenue"}
        assert body["link_types"] == [
            {"name": "has_order", "from_type": "Customer", "to_type": "Order"}
        ]

    def test_add_duplicate_property_rejected(self, admin_client: TestClient) -> None:
        t = admin_client.post(
            "/api/v1/fabric/schema/types",
            json={"name": "Customer", "properties": [{"name": "name", "type": "string"}]},
        ).json()
        dup = admin_client.post(
            f"/api/v1/fabric/schema/types/{t['id']}/properties",
            json={"property": {"name": "name", "type": "string"}},
        )
        assert dup.status_code == 422

    def test_patch_type_rename_versions_over_http(self, admin_client: TestClient) -> None:
        t = admin_client.post(
            "/api/v1/fabric/schema/types",
            json={"name": "Customer", "properties": [{"name": "revenue", "type": "number"}]},
        ).json()
        patch = admin_client.patch(
            f"/api/v1/fabric/schema/types/{t['id']}",
            json={
                "properties": [{"name": "annual_revenue", "type": "number"}],
                "renames": {"revenue": "annual_revenue"},
            },
        )
        assert patch.status_code == 200, patch.text
        assert patch.json()["version"] == 2
        assert [p["name"] for p in patch.json()["properties"]] == ["annual_revenue"]


class TestAdminGate:
    def test_member_denied_on_authoring(self, member_client: TestClient) -> None:
        resp = member_client.post(
            "/api/v1/fabric/schema/types",
            json={"name": "Customer", "properties": []},
        )
        assert resp.status_code == 403, resp.text

    def test_member_denied_on_link_type(self, member_client: TestClient) -> None:
        resp = member_client.post(
            "/api/v1/fabric/schema/link-types",
            json={"name": "has_order", "from_type": "Customer", "to_type": "Order"},
        )
        assert resp.status_code == 403

    def test_member_can_read_schema(self, member_client: TestClient) -> None:
        # The schema LIST is fabric.read — a member can render the browser.
        resp = member_client.get("/api/v1/fabric/schema")
        assert resp.status_code == 200

    def test_unauthenticated_denied(self, tmp_path: Path, monkeypatch) -> None:
        anon = _build_client(tmp_path, monkeypatch, role=None)
        resp = anon.post(
            "/api/v1/fabric/schema/types",
            json={"name": "Customer", "properties": []},
        )
        assert resp.status_code == 401


class TestWriteTimeEnforcementOverHTTP:
    def test_object_wrong_typed_property_rejected(self, admin_client: TestClient) -> None:
        t = admin_client.post(
            "/api/v1/fabric/schema/types",
            json={"name": "Customer", "properties": [{"name": "revenue", "type": "number"}]},
        ).json()
        bad = admin_client.post(
            "/api/v1/fabric/objects",
            json={"type_id": t["id"], "properties": {"revenue": "not-a-number"}},
        )
        assert bad.status_code == 422, bad.text
        assert bad.json()["error"]["code"] == "fabric.property_type_mismatch"

        ok = admin_client.post(
            "/api/v1/fabric/objects",
            json={"type_id": t["id"], "properties": {"revenue": 42}},
        )
        assert ok.status_code == 201, ok.text

    def test_link_enforced_against_declared_schema(self, admin_client: TestClient) -> None:
        # Author two types + a link type Customer --has_order--> Order.
        cust = admin_client.post(
            "/api/v1/fabric/schema/types", json={"name": "Customer", "properties": []}
        ).json()
        order = admin_client.post(
            "/api/v1/fabric/schema/types", json={"name": "Order", "properties": []}
        ).json()
        admin_client.post(
            "/api/v1/fabric/schema/link-types",
            json={"name": "has_order", "from_type": "Customer", "to_type": "Order"},
        )

        c = admin_client.post(
            "/api/v1/fabric/objects", json={"type_id": cust["id"], "properties": {}}
        ).json()
        o = admin_client.post(
            "/api/v1/fabric/objects", json={"type_id": order["id"], "properties": {}}
        ).json()

        # A valid link (Customer -> Order via has_order) is accepted.
        good = admin_client.post(
            "/api/v1/fabric/links",
            json={"from_id": c["id"], "to_id": o["id"], "link_type": "has_order"},
        )
        assert good.status_code == 201, good.text

        # An unregistered link type is rejected.
        unknown = admin_client.post(
            "/api/v1/fabric/links",
            json={"from_id": c["id"], "to_id": o["id"], "link_type": "invented"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "fabric.link_type_unregistered"

        # A declared type used with the WRONG endpoints (Order -> Customer) is
        # rejected — the declaration is directional.
        wrong = admin_client.post(
            "/api/v1/fabric/links",
            json={"from_id": o["id"], "to_id": c["id"], "link_type": "has_order"},
        )
        assert wrong.status_code == 422
        assert wrong.json()["error"]["code"] == "fabric.link_type_mismatch"

    def test_links_unenforced_when_no_link_schema(self, admin_client: TestClient) -> None:
        # A workspace that declared no link types keeps the pre-enforcement
        # behaviour: any link_type is accepted (backward compatible).
        t = admin_client.post(
            "/api/v1/fabric/schema/types", json={"name": "Node", "properties": []}
        ).json()
        a = admin_client.post(
            "/api/v1/fabric/objects", json={"type_id": t["id"], "properties": {}}
        ).json()
        b = admin_client.post(
            "/api/v1/fabric/objects", json={"type_id": t["id"], "properties": {}}
        ).json()
        resp = admin_client.post(
            "/api/v1/fabric/links",
            json={"from_id": a["id"], "to_id": b["id"], "link_type": "anything_goes"},
        )
        assert resp.status_code == 201, resp.text
