# Connectors — data integration layer for Paw OS.
# Created: 2026-03-27 — Multi-adapter facade for external data sources.
# DirectREST (YAML-defined) is the primary adapter. Composio/MCP are fallbacks.

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorProtocol,
    IngestACL,
    IngestAdapter,
    SyncResult,
)
from pocketpaw.connectors.registry import ConnectorRegistry

__all__ = [
    "ConnectorProtocol",
    "ConnectionResult",
    "ActionSchema",
    "ActionResult",
    "IngestACL",
    "IngestAdapter",
    "SyncResult",
    "ConnectorRegistry",
]
