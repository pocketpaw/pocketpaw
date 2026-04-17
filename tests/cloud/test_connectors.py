# Tests for ConnectorProtocol — YAML parsing, registry, adapter lifecycle.
# Created: 2026-03-27

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.connectors.protocol import ConnectorStatus, TrustLevel
from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.yaml_engine import DirectRESTAdapter, parse_connector_yaml

CONNECTORS_DIR = Path(__file__).parent.parent / "connectors"


class TestYAMLParsing:
    """Test parsing connector YAML definitions."""

    def test_parse_stripe_yaml(self) -> None:
        defn = parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")
        assert defn.name == "stripe"
        assert defn.display_name == "Stripe"
        assert defn.type == "payment"
        assert defn.icon == "credit-card"
        assert len(defn.actions) == 3
        assert defn.auth["method"] == "api_key"

    def test_parse_csv_yaml(self) -> None:
        defn = parse_connector_yaml(CONNECTORS_DIR / "csv.yaml")
        assert defn.name == "csv"
        assert defn.auth["method"] == "none"
        assert len(defn.actions) == 2

    def test_parse_generic_rest_yaml(self) -> None:
        defn = parse_connector_yaml(CONNECTORS_DIR / "rest_generic.yaml")
        assert defn.name == "rest_generic"
        assert defn.display_name == "REST API"

    def test_action_schemas(self) -> None:
        defn = parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")
        # create_invoice should have trust_level: confirm
        create = next(a for a in defn.actions if a["name"] == "create_invoice")
        assert create["trust_level"] == "confirm"

    def test_sync_config(self) -> None:
        defn = parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")
        assert defn.sync["table"] == "stripe_invoices"
        assert defn.sync["schedule"] == "every_15m"
        assert "amount" in defn.sync["mapping"]


class TestDirectRESTAdapter:
    """Test the DirectREST adapter lifecycle."""

    @pytest.fixture
    def stripe_adapter(self) -> DirectRESTAdapter:
        defn = parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")
        return DirectRESTAdapter(defn)

    @pytest.mark.asyncio
    async def test_connect_success(self, stripe_adapter: DirectRESTAdapter) -> None:
        result = await stripe_adapter.connect("pocket-1", {"STRIPE_API_KEY": "sk_test_123"})
        assert result.success is True
        assert result.status == ConnectorStatus.CONNECTED
        assert "stripe_invoices" in result.tables_created

    @pytest.mark.asyncio
    async def test_connect_missing_credential(self, stripe_adapter: DirectRESTAdapter) -> None:
        result = await stripe_adapter.connect("pocket-1", {})
        assert result.success is False
        assert "STRIPE_API_KEY" in result.message

    @pytest.mark.asyncio
    async def test_list_actions(self, stripe_adapter: DirectRESTAdapter) -> None:
        actions = await stripe_adapter.actions()
        assert len(actions) == 3
        names = [a.name for a in actions]
        assert "list_invoices" in names
        assert "create_invoice" in names

        create = next(a for a in actions if a.name == "create_invoice")
        assert create.trust_level == TrustLevel.CONFIRM

    @pytest.mark.asyncio
    async def test_execute_not_connected(self, stripe_adapter: DirectRESTAdapter) -> None:
        result = await stripe_adapter.execute("list_invoices", {})
        assert result.success is False
        assert result.error == "Not connected"

    @pytest.mark.asyncio
    async def test_execute_connected(self, stripe_adapter: DirectRESTAdapter) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        await stripe_adapter.connect("pocket-1", {"STRIPE_API_KEY": "sk_test_123"})

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = [{"id": "inv_1"}]
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await stripe_adapter.execute("list_invoices", {"limit": 5})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_unknown_action(self, stripe_adapter: DirectRESTAdapter) -> None:
        await stripe_adapter.connect("pocket-1", {"STRIPE_API_KEY": "sk_test_123"})
        result = await stripe_adapter.execute("nonexistent", {})
        assert result.success is False
        assert "Unknown action" in (result.error or "")

    @pytest.mark.asyncio
    async def test_disconnect(self, stripe_adapter: DirectRESTAdapter) -> None:
        await stripe_adapter.connect("pocket-1", {"STRIPE_API_KEY": "sk_test_123"})
        await stripe_adapter.disconnect("pocket-1")
        result = await stripe_adapter.execute("list_invoices", {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_schema(self, stripe_adapter: DirectRESTAdapter) -> None:
        schema = await stripe_adapter.schema()
        assert schema["table"] == "stripe_invoices"
        assert schema["schedule"] == "every_15m"


class TestConnectorRegistry:
    """Test connector discovery and management."""

    def test_scan_connectors(self) -> None:
        registry = ConnectorRegistry(CONNECTORS_DIR)
        available = registry.available
        names = [c["name"] for c in available]
        assert "stripe" in names
        assert "csv" in names
        assert "rest_generic" in names

    def test_get_definition(self) -> None:
        registry = ConnectorRegistry(CONNECTORS_DIR)
        defn = registry.get_definition("stripe")
        assert defn is not None
        assert defn.display_name == "Stripe"

    @pytest.mark.asyncio
    async def test_connect_and_status(self) -> None:
        registry = ConnectorRegistry(CONNECTORS_DIR)
        result = await registry.connect("pocket-1", "stripe", {"STRIPE_API_KEY": "sk_test_123"})
        assert result.success is True

        status = registry.status("pocket-1")
        stripe_status = next(s for s in status if s["name"] == "stripe")
        assert stripe_status["status"] == ConnectorStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        registry = ConnectorRegistry(CONNECTORS_DIR)
        await registry.connect("pocket-1", "stripe", {"STRIPE_API_KEY": "sk_test_123"})
        success = await registry.disconnect("pocket-1", "stripe")
        assert success is True

        adapter = registry.get_adapter("pocket-1", "stripe")
        assert adapter is None

    def test_nonexistent_connector(self) -> None:
        registry = ConnectorRegistry(CONNECTORS_DIR)
        defn = registry.get_definition("nonexistent")
        assert defn is None
