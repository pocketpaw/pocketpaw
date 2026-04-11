# ConnectorProtocol — interface for all data source adapters.
# Created: 2026-03-27 — Protocol-based, async, adapter-agnostic.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class ConnectorStatus(StrEnum):
    """Connection status."""

    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    SYNCING = "syncing"
    ERROR = "error"


class TrustLevel(StrEnum):
    """How much human oversight this action needs."""

    AUTO = "auto"  # Agent can execute without asking
    CONFIRM = "confirm"  # Agent must ask user first
    RESTRICTED = "restricted"  # Requires admin approval


@dataclass
class ConnectionResult:
    """Result of a connect() call."""

    success: bool
    connector_name: str
    status: ConnectorStatus = ConnectorStatus.DISCONNECTED
    message: str = ""
    tables_created: list[str] = field(default_factory=list)


@dataclass
class ActionSchema:
    """Schema for a single connector action."""

    name: str
    description: str = ""
    method: str = "GET"
    parameters: dict[str, Any] = field(default_factory=dict)
    trust_level: TrustLevel = TrustLevel.CONFIRM


@dataclass
class ActionResult:
    """Result of executing a connector action."""

    success: bool
    data: Any = None
    error: str | None = None
    records_affected: int = 0


@dataclass
class SyncResult:
    """Result of syncing data from a connector."""

    success: bool
    connector_name: str
    records_synced: int = 0
    records_updated: int = 0
    records_deleted: int = 0
    error: str | None = None
    duration_ms: float = 0


class ConnectorProtocol(Protocol):
    """Interface for all connector adapters.

    Implementations:
    - DirectRESTAdapter: YAML-defined REST API connectors
    - ComposioAdapter: 250+ apps with managed OAuth (future)
    - CuratedMCPAdapter: Whitelisted MCP servers (future)
    """

    @property
    def name(self) -> str:
        """Connector name (e.g. 'stripe', 'csv')."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable name (e.g. 'Stripe', 'CSV Import')."""
        ...

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        """Authenticate and establish connection to this data source."""
        ...

    async def disconnect(self, pocket_id: str) -> bool:
        """Disconnect from this data source."""
        ...

    async def actions(self) -> list[ActionSchema]:
        """Return available actions for this connector."""
        ...

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        """Execute a specific action (e.g. list_invoices, create_invoice)."""
        ...

    async def sync(self, pocket_id: str) -> SyncResult:
        """Pull latest data into pocket.db."""
        ...

    async def schema(self) -> dict[str, Any]:
        """Return data schema for pocket.db table mapping."""
        ...
