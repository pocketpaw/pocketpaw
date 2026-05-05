# Connectors — workspace-scoped business logic.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation.
# Module-level async API. Sole owner of writes to the
# ``WorkspaceConnector`` Beanie document. Reads merge the static
# registry catalog from src/pocketpaw/connectors/registry.py with the
# per-workspace state stored here.
#
# Cloud rules followed (per workspace CLAUDE.md):
# §2  Writes go through this service; routers never import models.
# §5  Module-level async functions, not a class.
# §6  Every request schema is re-validated at the service entry.
# §7  Every read filters by workspace_id.
# §9  Every write emits an event (or carries a ``# no-event`` justification).

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ee.cloud._core.errors import CloudError, NotFound, ValidationError
from ee.cloud.connectors.domain import AvailableConnector, WorkspaceConnector
from ee.cloud.connectors.dto import (
    ConnectorDetailResponse,
    ConnectorResponse,
    EnableConnectorRequest,
    ExecuteActionRequest,
    ExecuteActionResponse,
    UpdateConnectorConfigRequest,
    WidgetRecipeResponse,
)
from ee.cloud.models.connector import WorkspaceConnector as _WCDoc
from ee.cloud.shared.events import event_bus
from pocketpaw.connectors.protocol import ExecutionMode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry access — lazy singleton, mirrors src/pocketpaw/api/v1/connectors.py
# ---------------------------------------------------------------------------

_registry = None


def _get_registry():
    """Lazy-init the static registry. Reused across calls."""
    global _registry
    if _registry is None:
        from pocketpaw.connectors.registry import ConnectorRegistry

        _registry = ConnectorRegistry(Path("connectors"))
    return _registry


def _available_from_registry() -> list[AvailableConnector]:
    """Catalog of connectors the registry knows about.

    ``ConnectorDef.actions`` is ``list[dict[str, Any]]`` — raw YAML rows.
    Each row has a ``name`` key. ``ConnectorDef.auth`` is a dict shaped
    like ``{method: "bearer", credentials: [...]}``.
    """
    reg = _get_registry()
    out: list[AvailableConnector] = []
    for d in reg._definitions.values():  # noqa: SLF001 — registry exposes no public iter yet
        actions = tuple(
            a.get("name", "") for a in (d.actions or []) if isinstance(a, dict) and a.get("name")
        )
        auth_method = (d.auth or {}).get("method", "none") if isinstance(d.auth, dict) else "none"
        out.append(
            AvailableConnector(
                name=d.name,
                display_name=d.display_name,
                type=d.type,
                icon=d.icon,
                auth_method=auth_method,
                actions=actions,
            ),
        )
    return out


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _doc_to_domain(
    doc: _WCDoc,
    *,
    display_name: str,
    type_: str,
    icon: str,
) -> WorkspaceConnector:
    return WorkspaceConnector(
        name=doc.name,
        workspace_id=doc.workspace,
        display_name=display_name,
        type=type_,
        icon=icon,
        enabled=doc.enabled,
        scope=doc.scope,
        pocket_id=doc.pocket_id,
        user_id=doc.user_id,
        config=tuple(doc.config.items()),
        last_sync_at=doc.last_sync_at,
        last_sync_status=doc.last_sync_status,
        last_sync_error=doc.last_sync_error,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )


def _row_response(d: AvailableConnector, doc: _WCDoc | None) -> ConnectorResponse:
    """Build the wire row by merging registry + Mongo state."""
    if doc is None:
        return ConnectorResponse(
            name=d.name,
            display_name=d.display_name,
            type=d.type,
            icon=d.icon,
            status="disconnected",
            enabled=False,
        )
    return ConnectorResponse(
        name=d.name,
        display_name=d.display_name,
        type=d.type,
        icon=d.icon,
        status="connected" if doc.enabled else "disconnected",
        enabled=doc.enabled,
        scope=doc.scope,
        last_sync_at=doc.last_sync_at,
        last_sync_status=doc.last_sync_status,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_connectors(workspace_id: str) -> list[ConnectorResponse]:
    """List all available connectors with this workspace's enabled state.

    Read-only. Tenant filter on the Beanie query (cloud rule §7); the
    registry catalog is global by design.
    """
    available = _available_from_registry()
    docs = await _WCDoc.find(_WCDoc.workspace == workspace_id).to_list()
    by_name = {d.name: d for d in docs}
    return [_row_response(a, by_name.get(a.name)) for a in available]


async def get_connector(workspace_id: str, name: str) -> ConnectorDetailResponse:
    """One connector's detail row + actions + saved config.

    Raises ``NotFound`` if the registry doesn't know the name.
    """
    available = {a.name: a for a in _available_from_registry()}
    if name not in available:
        raise NotFound("connector", name)
    a = available[name]
    doc = await _WCDoc.find_one(_WCDoc.workspace == workspace_id, _WCDoc.name == name)
    base = _row_response(a, doc).model_dump()
    return ConnectorDetailResponse(
        **base,
        actions=list(a.actions),
        config=dict(doc.config) if doc else {},
    )


async def enable_connector(
    workspace_id: str,
    name: str,
    body: EnableConnectorRequest,
) -> ConnectorResponse:
    """Enable a connector for this workspace, creating the row if needed."""
    body = EnableConnectorRequest.model_validate(body)
    available = {a.name: a for a in _available_from_registry()}
    if name not in available:
        raise NotFound("connector", name)

    if body.scope == "pocket" and not body.pocket_id:
        raise ValidationError("connector.scope_missing_pocket", "scope=pocket requires pocket_id")
    if body.scope == "user" and not body.user_id:
        raise ValidationError("connector.scope_missing_user", "scope=user requires user_id")

    doc = await _WCDoc.find_one(_WCDoc.workspace == workspace_id, _WCDoc.name == name)
    if doc is None:
        doc = _WCDoc(
            workspace=workspace_id,
            name=name,
            enabled=True,
            scope=body.scope,
            pocket_id=body.pocket_id,
            user_id=body.user_id,
            config=body.config,
        )
        await doc.insert()
    else:
        doc.enabled = True
        doc.scope = body.scope
        doc.pocket_id = body.pocket_id
        doc.user_id = body.user_id
        if body.config:
            doc.config = body.config
        await doc.save()

    a = available[name]
    await event_bus.emit(
        "connector.enabled",
        {"workspace_id": workspace_id, "name": name, "scope": body.scope},
    )
    return _row_response(a, doc)


async def disable_connector(workspace_id: str, name: str) -> ConnectorResponse:
    """Disable (soft) a connector for this workspace.

    Keeps the row so config + history survive re-enable. The actual token
    revocation lives in the adapter's ``disconnect()`` method and is
    orchestrated separately — Phase 1 just flips the flag.
    """
    available = {a.name: a for a in _available_from_registry()}
    if name not in available:
        raise NotFound("connector", name)
    doc = await _WCDoc.find_one(_WCDoc.workspace == workspace_id, _WCDoc.name == name)
    if doc is None:
        # Already not enabled — return the disconnected row.
        return _row_response(available[name], None)
    doc.enabled = False
    await doc.save()
    await event_bus.emit(
        "connector.disabled",
        {"workspace_id": workspace_id, "name": name},
    )
    return _row_response(available[name], doc)


async def update_config(
    workspace_id: str,
    name: str,
    body: UpdateConnectorConfigRequest,
) -> ConnectorResponse:
    """Patch the saved config for one connector. Connector must be enabled first."""
    body = UpdateConnectorConfigRequest.model_validate(body)
    available = {a.name: a for a in _available_from_registry()}
    if name not in available:
        raise NotFound("connector", name)
    doc = await _WCDoc.find_one(_WCDoc.workspace == workspace_id, _WCDoc.name == name)
    if doc is None:
        raise NotFound("connector", name)
    doc.config = {**doc.config, **body.config}
    await doc.save()
    await event_bus.emit(
        "connector.config_updated",
        {"workspace_id": workspace_id, "name": name},
    )
    return _row_response(available[name], doc)


async def record_sync(
    workspace_id: str,
    name: str,
    *,
    status: str,
    error: str = "",
) -> WorkspaceConnector:
    """Update last_sync_at + last_sync_status from an adapter callback.

    No HTTP route in PR-1 — this is for adapters to call after a successful
    or failed sync. PR-3 (Gmail) is the first caller.
    """
    if status not in {"ok", "error"}:
        raise ValidationError("connector.invalid_sync_status", f"unknown status {status!r}")
    available = {a.name: a for a in _available_from_registry()}
    if name not in available:
        raise NotFound("connector", name)
    doc = await _WCDoc.find_one(_WCDoc.workspace == workspace_id, _WCDoc.name == name)
    if doc is None:
        raise NotFound("connector", name)
    doc.last_sync_at = datetime.utcnow()
    doc.last_sync_status = status
    doc.last_sync_error = error if status == "error" else ""
    await doc.save()
    a = available[name]
    await event_bus.emit(
        "connector.sync_recorded",
        {"workspace_id": workspace_id, "name": name, "status": status},
    )
    return _doc_to_domain(doc, display_name=a.display_name, type_=a.type, icon=a.icon)


# ---------------------------------------------------------------------------
# Phase 1 PR-2 — widget recipes + action execution
# ---------------------------------------------------------------------------


async def list_widget_recipes(workspace_id: str) -> list[WidgetRecipeResponse]:
    """Flatten widget recipes across every connector enabled for this workspace.

    Read-only, tenant-filtered. Frontend AddWidgetPicker calls this to
    populate the "From connectors" rail. Disabled connectors contribute
    zero recipes.
    """
    enabled_docs = await _WCDoc.find(
        _WCDoc.workspace == workspace_id, _WCDoc.enabled == True  # noqa: E712 — Beanie expects ==
    ).to_list()
    if not enabled_docs:
        return []

    reg = _get_registry()
    available = {a.name: a for a in _available_from_registry()}
    recipes: list[WidgetRecipeResponse] = []
    for doc in enabled_docs:
        a = available.get(doc.name)
        if a is None:
            continue
        # Each connector exposes recipes via its adapter; the registry
        # holds adapter instances per-pocket. For workspace-level recipe
        # listing we instantiate from the YAML def or a native adapter
        # without connecting — recipes are static metadata.
        defn = reg.get_definition(doc.name)
        if defn is None:
            continue
        adapter = _adapter_for_definition(defn, doc.name)
        try:
            adapter_recipes = await adapter.widgets()
        except Exception as exc:  # noqa: BLE001 — bad adapter shouldn't fail the whole list
            logger.warning("widgets() raised for %s: %s", doc.name, exc)
            continue
        for r in adapter_recipes or []:
            recipes.append(
                WidgetRecipeResponse(
                    connector=doc.name,
                    connector_display_name=a.display_name,
                    title=getattr(r, "title", str(r)),
                    display_type=getattr(r, "display_type", "stats"),
                    action=getattr(r, "action", ""),
                    params=getattr(r, "params", {}) or {},
                    default_size=getattr(r, "default_size", "col-1 row-1"),
                    description=getattr(r, "description", ""),
                ),
            )
    return recipes


def _adapter_for_definition(defn, name: str):
    """Build an adapter without connecting — for static metadata reads.

    Prefers the native adapter when ``ConnectorRegistry`` knows one
    (Gmail in PR-3; Calendar / Docs / Drive in PR-4..6; firebase / gcp
    in PR-9). Falls back to ``DirectRESTAdapter`` for YAML-only
    connectors.
    """
    from pocketpaw.connectors.registry import _create_native_adapter
    from pocketpaw.connectors.yaml_engine import DirectRESTAdapter

    native = _create_native_adapter(name)
    if native is not None:
        return native
    return DirectRESTAdapter(defn)


async def execute(
    workspace_id: str,
    name: str,
    body: ExecuteActionRequest,
    *,
    user_id: str | None = None,
) -> ExecuteActionResponse:
    """Execute one connector action with mode-aware dispatch.

    - ``cloud`` actions run in-process via the adapter.
    - ``local`` actions are forwarded to the user's pocketpaw runtime
      via ``connector.exec.requested`` on the chat WebSocket bus. PR-9
      lands the runtime listener; for now the dispatch returns a
      ``CloudError(503, "connector.local_agent_unavailable", ...)`` so
      callers see a clear "needs PR-9" signal instead of a silent fail.
    - ``sandbox`` actions raise 501 — reserved for a future PR.

    Tenancy: caller passes ``workspace_id`` from ``current_workspace_id``.
    The cloud router enforces auth; this function trusts ``workspace_id``
    is the right scope.
    """
    body = ExecuteActionRequest.model_validate(body)
    available = {a.name: a for a in _available_from_registry()}
    if name not in available:
        raise NotFound("connector", name)

    reg = _get_registry()
    defn = reg.get_definition(name)
    if defn is None:
        raise NotFound("connector", name)

    adapter = _adapter_for_definition(defn, name)
    schemas = await adapter.actions()
    schema = next((s for s in schemas if s.name == body.action), None)
    if schema is None:
        raise NotFound("connector.action", body.action)

    mode = schema.execution_mode

    if mode == ExecutionMode.SANDBOX:
        raise CloudError(
            501,
            "connector.sandbox_not_implemented",
            "sandbox execution is reserved for a future PR — see CHARTER.md §3 out of scope",
        )

    if mode == ExecutionMode.LOCAL:
        # PR-2: the bus listener doesn't exist yet (lands in PR-9).
        # Emit the request anyway so subscribers in tests can observe
        # the dispatch contract; return a structured 503 that the
        # frontend can show as "open your local PocketPaw".
        await event_bus.emit(
            "connector.exec.requested",
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "connector": name,
                "action": body.action,
                "params": body.params,
                "scope": body.scope,
                "requires_binary": schema.requires_binary,
            },
        )
        raise CloudError(
            503,
            "connector.local_agent_unavailable",
            "this action runs on your local PocketPaw runtime, "
            "which isn't connected. Open your local app and retry.",
        )

    # CLOUD path — run in-process. Connect first if needed, using the
    # workspace's saved config from the connector entity.
    doc = await _WCDoc.find_one(_WCDoc.workspace == workspace_id, _WCDoc.name == name)
    config = dict(doc.config) if doc else {}
    pocket_key = body.pocket_id or workspace_id
    if not adapter._connected:  # noqa: SLF001 — adapter API doesn't expose is_connected
        await adapter.connect(pocket_key, config)

    result = await adapter.execute(body.action, body.params)
    return ExecuteActionResponse(
        success=result.success,
        data=result.data,
        error=result.error,
        records_affected=result.records_affected,
        execution_mode=ExecutionMode.CLOUD.value,
    )


__all__ = [
    "disable_connector",
    "enable_connector",
    "execute",
    "get_connector",
    "list_connectors",
    "list_widget_recipes",
    "record_sync",
    "update_config",
]
