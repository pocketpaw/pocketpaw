# Connector registry — discovers and manages available connectors.
# Created: 2026-03-27 — Scans connectors/ dir for YAML definitions.
# Updated: 2026-03-30 — Native adapter support for database connectors.
# Updated: 2026-04-01 — Added Firebase CLI adapter registration.
# Updated: 2026-04-01 — Added GCP adapter for gcloud CLI integration.

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorStatus,
)
from pocketpaw.connectors.yaml_engine import ConnectorDef, DirectRESTAdapter, parse_connector_yaml


@runtime_checkable
class AnyAdapter(Protocol):
    """Union type for all adapter kinds."""

    @property
    def name(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult: ...
    async def disconnect(self, pocket_id: str) -> bool: ...
    async def actions(self) -> list[ActionSchema]: ...
    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult: ...


# Connectors handled by native Python adapters instead of YAML/REST.
# SQL databases use DatabaseAdapter, MongoDB uses MongoDBAdapter.
_SQL_CONNECTORS: set[str] = {"postgresql", "mysql", "mssql", "sqlite"}
_NOSQL_CONNECTORS: set[str] = {"mongodb"}
_CLI_CONNECTORS: set[str] = {"firebase", "gcp"}


def _create_native_adapter(connector_name: str) -> AnyAdapter | None:
    """Create a native adapter for database connectors."""
    if connector_name in _SQL_CONNECTORS:
        try:
            from pocketpaw.connectors.db_adapter import DatabaseAdapter

            return DatabaseAdapter(connector_name)
        except Exception:
            return None
    if connector_name in _NOSQL_CONNECTORS:
        try:
            from pocketpaw.connectors.mongo_adapter import MongoDBAdapter

            return MongoDBAdapter()
        except Exception:
            return None
    if connector_name in _CLI_CONNECTORS:
        try:
            if connector_name == "gcp":
                from pocketpaw.connectors.gcp_adapter import GCPAdapter

                return GCPAdapter()
            from pocketpaw.connectors.firebase_adapter import FirebaseAdapter

            return FirebaseAdapter()
        except Exception:
            return None
    return None


class ConnectorRegistry:
    """Discovers available connectors and manages instances per pocket."""

    def __init__(self, connectors_dir: Path | None = None) -> None:
        self._connectors_dir = connectors_dir or Path("connectors")
        self._definitions: dict[str, ConnectorDef] = {}
        self._instances: dict[str, AnyAdapter] = {}  # key = "{pocket_id}:{connector_name}"
        self._scan()

    def _scan(self) -> None:
        """Scan connectors directory for YAML definitions."""
        if not self._connectors_dir.exists():
            return
        for path in sorted(self._connectors_dir.glob("*.yaml")):
            try:
                defn = parse_connector_yaml(path)
                self._definitions[defn.name] = defn
            except Exception:
                pass  # Skip malformed YAMLs

    @property
    def available(self) -> list[dict[str, str]]:
        """List all available connector definitions."""
        return [
            {
                "name": d.name,
                "display_name": d.display_name,
                "type": d.type,
                "icon": d.icon,
            }
            for d in self._definitions.values()
        ]

    def get_definition(self, name: str) -> ConnectorDef | None:
        """Get a connector definition by name."""
        return self._definitions.get(name)

    def get_adapter(self, pocket_id: str, connector_name: str) -> AnyAdapter | None:
        """Get an active adapter instance for a pocket+connector."""
        key = f"{pocket_id}:{connector_name}"
        return self._instances.get(key)

    async def connect(self, pocket_id: str, connector_name: str, config: dict[str, Any]) -> Any:
        """Create and connect a connector adapter for a pocket."""
        defn = self._definitions.get(connector_name)
        if not defn:
            return None

        # Use native adapter if available, otherwise fall back to YAML/REST.
        adapter: AnyAdapter
        native = _create_native_adapter(connector_name)
        if native is not None:
            adapter = native
        else:
            adapter = DirectRESTAdapter(defn)

        result = await adapter.connect(pocket_id, config)

        if result.success:
            key = f"{pocket_id}:{connector_name}"
            self._instances[key] = adapter

        return result

    async def disconnect(self, pocket_id: str, connector_name: str) -> bool:
        """Disconnect a connector from a pocket."""
        key = f"{pocket_id}:{connector_name}"
        adapter = self._instances.get(key)
        if not adapter:
            return False
        await adapter.disconnect(pocket_id)
        del self._instances[key]
        return True

    def status(self, pocket_id: str) -> list[dict[str, Any]]:
        """Get connection status for all connectors in a pocket."""
        results = []
        for name, defn in self._definitions.items():
            key = f"{pocket_id}:{name}"
            adapter = self._instances.get(key)
            results.append(
                {
                    "name": name,
                    "display_name": defn.display_name,
                    "icon": defn.icon,
                    "status": ConnectorStatus.CONNECTED
                    if adapter
                    else ConnectorStatus.DISCONNECTED,
                }
            )
        return results

    def reload(self) -> None:
        """Re-scan the connectors directory."""
        self._definitions.clear()
        self._scan()
