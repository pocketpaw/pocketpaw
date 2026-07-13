# tests/cloud/test_fabric_mcp.py — the agent-facing MCP surface for read-only
# Fabric ontology access (feat/fabric-instinct-mcp-providers).
#
# Created: 2026-06-11 (feat/fabric-instinct-mcp-providers).
#
# What this pins — the MCP tools, driven through the REAL handlers against a
# tmp-file FabricStore:
#   * tool-id / server-name contract (SERVER_NAME, *_TOOL_ID, *_TOOL_IDS —
#     fabric_query / fabric_stats names are pinned: a deployed skill calls them).
#   * the provider exposes the server + tool ids (extensions wiring).
#   * fabric_query resolves the workspace from ContextVars, runs the query
#     scoped to it (W4a: own rows + legacy NULL rows, never another tenant's),
#     forwards property filters, clamps the limit, and returns JSON-friendly
#     {total, returned, truncated, objects}.
#   * fabric_stats returns {types, objects, links, type_names}.
#   * results are size-capped: an oversized object list is truncated from the
#     tail and flagged truncated=true.
#   * error relaying: bad input types refuse cleanly; a store failure returns a
#     plain relayable error; missing identity refuses.
#
# Updated: 2026-07-11 (feat/paw-cli, C2) — the server grew three ontology
# MODIFICATION tools; this file now also pins:
#   * the extended tool-id contract (5 ids).
#   * fabric_link_create — creates a workspace-stamped link; refuses a
#     cross-tenant endpoint id; enforces the workspace's DECLARED link schema
#     through the router's own _enforce_link_type.
#   * fabric_link_delete — deletes through the scoped get_link guard; a
#     cross-tenant link id refuses and the row survives.
#   * fabric_type_update — RBAC-gated on fabric.admin (deny envelope for a
#     non-admin, never a write); renames migrate existing objects and bump the
#     type version; refuses an empty change set.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The handlers read
# identity through ee.cloud.chat.agent_service ContextVars (set in-test via
# attach_agent_identity) and the store through pocketpaw.stores.get_fabric_store
# (patched to a tmp-file store so nothing touches ~/.pocketpaw/fabric.db).
# The registry (declared link types / schema re-registration) is patched to a
# tmp-file WorkspaceFabricStore at BOTH import sites (router + storage).

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.fabric as fabric_mcp  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.extensions import CloudFabricMcpProvider  # noqa: E402

from pocketpaw.fabric.store import FabricStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> FabricStore:
    """Isolated FabricStore on a tmp file, wired in where the handlers read it
    (``pocketpaw.stores.get_fabric_store``)."""
    st = FabricStore(tmp_path / "fabric_mcp_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda: st)
    return st


async def _seed_customers(store: FabricStore) -> list[str]:
    """Define a Customer type and create three objects: two in workspace w1,
    one in workspace w2. Returns the w1 object ids."""
    obj_type = await store.define_type(name="Customer", properties=[])
    a = await store.create_object(
        type_id=obj_type.id,
        properties={"name": "Acme", "status": "active", "mrr": 100},
        workspace_id="w1",
    )
    b = await store.create_object(
        type_id=obj_type.id,
        properties={"name": "Bolt", "status": "churned", "mrr": 50},
        workspace_id="w1",
    )
    await store.create_object(
        type_id=obj_type.id,
        properties={"name": "Other-Tenant", "status": "active", "mrr": 999},
        workspace_id="w2",
    )
    return [a.id, b.id]


class _identity:
    """Context manager that sets the workspace/user/session ContextVars the
    handlers read, then resets them."""

    def __init__(self, *, workspace="w1", user="u1", session="sess-1"):
        self._ws, self._user, self._sess = workspace, user, session
        self._tokens = None

    def __enter__(self):
        self._tokens = attach_agent_identity(
            workspace_id=self._ws, user_id=self._user, session_mongo_id=self._sess
        )
        return self

    def __exit__(self, *exc):
        detach_agent_identity(self._tokens)
        return False


async def _result_body(res: dict) -> dict:
    """Parse the JSON body out of a success MCP response."""
    assert res.get("is_error") is not True, res
    return json.loads(res["content"][0]["text"])


# ---------------------------------------------------------------------------
# tool-id / provider contract pins
# ---------------------------------------------------------------------------


def test_tool_id_contract_pin() -> None:
    """The server + tool ids are the exact namespaced strings the allowlist
    machinery (and a deployed skill) match."""
    assert fabric_mcp.SERVER_NAME == "pocketpaw_fabric"
    assert fabric_mcp.FABRIC_QUERY_TOOL_ID == "mcp__pocketpaw_fabric__fabric_query"
    assert fabric_mcp.FABRIC_STATS_TOOL_ID == "mcp__pocketpaw_fabric__fabric_stats"
    assert fabric_mcp.FABRIC_LINK_CREATE_TOOL_ID == "mcp__pocketpaw_fabric__fabric_link_create"
    assert fabric_mcp.FABRIC_LINK_DELETE_TOOL_ID == "mcp__pocketpaw_fabric__fabric_link_delete"
    assert fabric_mcp.FABRIC_TYPE_UPDATE_TOOL_ID == "mcp__pocketpaw_fabric__fabric_type_update"
    assert fabric_mcp.FABRIC_TOOL_IDS == (
        "mcp__pocketpaw_fabric__fabric_query",
        "mcp__pocketpaw_fabric__fabric_stats",
        "mcp__pocketpaw_fabric__fabric_link_create",
        "mcp__pocketpaw_fabric__fabric_link_delete",
        "mcp__pocketpaw_fabric__fabric_type_update",
    )


def test_provider_exposes_server_and_tool_ids() -> None:
    """The extensions provider builds the server and reports the tool ids — the
    pocketpaw.mcp_servers registration loop reads both."""
    provider = CloudFabricMcpProvider()
    assert provider.tool_ids() == list(fabric_mcp.FABRIC_TOOL_IDS)

    built = provider.build_server()
    if built is not None:
        name, server = built
        assert name == "pocketpaw_fabric"
        assert server is not None


# ---------------------------------------------------------------------------
# fabric_query — the REAL read path
# ---------------------------------------------------------------------------


async def test_query_returns_workspace_scoped_objects(store):
    """Objects come back JSON-friendly and scoped to the caller's workspace —
    another tenant's rows never appear."""
    w1_ids = await _seed_customers(store)

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_query_handler({"type_name": "Customer"})

    body = await _result_body(res)
    assert body["total"] == 2
    assert body["returned"] == 2
    assert body["truncated"] is False
    got_ids = {o["id"] for o in body["objects"]}
    assert got_ids == set(w1_ids)
    names = {o["properties"]["name"] for o in body["objects"]}
    assert names == {"Acme", "Bolt"}
    assert "Other-Tenant" not in names
    for o in body["objects"]:
        assert o["type_name"] == "Customer"


async def test_query_forwards_filters(store):
    """Property filters reach the store — only matching objects return."""
    await _seed_customers(store)

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_query_handler(
            {"type_name": "Customer", "filters": {"status": "active"}}
        )

    body = await _result_body(res)
    assert body["total"] == 1
    assert body["objects"][0]["properties"]["name"] == "Acme"


async def test_query_clamps_limit(store):
    """A limit over the cap is clamped, not refused."""
    await _seed_customers(store)

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_query_handler({"type_name": "Customer", "limit": 10_000})

    body = await _result_body(res)
    assert body["returned"] <= fabric_mcp.MAX_QUERY_LIMIT


async def test_query_truncates_oversized_results(store, monkeypatch):
    """An object list over the byte budget is truncated from the tail and
    flagged — the agent sees truncated=true instead of a blown context."""
    await _seed_customers(store)
    monkeypatch.setattr(fabric_mcp, "MAX_RESULT_BYTES", 80)

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_query_handler({"type_name": "Customer"})

    body = await _result_body(res)
    assert body["truncated"] is True
    assert body["returned"] < body["total"]


# ---------------------------------------------------------------------------
# fabric_stats
# ---------------------------------------------------------------------------


async def test_stats_returns_counts_and_type_names(store):
    """Stats come back JSON-friendly with counts + type names, scoped to the
    caller's workspace.

    fix/fabric-stats-workspace-scope: the seed puts 2 Customer rows in w1 and 1
    in w2. A w1-scoped stats counts only w1's 2 objects (NOT the instance-wide
    3) so it agrees with fabric_query.
    """
    await _seed_customers(store)

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_stats_handler({})

    body = await _result_body(res)
    assert body["types"] == 1
    assert body["objects"] == 2  # w1's two rows — NOT the instance-wide 3
    assert body["type_names"] == ["Customer"]
    assert "links" in body


async def test_stats_does_not_leak_other_tenant_type_names(store):
    """The live leak, pinned at the MCP boundary.

    w2 alone models "Lease" / "Lease2"; w1 models "Customer". A w1-scoped
    fabric_stats must NOT name w2's experimental types. SZD-2 — type isolation
    is by who DEFINED the type (its own workspace_id), so w2's experimental
    types are stamped with workspace="w2".
    """
    await _seed_customers(store)  # Customer rows in w1 (+ one in w2)
    lease = await store.define_type(name="Lease", properties=[], workspace_id="w2")
    lease2 = await store.define_type(name="Lease2", properties=[], workspace_id="w2")
    await store.create_object(lease.id, {"tenant": "X"}, workspace_id="w2")
    await store.create_object(lease2.id, {"tenant": "Y"}, workspace_id="w2")

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_stats_handler({})
    body = await _result_body(res)

    assert body["type_names"] == ["Customer"]
    assert "Lease" not in body["type_names"]
    assert "Lease2" not in body["type_names"]
    assert body["types"] == 1  # count matches the visible type list


async def test_stats_agrees_with_query_object_count(store):
    """The stats/query consistency invariant that surfaced the bug: a w1-scoped
    stats object count equals the total of a w1-scoped (unfiltered) query."""
    await _seed_customers(store)  # 2 in w1, 1 in w2

    with _identity(workspace="w1"):
        stats_res = await fabric_mcp._fabric_stats_handler({})
        query_res = await fabric_mcp._fabric_query_handler({})

    stats_body = await _result_body(stats_res)
    query_body = await _result_body(query_res)
    assert stats_body["objects"] == query_body["total"] == 2


# ---------------------------------------------------------------------------
# workspace resolution + error relaying
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler,args",
    [
        (fabric_mcp._fabric_query_handler, {"type_name": "Customer"}),
        (fabric_mcp._fabric_stats_handler, {}),
    ],
)
async def test_identity_missing_errors(store, handler, args):
    """Called without workspace ContextVars → an explicit error."""
    res = await handler(args)
    assert res.get("is_error") is True
    assert "workspace context" in res["content"][0]["text"]


@pytest.mark.parametrize(
    "args,needle",
    [
        ({"type_name": 42}, "`type_name`"),
        ({"filters": "not-an-object"}, "`filters`"),
        ({"limit": 0}, "`limit`"),
        ({"limit": "ten"}, "`limit`"),
    ],
)
async def test_bad_input_refused(store, args, needle):
    """Malformed inputs refuse cleanly with the offending field named."""
    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_query_handler(args)
    assert res.get("is_error") is True
    assert needle in res["content"][0]["text"]


async def test_store_error_is_relayed(store, monkeypatch):
    """A store failure is relayed as a plain error, not a crash."""

    class _Boom:
        async def query(self, q, workspace_id=None):
            raise RuntimeError("fabric db is locked")

    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda: _Boom())
    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_query_handler({"type_name": "Customer"})
    assert res.get("is_error") is True
    assert "fabric db is locked" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# C2 modification tools — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path, monkeypatch):
    """Isolated workspace-registry store, patched at BOTH call sites: the
    router's module-level import (used by _enforce_link_type) and the storage
    factory (used by the type-update handler's re-registration)."""
    from pocketpaw_ee.fabric.storage import WorkspaceFabricStore

    reg = WorkspaceFabricStore(tmp_path / "fabric_registry_test.db")
    monkeypatch.setattr("pocketpaw_ee.fabric.router.get_registry_store", lambda: reg)
    monkeypatch.setattr("pocketpaw_ee.fabric.storage.get_registry_store", lambda: reg)
    return reg


@pytest.fixture
def admin_ok(monkeypatch):
    """Make the fabric.admin RBAC gate PASS: a resolvable user + a no-op check."""

    async def _fake_load_user(user_id):
        return object()

    monkeypatch.setattr(fabric_mcp, "_load_user", _fake_load_user)
    monkeypatch.setattr(
        "pocketpaw_ee.guards.deps.check_workspace_action", lambda user, ws, action: None
    )


async def _seed_link(store: FabricStore, workspace: str = "w1"):
    """Two Customer objects + one link between them in ``workspace``.
    Returns (from_id, to_id, link_id)."""
    obj_type = await store.define_type(name=f"Cust-{workspace}", properties=[])
    a = await store.create_object(obj_type.id, {"name": "A"}, workspace_id=workspace)
    b = await store.create_object(obj_type.id, {"name": "B"}, workspace_id=workspace)
    lnk = await store.link(a.id, b.id, "knows", workspace_id=workspace)
    return a.id, b.id, lnk.id


# ---------------------------------------------------------------------------
# fabric_link_create
# ---------------------------------------------------------------------------


async def test_link_create_stamps_workspace(store, registry):
    """A created link lands workspace-stamped — visible to w1, not to w2."""
    w1_ids = await _seed_customers(store)

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_link_create_handler(
            {"from_id": w1_ids[0], "to_id": w1_ids[1], "link_type": "competes_with"}
        )

    body = await _result_body(res)
    assert body["created"] is True
    assert body["link"]["from_object_id"] == w1_ids[0]
    assert body["link"]["link_type"] == "competes_with"

    links_w1, total_w1 = await store.list_links(workspace_id="w1")
    assert total_w1 == 1
    _, total_w2 = await store.list_links(workspace_id="w2")
    assert total_w2 == 0


async def test_link_create_refuses_cross_tenant_endpoint(store, registry):
    """An endpoint id belonging to another workspace refuses — no link written."""
    await _seed_customers(store)
    # The w2 object (scoped read from w1 must not resolve it).
    w2_objs = await store.query(
        __import__("pocketpaw.fabric.models", fromlist=["FabricQuery"]).FabricQuery(),
        workspace_id="w2",
    )
    w2_id = w2_objs.objects[0].id
    w1_ids = await _seed_customers(store)

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_link_create_handler(
            {"from_id": w1_ids[0], "to_id": w2_id, "link_type": "knows"}
        )

    assert res.get("is_error") is True
    assert "not found in this workspace" in res["content"][0]["text"]
    _, total = await store.list_links(workspace_id="w1")
    assert total == 0


async def test_link_create_enforces_declared_schema(store, registry):
    """With a declared link schema, an unregistered link type refuses — the
    router's own enforcement runs behind the MCP tool."""
    w1_ids = await _seed_customers(store)
    registry.register_link("w1", "has_order", "Customer", "Order")

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_link_create_handler(
            {"from_id": w1_ids[0], "to_id": w1_ids[1], "link_type": "made_up_type"}
        )

    assert res.get("is_error") is True
    assert "made_up_type" in res["content"][0]["text"]
    _, total = await store.list_links(workspace_id="w1")
    assert total == 0


@pytest.mark.parametrize("missing", ["from_id", "to_id", "link_type"])
async def test_link_create_requires_fields(store, registry, missing):
    args = {"from_id": "a", "to_id": "b", "link_type": "knows"}
    del args[missing]
    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_link_create_handler(args)
    assert res.get("is_error") is True
    assert f"`{missing}`" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# fabric_link_delete
# ---------------------------------------------------------------------------


async def test_link_delete_removes_own_link(store):
    _, _, link_id = await _seed_link(store, workspace="w1")

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_link_delete_handler({"link_id": link_id})

    body = await _result_body(res)
    assert body["deleted"] is True
    assert body["link"]["id"] == link_id
    _, total = await store.list_links(workspace_id="w1")
    assert total == 0


async def test_link_delete_refuses_cross_tenant_link(store):
    """Another tenant's link id refuses, and the row SURVIVES."""
    _, _, w2_link_id = await _seed_link(store, workspace="w2")

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_link_delete_handler({"link_id": w2_link_id})

    assert res.get("is_error") is True
    assert "not found in this workspace" in res["content"][0]["text"]
    _, total = await store.list_links(workspace_id="w2")
    assert total == 1


async def test_link_delete_requires_link_id(store):
    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_link_delete_handler({})
    assert res.get("is_error") is True
    assert "`link_id`" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# fabric_type_update
# ---------------------------------------------------------------------------


async def test_type_update_renames_and_bumps_version(store, registry, admin_ok):
    """A rename migrates existing objects and bumps the type version."""
    from pocketpaw.fabric.models import PropertyDef

    obj_type = await store.define_type(
        name="Customer",
        properties=[PropertyDef(name="name", type="string")],
        workspace_id="w1",
    )
    obj = await store.create_object(obj_type.id, {"name": "Acme"}, workspace_id="w1")

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_type_update_handler(
            {"type_name": "Customer", "renames": {"name": "full_name"}}
        )

    body = await _result_body(res)
    assert body["updated"] is True
    assert body["type"]["version"] == 2

    migrated = await store.get_object(obj.id, workspace_id="w1")
    assert migrated.properties.get("full_name") == "Acme"
    assert "name" not in migrated.properties


async def test_type_update_denied_for_non_admin(store, registry, monkeypatch):
    """A non-admin gets a structured deny envelope and NO write happens."""
    from pocketpaw_ee.guards.rbac import Forbidden

    async def _fake_load_user(user_id):
        return object()

    def _deny(user, ws, action):
        raise Forbidden("workspace.insufficient_role", "admin required")

    monkeypatch.setattr(fabric_mcp, "_load_user", _fake_load_user)
    monkeypatch.setattr("pocketpaw_ee.guards.deps.check_workspace_action", _deny)

    obj_type = await store.define_type(name="Customer", properties=[], workspace_id="w1")

    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_type_update_handler(
            {"type_name": "Customer", "description": "hacked"}
        )

    body = await _result_body(res)
    assert body["denied"] is True
    assert body["ok"] is False
    unchanged = await store.get_type(obj_type.id, workspace_id="w1")
    assert unchanged.description != "hacked"


async def test_type_update_requires_a_change(store, registry, admin_ok):
    await store.define_type(name="Customer", properties=[], workspace_id="w1")
    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_type_update_handler({"type_name": "Customer"})
    assert res.get("is_error") is True
    assert "at least one change" in res["content"][0]["text"]


async def test_type_update_unknown_type_refuses(store, registry, admin_ok):
    with _identity(workspace="w1"):
        res = await fabric_mcp._fabric_type_update_handler(
            {"type_name": "Ghost", "description": "x"}
        )
    assert res.get("is_error") is True
    assert "Ghost" in res["content"][0]["text"]


@pytest.mark.parametrize(
    "handler,args",
    [
        (fabric_mcp._fabric_link_create_handler, {"from_id": "a", "to_id": "b", "link_type": "t"}),
        (fabric_mcp._fabric_link_delete_handler, {"link_id": "x"}),
        (fabric_mcp._fabric_type_update_handler, {"type_name": "Customer", "description": "d"}),
    ],
)
async def test_write_tools_require_identity(store, handler, args):
    """Called without workspace ContextVars → an explicit error, no write."""
    res = await handler(args)
    assert res.get("is_error") is True
    assert "workspace context" in res["content"][0]["text"]
