# Connector tools — let the agent list, connect, and execute connector actions.
# Created: 2026-03-29 — Wires ConnectorRegistry into agent tool interface.

import json
import logging
from pathlib import Path
from typing import Any

from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# Singleton registry — shared across all tool instances.
_registry: ConnectorRegistry | None = None


def _get_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry(Path("connectors"))
    return _registry


class ConnectorListTool(BaseTool):
    """List available connectors and their connection status."""

    @property
    def name(self) -> str:
        return "connector_list"

    @property
    def description(self) -> str:
        return (
            "List all available data connectors (Stripe, REST APIs, CSV, etc.) and show "
            "which ones are currently connected. Use this to discover what external sources "
            "can be queried."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "Pocket ID to check status for (default: 'default')",
                },
            },
        }

    async def execute(self, pocket_id: str = "default") -> str:
        reg = _get_registry()
        available = reg.available
        if not available:
            return "No connectors found. Add YAML definitions to the connectors/ directory."

        status = reg.status(pocket_id)
        status_map = {s["name"]: s["status"].value for s in status}

        lines = [f"Available connectors ({len(available)}):"]
        for c in available:
            st = status_map.get(c["name"], "disconnected")
            marker = "[connected]" if st == "connected" else "[disconnected]"
            lines.append(f"  {c['display_name']} ({c['name']}) — {c['type']} {marker}")

        return "\n".join(lines)


class ConnectorConnectTool(BaseTool):
    """Connect to an external data source."""

    @property
    def name(self) -> str:
        return "connector_connect"

    @property
    def description(self) -> str:
        return (
            "Connect to an external data source by name. Provide the connector name "
            "(e.g., 'stripe', 'rest_generic') and any required credentials. "
            "Once connected, use connector_execute to fetch data from it."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "connector_name": {
                    "type": "string",
                    "description": "Name of the connector (e.g., 'stripe', 'rest_generic', 'csv')",
                },
                "config": {
                    "type": "object",
                    "description": (
                        "Credentials and config"
                        " (e.g., {'STRIPE_API_KEY': 'sk_...'}"
                        " or {'BASE_URL': 'https://api.example.com'})"
                    ),
                },
                "pocket_id": {
                    "type": "string",
                    "description": "Pocket ID (default: 'default')",
                },
            },
            "required": ["connector_name", "config"],
        }

    async def execute(
        self, connector_name: str, config: dict[str, Any], pocket_id: str = "default"
    ) -> str:
        reg = _get_registry()

        defn = reg.get_definition(connector_name)
        if not defn:
            available = [c["name"] for c in reg.available]
            return f"Unknown connector: '{connector_name}'. Available: {', '.join(available)}"

        result = await reg.connect(pocket_id, connector_name, config)
        if result is None:
            return f"Failed to create adapter for '{connector_name}'."

        if result.success:
            tables = f" Tables: {', '.join(result.tables_created)}" if result.tables_created else ""
            return f"Connected to {defn.display_name}.{tables}"
        else:
            return f"Connection failed: {result.message}"


class ConnectorExecuteTool(BaseTool):
    """Execute an action on a connected data source."""

    @property
    def name(self) -> str:
        return "connector_execute"

    @property
    def description(self) -> str:
        return (
            "Execute an action on a connected data source. For example, fetch invoices from "
            "Stripe, query a REST API endpoint, or import a CSV file. Use connector_list to "
            "see available connectors and connector_actions to see what actions are available."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "connector_name": {
                    "type": "string",
                    "description": "Name of the connected connector",
                },
                "action": {
                    "type": "string",
                    "description": "Action to execute (e.g., 'list_invoices', 'get_endpoint')",
                },
                "params": {
                    "type": "object",
                    "description": "Action parameters (e.g., {'limit': 5} or {'path': '/users'})",
                },
                "pocket_id": {
                    "type": "string",
                    "description": "Pocket ID (default: 'default')",
                },
            },
            "required": ["connector_name", "action"],
        }

    async def execute(
        self,
        connector_name: str,
        action: str,
        params: dict[str, Any] | None = None,
        pocket_id: str = "default",
    ) -> str:
        reg = _get_registry()
        adapter = reg.get_adapter(pocket_id, connector_name)
        if not adapter:
            return f"Connector '{connector_name}' is not connected. Use connector_connect first."

        result = await adapter.execute(action, params or {})
        if not result.success:
            return f"Action failed: {result.error}"

        # Format data for the agent
        data = result.data
        if isinstance(data, list):
            summary = f"Returned {len(data)} record(s).\n"
            # Truncate large lists for readability
            items = data[:20]
            summary += json.dumps(items, indent=2, default=str)
            if len(data) > 20:
                summary += f"\n... and {len(data) - 20} more."
            return summary
        elif isinstance(data, dict):
            return json.dumps(data, indent=2, default=str)
        else:
            return str(data)


class ConnectorActionsTool(BaseTool):
    """List available actions for a connector."""

    @property
    def name(self) -> str:
        return "connector_actions"

    @property
    def description(self) -> str:
        return (
            "Show what actions are available for a connector (e.g., list_invoices, "
            "list_customers for Stripe). Shows method, parameters, and trust level."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "connector_name": {
                    "type": "string",
                    "description": "Name of the connector to inspect",
                },
            },
            "required": ["connector_name"],
        }

    async def execute(self, connector_name: str) -> str:
        reg = _get_registry()
        defn = reg.get_definition(connector_name)
        if not defn:
            available = [c["name"] for c in reg.available]
            return f"Unknown connector: '{connector_name}'. Available: {', '.join(available)}"

        lines = [f"Actions for {defn.display_name}:"]
        for act in defn.actions:
            method = act.get("method", "GET")
            trust = act.get("trust_level", "confirm")
            desc = act.get("description", "")
            params = list(act.get("params", {}).keys()) + list(act.get("body", {}).keys())
            param_str = f" — params: {', '.join(params)}" if params else ""
            lines.append(f"  {act['name']} [{method}] (trust: {trust}) {desc}{param_str}")

        # Show required credentials
        creds = defn.auth.get("credentials", [])
        if creds:
            lines.append("\nRequired credentials:")
            for c in creds:
                req = " (required)" if c.get("required") else " (optional)"
                lines.append(f"  {c['name']}{req} — {c.get('description', '')}")

        return "\n".join(lines)
