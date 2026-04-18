# Connectors router — list, connect, disconnect, execute connector actions.
# Created: 2026-03-29 — REST API for the ConnectorRegistry.

from __future__ import annotations

import logging
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


def _get_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry(Path("connectors"))
    return _registry


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


# ── Routes ───────────────────────────────────────────────────────────────────


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
    reg = _get_registry()
    result = await reg.connect(req.pocket_id, req.connector_name, req.config)
    if result is None:
        return ConnectResponse(success=False, message=f"Unknown connector: {req.connector_name}")
    return ConnectResponse(
        success=result.success,
        message=result.message or "",
        tables_created=result.tables_created or [],
    )


@router.post("/connectors/disconnect", response_model=ConnectResponse)
async def disconnect_connector(req: DisconnectRequest):
    """Disconnect a data source."""
    reg = _get_registry()
    ok = await reg.disconnect(req.pocket_id, req.connector_name)
    return ConnectResponse(
        success=ok,
        message="Disconnected" if ok else "Not connected",
    )


@router.post("/connectors/execute", response_model=ExecuteResponse)
async def execute_connector_action(req: ExecuteRequest):
    """Execute an action on a connected data source."""
    reg = _get_registry()
    adapter = reg.get_adapter(req.pocket_id, req.connector_name)
    if not adapter:
        return ExecuteResponse(
            success=False,
            error=f"Connector '{req.connector_name}' is not connected",
        )

    result = await adapter.execute(req.action, req.params)
    return ExecuteResponse(
        success=result.success,
        data=result.data,
        error=result.error,
        records_affected=result.records_affected,
    )
