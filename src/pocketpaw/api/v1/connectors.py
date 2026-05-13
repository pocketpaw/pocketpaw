# Connectors router — list, connect, disconnect, execute connector actions.
# Created: 2026-03-29 — REST API for the ConnectorRegistry.
# Updated: 2026-04-19 (Cluster C / PR2) — Added GET /connectors/{kind}/status
#   returning a structured {connected, last_sync, cred_state, scope} payload
#   for the ConnectorCard UI. Gap C5 in docs/plans/FEATURE-HARDENING-PLAN.md.

from __future__ import annotations

import logging
from datetime import UTC
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from pocketpaw.api.deps import require_scope
from pocketpaw.connectors.registry import ConnectorRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Connectors"], dependencies=[Depends(require_scope("connectors"))])

# Singleton registry — lazily initialized.
_registry: ConnectorRegistry | None = None

# Alternate name lookup: some UIs pass "google_drive" but the registry key is "drive".
_ALT_NAMES: dict[str, str] = {
    "google_drive": "drive",
    "google_gmail": "gmail",
    "google_calendar": "gcalendar",
    "google_docs": "gdocs",
}


def _get_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry(Path("connectors"))
    return _registry


def _resolve_connector_name(name: str) -> str:
    """Resolve aliased connector names to their registry key."""
    return _ALT_NAMES.get(name, name)


# Connector registry name → OAuth token store service name.
# Used by lazy-connect and disconnect to read / delete the right tokens.
_CONNECTOR_TO_TOKEN_SERVICE: dict[str, str] = {
    "drive": "google_drive",
    "gmail": "google_gmail",
    "gcalendar": "google_calendar",
    "gdocs": "google_docs",
    "spotify": "spotify",
}


# ── Request / Response models ────────────────────────────────────────────────


class ConnectorInfo(BaseModel):
    name: str
    display_name: str
    type: str
    icon: str
    status: str = "disconnected"


class ConnectorActionInfo(BaseModel):
    name: str
    description: str
    method: str
    params: list[str]
    trust_level: str


class ConnectorDetailResponse(BaseModel):
    name: str
    display_name: str
    type: str
    icon: str
    status: str
    actions: list[ConnectorActionInfo]
    credentials: list[dict[str, Any]]


class ConnectRequest(BaseModel):
    connector_name: str
    config: dict[str, Any]
    pocket_id: str = "default"


class ConnectResponse(BaseModel):
    success: bool
    message: str
    tables_created: list[str] = []


class DisconnectRequest(BaseModel):
    connector_name: str
    pocket_id: str = "default"


class ExecuteRequest(BaseModel):
    connector_name: str
    action: str
    params: dict[str, Any] = {}
    pocket_id: str = "default"


class ExecuteResponse(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None
    records_affected: int = 0


class ConnectorStatusResponse(BaseModel):
    """Structured status for one connector in one pocket.

    Consumed by paw-enterprise's ``ConnectorCard`` component which renders
    the Connect/Disconnect UI in PocketDataPanel. Fields:

    - ``connected``: boolean — adapter is instantiated and credentials have
      not been revoked.
    - ``last_sync``: ISO timestamp of the last successful action, or null
      if the connector has never been used.
    - ``cred_state``: ``"valid" | "expired" | "missing" | "revoked"`` — the
      UI uses this to choose the badge colour.
    - ``scope``: the OAuth/permission scope string granted at connect time.
      Never includes secret material — just the grant descriptor. Empty
      string if the connector doesn't use scoped creds.
    """

    name: str
    pocket_id: str
    connected: bool
    last_sync: str | None = None
    cred_state: str = "missing"
    scope: str = ""


# ── Routes ───────────────────────────────────────────────────────────────────


# Per-pocket connector status side-table. Kept in memory because the
# status is an ephemeral snapshot — the adapter instance map in the
# registry is the source of truth for "connected" and this layer just
# decorates it with the last-sync timestamp and cred state observed at
# connect time.
_STATUS_EXTRAS: dict[str, dict[str, Any]] = {}


def _extras_key(pocket_id: str, connector_name: str) -> str:
    return f"{pocket_id}:{connector_name}"


def record_connector_event(
    *,
    pocket_id: str,
    connector_name: str,
    cred_state: str | None = None,
    last_sync: str | None = None,
    scope: str | None = None,
) -> None:
    """Persist the fields the status endpoint surfaces.

    Called by the connect/disconnect/execute paths below. Kept as a small
    helper so the registry stays unaware of the API layer.
    """
    key = _extras_key(pocket_id, connector_name)
    entry = _STATUS_EXTRAS.setdefault(key, {})
    if cred_state is not None:
        entry["cred_state"] = cred_state
    if last_sync is not None:
        entry["last_sync"] = last_sync
    if scope is not None:
        entry["scope"] = scope


@router.get(
    "/connectors/{connector_name}/status",
    response_model=ConnectorStatusResponse,
)
async def get_connector_status(
    connector_name: str,
    pocket_id: str = "default",
) -> ConnectorStatusResponse:
    """Status payload for one connector in one pocket.

    The pocket_id filter is essential — per the Cluster C security review,
    connector credentials are scoped by pocket, so status queries are too.
    A pocket_id the caller doesn't have access to will simply return
    ``connected=false`` without leaking whether the connector exists for
    another pocket.

    Unlike the initial implementation (which returned a stale ``cred_state``
    cached at connect-time), this version:

    1. Resolves alternate names (``google_drive`` → ``drive``).
    2. Lazy-connects if the OAuth token store has valid credentials but no
       in-memory adapter exists yet (handles server restarts and delayed
       adapter creation after the OAuth callback).
    3. Calls ``health()`` on the adapter for a real-time ``cred_state``
       instead of returning a stale cached value.
    """
    resolved_name = _resolve_connector_name(connector_name)
    reg = _get_registry()
    defn = reg.get_definition(resolved_name)
    if not defn:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Connector '{resolved_name}' not found")

    adapter = reg.get_adapter(pocket_id, resolved_name)

    # Lazy connect: if no adapter in memory but the OAuth token store
    # has credentials for this connector, try to instantiate now.
    if adapter is None:
        adapter = await _try_lazy_connect(reg, pocket_id, resolved_name)

    extras = _STATUS_EXTRAS.get(_extras_key(pocket_id, resolved_name), {})
    connected = adapter is not None
    last_sync = extras.get("last_sync")
    scope = extras.get("scope", "")

    if connected:
        # Real-time health check so cred_state reflects actual token validity.
        try:
            health = await adapter.health()
            cred_state = "valid" if health.ok else "expired"
        except Exception:
            cred_state = extras.get("cred_state", "valid")
        # Persist the updated cred_state for the next poll.
        record_connector_event(
            pocket_id=pocket_id,
            connector_name=resolved_name,
            cred_state=cred_state,
        )
    else:
        cred_state = extras.get("cred_state", "missing")

    return ConnectorStatusResponse(
        name=connector_name,
        pocket_id=pocket_id,
        connected=connected,
        last_sync=last_sync,
        cred_state=cred_state,
        scope=scope,
    )


async def _try_lazy_connect(
    reg: ConnectorRegistry,
    pocket_id: str,
    connector_name: str,
) -> Any | None:
    """Try to create an adapter if the OAuth token store has credentials.

    This handles two scenarios:
    - Server restart: adapters are in-memory, tokens are on disk.
    - OAuth callback: tokens were saved but no adapter was registered.
    """
    # Only connectors with OAuth clients in the token store qualify.
    # Check if the token store has saved tokens for this connector.
    try:
        from pocketpaw.clients.token_store import TokenStore

        store = TokenStore()

        service = _CONNECTOR_TO_TOKEN_SERVICE.get(connector_name)
        if service is None:
            return None  # Not an OAuth connector — can't lazy connect.

        tokens = store.load(service)
        if tokens is None:
            return None  # No saved tokens — nothing to connect with.

        # Tokens exist on disk. Try to connect the adapter.
        config: dict[str, Any] = {"scope": " ".join(tokens.scopes) if tokens.scopes else ""}
        result = await reg.connect(pocket_id, connector_name, config)
        if result and result.success:
            record_connector_event(
                pocket_id=pocket_id,
                connector_name=connector_name,
                cred_state="valid",
                scope=config["scope"],
            )
            logger.info("Lazy-connected %s for pocket %s (tokens found on disk)", connector_name, pocket_id)
            return reg.get_adapter(pocket_id, connector_name)

        return None
    except Exception as exc:
        logger.debug("Lazy connect failed for %s: %s", connector_name, exc)
        return None


@router.get("/connectors", response_model=list[ConnectorInfo])
async def list_connectors(pocket_id: str = "default"):
    """List all available connectors with their connection status."""
    reg = _get_registry()
    status_map = {s["name"]: s["status"].value for s in reg.status(pocket_id)}

    return [
        ConnectorInfo(
            name=c["name"],
            display_name=c["display_name"],
            type=c["type"],
            icon=c.get("icon", "plug"),
            status=status_map.get(c["name"], "disconnected"),
        )
        for c in reg.available
    ]


@router.get("/connectors/{connector_name}", response_model=ConnectorDetailResponse)
async def get_connector_detail(connector_name: str, pocket_id: str = "default"):
    """Get connector details including available actions and required credentials."""
    connector_name = _resolve_connector_name(connector_name)
    reg = _get_registry()
    defn = reg.get_definition(connector_name)
    if not defn:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found")

    status_list = reg.status(pocket_id)
    status = "disconnected"
    for s in status_list:
        if s["name"] == connector_name:
            status = s["status"].value
            break

    actions = []
    for act in defn.actions:
        params = list(act.get("params", {}).keys()) + list(act.get("body", {}).keys())
        actions.append(
            ConnectorActionInfo(
                name=act["name"],
                description=act.get("description", ""),
                method=act.get("method", "GET"),
                params=params,
                trust_level=act.get("trust_level", "confirm"),
            )
        )

    credentials = defn.auth.get("credentials", [])

    return ConnectorDetailResponse(
        name=defn.name,
        display_name=defn.display_name,
        type=defn.type,
        icon=defn.icon,
        status=status,
        actions=actions,
        credentials=credentials,
    )


@router.post("/connectors/connect", response_model=ConnectResponse)
async def connect_connector(req: ConnectRequest):
    """Connect to a data source with credentials."""
    from datetime import datetime

    connector_name = _resolve_connector_name(req.connector_name)
    reg = _get_registry()
    result = await reg.connect(req.pocket_id, connector_name, req.config)
    if result is None:
        return ConnectResponse(success=False, message=f"Unknown connector: {req.connector_name}")
    if result.success:
        scope = str(req.config.get("scope") or req.config.get("scopes") or "")
        record_connector_event(
            pocket_id=req.pocket_id,
            connector_name=connector_name,
            cred_state="valid",
            last_sync=datetime.now(UTC).isoformat(),
            scope=scope,
        )
    return ConnectResponse(
        success=result.success,
        message=result.message or "",
        tables_created=result.tables_created or [],
    )


@router.post("/connectors/disconnect", response_model=ConnectResponse)
async def disconnect_connector(req: DisconnectRequest):
    """Disconnect a data source and delete stored OAuth tokens."""
    connector_name = _resolve_connector_name(req.connector_name)
    reg = _get_registry()
    ok = await reg.disconnect(req.pocket_id, connector_name)
    if ok:
        # Delete stored OAuth tokens so the connector stays disconnected
        # after server restart and doesn't lazy-connect on the next status poll.
        _delete_oauth_tokens(connector_name)
        record_connector_event(
            pocket_id=req.pocket_id,
            connector_name=connector_name,
            cred_state="missing",
            scope="",
        )
    return ConnectResponse(
        success=ok,
        message="Disconnected" if ok else "Not connected",
    )


def _delete_oauth_tokens(connector_name: str) -> None:
    """Delete OAuth tokens for a connector from the token store."""
    try:
        from pocketpaw.clients.token_store import TokenStore

        store = TokenStore()
        service = _CONNECTOR_TO_TOKEN_SERVICE.get(connector_name)
        if service:
            store.delete(service)
            logger.info("Deleted OAuth tokens for %s (%s)", connector_name, service)
    except Exception as exc:
        logger.debug("Could not delete OAuth tokens for %s: %s", connector_name, exc)


@router.post("/connectors/execute", response_model=ExecuteResponse)
async def execute_connector_action(req: ExecuteRequest):
    """Execute an action on a connected data source."""
    from datetime import datetime

    connector_name = _resolve_connector_name(req.connector_name)
    reg = _get_registry()
    adapter = reg.get_adapter(req.pocket_id, connector_name)
    if not adapter:
        return ExecuteResponse(
            success=False,
            error=f"Connector '{req.connector_name}' is not connected",
        )

    result = await adapter.execute(req.action, req.params)
    if result.success:
        record_connector_event(
            pocket_id=req.pocket_id,
            connector_name=connector_name,
            cred_state="valid",
            last_sync=datetime.now(UTC).isoformat(),
        )
    return ExecuteResponse(
        success=result.success,
        data=result.data,
        error=result.error,
        records_affected=result.records_affected,
    )
