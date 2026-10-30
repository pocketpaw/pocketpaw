# Connectors — data integration layer for Paw OS.
# Created: 2026-03-27 — Multi-adapter facade for external data sources.
# DirectREST (YAML-defined) is the primary adapter. Composio/MCP are fallbacks.
# Updated: 2026-06-11 (gap1-connfabric slice) — re-export the connector->Fabric
#   ingestion primitives (FabricMapping / IngestResult / ingest_records) so the
#   reusable "connector record -> typed Fabric object with provenance" pattern is
#   discoverable from the package root, not buried in the submodule.

from pocketpaw.connectors.fabric_ingest import (
    FabricMapping,
    IngestResult,
    ingest_records,
)
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
    "FabricMapping",
    "IngestResult",
    "ingest_records",
]
