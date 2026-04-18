# ConnectorProtocol — interface for all data source adapters.
# Created: 2026-03-27 — Protocol-based, async, adapter-agnostic.
# Updated: 2026-04-13 (Move 7 PR-A) — Added IngestACL + IngestAdapter
#   alias so the ingest side of the protocol carries source-side ACLs
#   into Fabric. Adapters that emit documents tagged with inherited
#   scope keep org permissions intact end-to-end (a private Slack
#   channel's messages stay private inside Single Brain).

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


@dataclass
class IngestACL:
    """Source-side access control list inherited into Single Brain.

    When an adapter pulls a document from a tenant system that already has
    ACLs (a private Slack channel, a HubSpot deal restricted to one team,
    a Notion page shared with a sub-list), the adapter reports those ACLs
    here so paw-runtime tags the resulting Fabric object with the matching
    scope tags before it lands. The brain inherits the source's permissions
    instead of flattening them to "everyone with access to the connector."

    Empty fields are intentional defaults — most ingests are global to the
    pocket and need no per-record scoping. When ``scope`` is non-empty,
    the runtime applies it as the Fabric object's ``scope`` list verbatim;
    when ``visibility`` is set, callers can also surface a UI label.
    """

    scope: list[str] = field(default_factory=list)
    visibility: str = ""  # "public" | "private" | "members" | "owner"
    source_principal: str = ""  # The original ACL holder, e.g. "channel:#founders"
    metadata: dict[str, Any] = field(default_factory=dict)


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


class IngestAdapter(ConnectorProtocol, Protocol):
    """A connector that pulls data INTO Paw OS (vs sending it out).

    Same surface as :class:`ConnectorProtocol` plus :meth:`permissions`,
    which reports the ACLs of records the adapter can ingest. Existing
    REST + DB + file connectors satisfy this protocol once they implement
    permissions(); the alias makes the intent explicit at type-check time
    and lets the fleet template runtime (PR-B) discover ACL-aware
    connectors without sniffing for the method.
    """

    async def permissions(self, pocket_id: str, record_id: str | None = None) -> IngestACL:
        """Report source-side ACLs for the next ingest.

        ``record_id`` may be ``None`` for connector-wide defaults (e.g. a
        Stripe-account-wide read scope). When present, returns the ACLs
        for one specific record so per-document scoping works for systems
        like Slack channels or HubSpot deal-team membership.
        """
        ...
