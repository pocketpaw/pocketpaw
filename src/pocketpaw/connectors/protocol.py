# ConnectorProtocol — interface for all data source adapters.
# Created: 2026-03-27 — Protocol-based, async, adapter-agnostic.
# Updated: 2026-04-13 (Move 7 PR-A) — Added IngestACL + IngestAdapter
#   alias so the ingest side of the protocol carries source-side ACLs
#   into Fabric.
# Updated: 2026-05-03 (Phase 1 PR-2) — Added ExecutionMode +
#   requires_binary on ActionSchema, ConnectorScope tagged union,
#   WidgetRecipe + ConnectorHealth dataclasses, and widgets() / health()
#   methods on ConnectorProtocol. See ee/cloud/connectors/CHARTER.md
#   §4 + §6.2 for the rationale (CLI connectors can't multi-tenant in
#   cloud, so the runtime needs to know where each action runs).

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


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


class ExecutionMode(StrEnum):
    """Where a connector action is allowed to execute.

    The cloud router (``ee/cloud/connectors/router.py``) inspects this
    on each request and dispatches accordingly:

    - ``CLOUD`` — runs in the FastAPI process (default for YAML / REST
      connectors, in-process logic).
    - ``LOCAL`` — runs on the user's pocketpaw runtime via the
      local-agent bus (CLI tools that depend on the user's machine
      state — gcloud, firebase, gh, kubectl, …). The cloud router
      forwards the call through ``connector.exec.requested`` on the
      shared chat WebSocket and awaits ``connector.exec.completed``.
    - ``SANDBOX`` — reserved. Ephemeral container per invocation with
      workspace-scoped service-account creds. Implementation deferred
      until a real client need surfaces (see CHARTER.md §3 out of scope).
    """

    CLOUD = "cloud"
    LOCAL = "local"
    SANDBOX = "sandbox"


# ---------------------------------------------------------------------------
# Connector scope — tagged union resolved at the runtime boundary.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PocketScope:
    """Scope bound to one pocket. Used by KB ingestion + per-pocket data sources."""

    pocket_id: str
    workspace_id: str = ""
    kind: Literal["pocket"] = "pocket"


@dataclass(frozen=True)
class WorkspaceScope:
    """Scope bound to one workspace. Default for home widgets and automations."""

    workspace_id: str
    kind: Literal["workspace"] = "workspace"


@dataclass(frozen=True)
class UserScope:
    """Scope bound to one user. Used for personal email / calendar feeds."""

    user_id: str
    workspace_id: str = ""  # always present so tenancy works regardless
    kind: Literal["user"] = "user"


ConnectorScope = PocketScope | WorkspaceScope | UserScope


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
    """Schema for a single connector action.

    ``execution_mode`` (added Phase 1 PR-2) tells the cloud router where
    this action is allowed to run. YAML connectors default to ``CLOUD``;
    CLI adapters override per action.

    ``requires_binary`` names the executable the action shells out to
    (``"gcloud"``, ``"firebase"``, ``"gh"``, …) so the local agent can
    fail fast with a useful error when the binary is missing.
    """

    name: str
    description: str = ""
    method: str = "GET"
    parameters: dict[str, Any] = field(default_factory=dict)
    trust_level: TrustLevel = TrustLevel.CONFIRM
    execution_mode: ExecutionMode = ExecutionMode.CLOUD
    requires_binary: str | None = None


# ---------------------------------------------------------------------------
# Health + widget recipes — added Phase 1 PR-2
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorHealth:
    """Live status snapshot. Returned by ``Connector.health(scope)``.

    The cloud's ConnectorPanel frontend uses this to show a real status
    badge instead of inferring from the last execute() (which was
    fragile — see CHARTER.md §4 note 3).
    """

    ok: bool
    status: ConnectorStatus
    message: str = ""
    checked_at_ms: int = 0  # ms-since-epoch — frontend formats relative


@dataclass(frozen=True)
class WidgetRecipe:
    """Pre-baked default widget the connector contributes to home dashboards.

    AddWidgetPicker reads these via ``GET /api/v1/cloud/connectors/widget-recipes``
    to populate the "From connectors" rail. The recipe is a thin spec —
    title + display type + the action call to make — that compiles to a
    Ripple UISpec at render time. Survives Ripple version bumps because
    the recipe shape doesn't pin a UISpec version.
    """

    title: str
    display_type: str  # "metric" | "chart" | "table" | "feed" | "stats"
    action: str  # action name on the connector (e.g. "search")
    params: dict[str, Any] = field(default_factory=dict)
    default_size: str = "col-1 row-1"  # grid-span hint
    description: str = ""


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

    # --- Phase 1 PR-2 additions ----------------------------------------------

    async def widgets(self) -> list[WidgetRecipe]:
        """Return default home widgets the connector contributes.

        Default implementation in ``DirectRESTAdapter`` returns ``[]``;
        native connectors (Gmail, Calendar, …) override to expose recipes
        like Inbox, Today's Calendar, Recent Drive activity.
        """
        ...

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        """Live health snapshot for the ConnectorPanel status badge.

        Implementations should be cheap (single auth-check or HEAD/OPTIONS
        request). Heavy probes belong in a dedicated diagnostics tool,
        not the per-request status endpoint.
        """
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
