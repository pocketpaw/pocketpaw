# Tests for critical gaps — real HTTP in connectors + agent tools for Fabric/Instinct.
# Created: 2026-03-28

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.connectors.yaml_engine import DirectRESTAdapter, parse_connector_yaml

CONNECTORS_DIR = Path(__file__).parent.parent / "connectors"


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
        from ee.fabric.models import FabricObject, FabricQueryResult
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
    async def test_fabric_create_object(self):
        from ee.fabric.models import FabricObject, ObjectType
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
        from ee.instinct.models import Action, ActionTrigger
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
        from ee.instinct.models import AuditEntry
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
