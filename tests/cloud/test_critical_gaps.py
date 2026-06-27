# Tests for critical gaps — real HTTP in connectors + agent tools for Fabric/Instinct.
# Created: 2026-03-28
# Updated: 2026-06-10 (W0d) — Added test_fabric_query_forwards_filters: the
#   FabricQueryTool must forward its `filters` arg into FabricQuery so chat-driven
#   property filters ("rent > X") narrow results instead of returning all objects.
# Updated: 2026-06-13 (feat/fabric-multihop) — Added test_fabric_query_forwards_path
#   and test_fabric_query_rejects_bad_hop: the FabricQueryTool must forward its
#   new `path` arg into FabricQuery.path so an LLM can issue the 2-hop ontology
#   join (open Deals -> Customer -> Competitor) as ONE tool call, and must turn a
#   malformed hop into a readable error string rather than raising.
# Updated: 2026-06-13 (review fixes #1465) — Added
#   test_fabric_query_too_deep_path_returns_clean_error: a path deeper than
#   MAX_HOPS is rejected at FabricQuery construction and surfaces through the
#   tool as a clean error string (no store call, no raised exception).

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.connectors.yaml_engine import DirectRESTAdapter, parse_connector_yaml

CONNECTORS_DIR = Path(__file__).parent.parent.parent / "connectors"


# --- Gap 1: Real HTTP in DirectRESTAdapter ---


class TestRealHTTP:
    @pytest.fixture
    def stripe_adapter(self) -> DirectRESTAdapter:
        defn = parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")
        adapter = DirectRESTAdapter(defn)
        return adapter

    @pytest.mark.asyncio
    async def test_execute_builds_auth_headers(self, stripe_adapter):
        await stripe_adapter.connect("p1", {"STRIPE_API_KEY": "sk_test_123"})
        headers = stripe_adapter._build_auth_headers()
        assert headers["Authorization"] == "Bearer sk_test_123"

    @pytest.mark.asyncio
    async def test_execute_local_action_skips_http(self):
        defn = parse_connector_yaml(CONNECTORS_DIR / "csv.yaml")
        adapter = DirectRESTAdapter(defn)
        await adapter.connect("p1", {})
        result = await adapter.execute("import_file", {"file_path": "/tmp/data.csv"})
        assert result.success is True
        assert result.data["action"] == "import_file"

    @pytest.mark.asyncio
    async def test_execute_not_connected(self, stripe_adapter):
        result = await stripe_adapter.execute("list_invoices", {})
        assert result.success is False
        assert result.error == "Not connected"

    @pytest.mark.asyncio
    async def test_execute_unknown_action(self, stripe_adapter):
        await stripe_adapter.connect("p1", {"STRIPE_API_KEY": "sk_test_123"})
        result = await stripe_adapter.execute("nonexistent", {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_makes_http_call(self, stripe_adapter):
        """Test that execute() calls httpx with correct method/url/headers."""
        await stripe_adapter.connect("p1", {"STRIPE_API_KEY": "sk_test_123"})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = [{"id": "inv_1", "amount_due": 5000}]
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await stripe_adapter.execute("list_invoices", {"limit": 5})

        assert result.success is True
        assert isinstance(result.data, list)
        assert result.data[0]["id"] == "inv_1"
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_handles_http_error(self, stripe_adapter):
        """Test that HTTP errors are caught and returned as ActionResult."""
        await stripe_adapter.connect("p1", {"STRIPE_API_KEY": "sk_test_123"})

        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=mock_response
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await stripe_adapter.execute("list_invoices", {})

        assert result.success is False
        assert "401" in result.error

    @pytest.mark.asyncio
    async def test_build_auth_basic(self):
        """Test basic auth header building."""
        defn = parse_connector_yaml(CONNECTORS_DIR / "rest_generic.yaml")
        adapter = DirectRESTAdapter(defn)
        # Override auth method for test
        adapter._def.auth["method"] = "basic"
        adapter._credentials = {"username": "user", "password": "pass"}
        headers = adapter._build_auth_headers()
        assert headers["Authorization"].startswith("Basic ")


# --- Gap 2: Agent Tools ---


class TestFabricTools:
    @pytest.mark.asyncio
    async def test_fabric_query_no_store(self):
        from pocketpaw.tools.builtin.fabric_tools import FabricQueryTool

        tool = FabricQueryTool()
        with patch("pocketpaw.tools.builtin.fabric_tools._get_fabric_store", return_value=None):
            result = await tool.execute(type_name="Customer")
        assert "not available" in result

    @pytest.mark.asyncio
    async def test_fabric_query_with_results(self):
        from pocketpaw.fabric.models import FabricObject, FabricQueryResult
        from pocketpaw.tools.builtin.fabric_tools import FabricQueryTool

        mock_store = MagicMock()
        mock_store.query = AsyncMock(
            return_value=FabricQueryResult(
                objects=[
                    FabricObject(
                        type_id="t1",
                        type_name="Customer",
                        properties={"name": "Acme", "revenue": 50000},
                    ),
                    FabricObject(
                        type_id="t1",
                        type_name="Customer",
                        properties={"name": "Beta Corp", "revenue": 30000},
                    ),
                ],
                total=2,
            )
        )

        tool = FabricQueryTool()
        with patch(
            "pocketpaw.tools.builtin.fabric_tools._get_fabric_store", return_value=mock_store
        ):
            result = await tool.execute(type_name="Customer")

        assert "Found 2" in result
        assert "Acme" in result
        assert "Beta Corp" in result

    @pytest.mark.asyncio
    async def test_fabric_query_forwards_filters(self, tmp_path):
        # W0d: the agent tool must pass `filters` through to FabricQuery so
        # chat-driven "rent > X" queries actually narrow. Use a real store so
        # the whole tool -> FabricQuery -> store.query path is exercised.
        from pocketpaw.fabric.store import FabricStore
        from pocketpaw.tools.builtin.fabric_tools import FabricQueryTool

        store = FabricStore(tmp_path / "fabric.db")
        t = await store.define_type(name="Lease", properties=[])
        await store.create_object(t.id, {"tenant": "Acme", "rent": 500})
        await store.create_object(t.id, {"tenant": "Globex", "rent": 2000})

        tool = FabricQueryTool()
        with patch("pocketpaw.tools.builtin.fabric_tools._get_fabric_store", return_value=store):
            result = await tool.execute(type_name="Lease", filters={"rent": {">": 1000}})

        assert "Found 1" in result
        assert "Globex" in result
        assert "Acme" not in result

    @pytest.mark.asyncio
    async def test_fabric_query_forwards_path(self, tmp_path):
        # feat/fabric-multihop: the agent tool must forward `path` into
        # FabricQuery.path so an LLM issues the 2-hop join as ONE call. Real
        # store so the whole tool -> FabricQuery -> store.query path runs.
        from pocketpaw.fabric.store import FabricStore
        from pocketpaw.tools.builtin.fabric_tools import FabricQueryTool

        store = FabricStore(tmp_path / "fabric.db")
        deal_t = await store.define_type(name="Deal", properties=[])
        cust_t = await store.define_type(name="Customer", properties=[])
        comp_t = await store.define_type(name="Competitor", properties=[])
        acme = await store.create_object(cust_t.id, {"name": "Acme"})
        rival = await store.create_object(comp_t.id, {"name": "Rival Inc"})
        open_deal = await store.create_object(deal_t.id, {"name": "Acme deal", "stage": "open"})
        won_deal = await store.create_object(deal_t.id, {"name": "Won deal", "stage": "won"})
        await store.link(open_deal.id, acme.id, "deal_for")
        await store.link(won_deal.id, acme.id, "deal_for")
        await store.link(acme.id, rival.id, "competes_with")

        tool = FabricQueryTool()
        with patch("pocketpaw.tools.builtin.fabric_tools._get_fabric_store", return_value=store):
            # From the Competitor, walk back to the OPEN Deals via the Customer.
            result = await tool.execute(
                linked_to=rival.id,
                path=[
                    {"link_type": "competes_with", "object_type": "Customer", "direction": "in"},
                    {
                        "link_type": "deal_for",
                        "object_type": "Deal",
                        "direction": "in",
                        "filters": {"stage": "open"},
                    },
                ],
            )

        assert "Found 1" in result
        assert "Acme deal" in result
        assert "Won deal" not in result

    @pytest.mark.asyncio
    async def test_fabric_query_rejects_bad_hop(self):
        # A malformed hop (unknown direction) must surface a readable error, not
        # raise — the LLM can read it and retry. No store call should happen.
        from pocketpaw.tools.builtin.fabric_tools import FabricQueryTool

        mock_store = MagicMock()
        mock_store.query = AsyncMock()

        tool = FabricQueryTool()
        with patch(
            "pocketpaw.tools.builtin.fabric_tools._get_fabric_store", return_value=mock_store
        ):
            result = await tool.execute(
                linked_to="obj-x",
                path=[{"link_type": "deal_for", "direction": "sideways"}],
            )

        assert "Invalid path hop at index 0" in result
        mock_store.query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fabric_query_too_deep_path_returns_clean_error(self):
        # review fixes #1465: a path deeper than MAX_HOPS is rejected when the
        # FabricQuery is built (inside the tool's try), so it surfaces as a clean
        # "Error querying Fabric" string rather than raising out of the tool.
        from pocketpaw.fabric.models import MAX_HOPS
        from pocketpaw.tools.builtin.fabric_tools import FabricQueryTool

        mock_store = MagicMock()
        mock_store.query = AsyncMock()

        tool = FabricQueryTool()
        with patch(
            "pocketpaw.tools.builtin.fabric_tools._get_fabric_store", return_value=mock_store
        ):
            result = await tool.execute(
                linked_to="obj-x",
                path=[{"link_type": "rel"} for _ in range(MAX_HOPS + 1)],
            )

        assert "Error querying Fabric" in result
        mock_store.query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fabric_create_object(self):
        from pocketpaw.fabric.models import FabricObject, ObjectType
        from pocketpaw.tools.builtin.fabric_tools import FabricCreateTool

        mock_store = MagicMock()
        mock_store.get_type_by_name = AsyncMock(
            return_value=ObjectType(name="Customer", properties=[])
        )
        mock_store.create_object = AsyncMock(
            return_value=FabricObject(
                type_id="t1",
                type_name="Customer",
                properties={"name": "Acme"},
            )
        )

        tool = FabricCreateTool()
        with patch(
            "pocketpaw.tools.builtin.fabric_tools._get_fabric_store", return_value=mock_store
        ):
            result = await tool.execute(
                action="create_object", type_name="Customer", properties={"name": "Acme"}
            )

        assert "Created Customer" in result
        assert "Acme" in result


class TestInstinctTools:
    @pytest.mark.asyncio
    async def test_propose_action(self):
        from pocketpaw.instinct.models import Action, ActionTrigger
        from pocketpaw.tools.builtin.instinct_tools import InstinctProposeTool

        mock_store = MagicMock()
        mock_store.propose = AsyncMock(
            return_value=Action(
                pocket_id="p1",
                title="Reorder inventory",
                description="Stock low",
                recommendation="Order 20 units",
                trigger=ActionTrigger(type="agent", source="pocketpaw", reason="low stock"),
            )
        )

        tool = InstinctProposeTool()
        with patch(
            "pocketpaw.tools.builtin.instinct_tools._get_instinct_store", return_value=mock_store
        ):
            result = await tool.execute(
                pocket_id="p1",
                title="Reorder inventory",
                recommendation="Order 20 units",
                reason="Stock below threshold",
            )

        assert "Action proposed" in result
        assert "Reorder inventory" in result
        assert "pending" in result

    @pytest.mark.asyncio
    async def test_pending_empty(self):
        from pocketpaw.tools.builtin.instinct_tools import InstinctPendingTool

        mock_store = MagicMock()
        mock_store.pending = AsyncMock(return_value=[])

        tool = InstinctPendingTool()
        with patch(
            "pocketpaw.tools.builtin.instinct_tools._get_instinct_store", return_value=mock_store
        ):
            result = await tool.execute()

        assert "all clear" in result

    @pytest.mark.asyncio
    async def test_audit_query(self):
        from pocketpaw.instinct.models import AuditEntry
        from pocketpaw.tools.builtin.instinct_tools import InstinctAuditTool

        mock_store = MagicMock()
        mock_store.query_audit = AsyncMock(
            return_value=[
                AuditEntry(
                    actor="agent:claude", event="action_proposed", description="Proposed: Reorder"
                ),
            ]
        )

        tool = InstinctAuditTool()
        with patch(
            "pocketpaw.tools.builtin.instinct_tools._get_instinct_store", return_value=mock_store
        ):
            result = await tool.execute(limit=5)

        assert "action_proposed" in result
        assert "Reorder" in result
