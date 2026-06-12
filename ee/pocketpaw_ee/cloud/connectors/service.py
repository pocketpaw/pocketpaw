# Connectors — workspace-scoped business logic.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation.
# Updated: 2026-06-07 (M3 connector→skill auto-authoring) — enable/disable of a
#   POCKET-scoped connector now RE-DERIVES the pocket's surface_profile from ALL
#   its enabled pocket-scoped connectors (``_rederive_pocket_surface_profile``)
#   and persists it via ``pockets.service.apply_derived_surface_profile`` (the
#   Beanie-write boundary — this service never imports the Pocket doc). Derivation
#   itself is the pure ``derivation.derive_surface_profile``.
# Updated: 2026-06-08 (Phase B chunk 7 — purge path) — added
#   ``disconnect_member``: the member-facing "disconnect my accounts" path. It
#   delegates to member_ingest.purge.purge_member_data to delete the caller's
#   own per-user KB scope, OAuth tokens, connector rows, and ingest-state.
#   Bound to the authenticated caller at the router (member_id == caller).
# Updated: 2026-06-08 (connector-mcp-execution / keystone) — added
#   ``list_pocket_connectors`` so the cloud chat agent's in-process MCP server
#   (``ee/pocketpaw_ee/agent/mcp_servers/connectors.py``) can enumerate a
#   pocket's enabled, pocket-scoped connectors and each action's trust level /
#   execution mode WITHOUT the MCP layer importing the WorkspaceConnector Beanie
#   doc (OSS-EE boundary §2). The MCP ``connector_execute`` tool reuses the
#   existing ``execute(...)`` for read (auto-trust) actions and blocks
#   write/confirm-trust actions in v1.
# Updated: 2026-06-12 (feat/connector-as-pocket-backend) — added
#   ``is_connector_enabled_for_workspace``: the validation gate
#   ``pockets.service.set_pocket_backend`` calls before binding a pocket to a
#   ``backend_type="connector"`` backend. Keeps the WorkspaceConnector Beanie
#   read in THIS service (the owner of the connector docs) so the pockets
#   service never imports the connector model.
# Updated: 2026-06-12 (workspace-scope visibility) — ``list_pocket_connectors``
#   and ``is_connector_bound_to_pocket`` now ALSO match ``scope="workspace"``
#   rows: a workspace-enabled connector is visible/usable from every pocket in
#   that workspace, not only pockets with an explicit pocket-scoped row. Fixes
#   the UI-says-connected / agent-says-no-connectors split (the connectors page
#   reads the workspace catalog; the agent MCP tool read only pocket-scoped
#   rows). Cross-tenant posture unchanged — workspace scope IS the tenant; the
#   read/write trust gate still applies per action.
# Updated: 2026-06-12 (connector-store-unification CS-3) — the cloud
#   ``execute()`` path now goes through ``registry.ensure_connected`` (scope
#   keys ``pocket:<pocket_id>`` / ``ws:<workspace_id>``) backed by the
#   WorkspaceConnector state store, so a fresh process executes from a seeded
#   doc with NO prior /connect; the inline ``adapter._connected`` check is
#   gone. ``enable_connector`` / ``update_config`` / ``disable_connector``
#   drop any live registry adapter for the touched row so the next execute
#   rehydrates with current config instead of serving a stale connection.
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

from beanie.operators import And, Or

from pocketpaw.connectors.protocol import ExecutionMode
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    ConnectorConfigUpdated,
    ConnectorDisabled,
    ConnectorEnabled,
    ConnectorSyncRecorded,
)
from pocketpaw_ee.cloud.connectors.domain import (
    AvailableConnector,
    ConnectorActionInfo,
    ConnectorSurfaceContribution,
    PocketConnectorInfo,
    WorkspaceConnector,
)
from pocketpaw_ee.cloud.connectors.dto import (
    ConnectorDetailResponse,
    ConnectorResponse,
    EnableConnectorRequest,
    ExecuteActionRequest,
    ExecuteActionResponse,
    UpdateConnectorConfigRequest,
    WidgetRecipeResponse,
)
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector as _WCDoc
from pocketpaw_ee.cloud.shared.events import event_bus

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
        # M3 — carry the connector's surface-profile contribution (skill + tool
        # patterns) into the cloud domain, translating the OSS
        # ``ConnectorSurfaceProfile`` to the cloud-side mirror. ``None`` when the
        # YAML has no block.
        sp = getattr(d, "surface_profile", None)
        contribution = (
            ConnectorSurfaceContribution(
                skill=sp.skill,
                allow_tools=tuple(sp.allow_tools),
                deny_tools=tuple(sp.deny_tools),
            )
            if sp is not None
            else None
        )
        out.append(
            AvailableConnector(
                name=d.name,
                display_name=d.display_name,
                type=d.type,
                icon=d.icon,
                auth_method=auth_method,
                actions=actions,
                surface_profile=contribution,
            ),
        )
    return out


# ---------------------------------------------------------------------------
# M3 — connector→skill/tool surface-profile auto-authoring
# ---------------------------------------------------------------------------


async def _rederive_pocket_surface_profile(workspace_id: str, pocket_id: str) -> None:
    """Re-derive + persist a pocket's surface_profile from its enabled connectors.

    Triggered on enable/disable of a POCKET-scoped connector. Loads ALL connectors
    enabled at ``scope=pocket`` for this ``pocket_id`` (tenant-filtered, cloud rule
    §7), merges each with its registry def to recover its ``surface_profile``
    contribution, runs the pure ``derive_surface_profile`` over the full set, then
    hands the result to ``pockets.service.apply_derived_surface_profile`` for the
    Beanie write.

    Deriving from the FULL enabled set (not just the toggled connector) is what
    makes enable AND disable correct: a disabled connector simply isn't in the set,
    so its contribution drops on the next re-derive.

    The Pocket-doc write lives in ``pockets.service`` (Beanie-write boundary —
    this connectors service must not import the Pocket model). Imported lazily to
    keep import-linter's static graph clean and avoid an import cycle.
    """
    from pocketpaw_ee.cloud.connectors.derivation import derive_surface_profile
    from pocketpaw_ee.cloud.pockets.service import apply_derived_surface_profile

    enabled_docs = await _WCDoc.find(
        _WCDoc.workspace == workspace_id,
        _WCDoc.pocket_id == pocket_id,
        _WCDoc.scope == "pocket",
        _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
    ).to_list()

    available = {a.name: a for a in _available_from_registry()}
    contributing = [available[d.name] for d in enabled_docs if d.name in available]
    profile = derive_surface_profile(contributing)
    await apply_derived_surface_profile(workspace_id, pocket_id, profile)


async def _drop_live_adapter(doc: _WCDoc) -> None:
    """Drop any live registry adapter for this row's scope keys.

    Called after enable/disable/config writes so the next execute rehydrates
    through ``ensure_connected`` with the row's CURRENT state instead of
    serving an adapter connected with stale config (or one for a row that was
    just disabled). ``registry.disconnect`` closes the adapter's held handles;
    the durable row itself is untouched — the cloud state store's delete is a
    deliberate no-op for namespaced keys (see ``state_provider.py``).
    """
    reg = _get_registry()
    keys = [f"ws:{doc.workspace}"]
    if doc.pocket_id:
        keys.append(f"pocket:{doc.pocket_id}")
    for key in keys:
        try:
            await reg.disconnect(key, doc.name)
        except Exception as exc:  # noqa: BLE001 — best-effort cache drop
            logger.warning("live adapter drop failed for %s (%s): %s", doc.name, key, exc)


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
        # Re-enable may have changed config/scope — a live adapter from a
        # previous execute must not keep serving the old connection.
        await _drop_live_adapter(doc)

    a = available[name]
    await event_bus.emit(
        "connector.enabled",
        {"workspace_id": workspace_id, "name": name, "scope": body.scope},
    )
    await emit(
        ConnectorEnabled(
            data={
                "workspace_id": workspace_id,
                "name": name,
                "scope": body.scope,
            }
        )
    )

    # M3 — auto-author the pocket's surface_profile from its enabled connectors.
    # Re-derive from the FULL enabled set so this is idempotent and order-free.
    if body.scope == "pocket" and body.pocket_id:
        await _rederive_pocket_surface_profile(workspace_id, body.pocket_id)

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
    # Capture the binding BEFORE flipping the flag — a pocket-scoped connector
    # being disabled must re-derive that pocket's surface_profile so its
    # contribution drops out of the union.
    was_pocket_scoped = doc.scope == "pocket" and bool(doc.pocket_id)
    pocket_id_for_rederive = doc.pocket_id
    doc.enabled = False
    await doc.save()
    # A disabled connector must stop executing immediately — drop any live
    # adapter so the next execute can't reuse the old connection.
    await _drop_live_adapter(doc)
    await event_bus.emit(
        "connector.disabled",
        {"workspace_id": workspace_id, "name": name},
    )
    await emit(
        ConnectorDisabled(
            data={
                "workspace_id": workspace_id,
                "name": name,
            }
        )
    )

    # M3 — re-derive after the flag flip so the disabled connector (now
    # ``enabled=False``) is excluded from the enabled set used by derivation.
    if was_pocket_scoped and pocket_id_for_rederive:
        await _rederive_pocket_surface_profile(workspace_id, pocket_id_for_rederive)

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
    # Config changed — drop any live adapter so the next execute reconnects
    # with the patched config instead of the one it was built with.
    await _drop_live_adapter(doc)
    await event_bus.emit(
        "connector.config_updated",
        {"workspace_id": workspace_id, "name": name},
    )
    await emit(
        ConnectorConfigUpdated(
            data={
                "workspace_id": workspace_id,
                "name": name,
            }
        )
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
    await emit(
        ConnectorSyncRecorded(
            data={
                "workspace_id": workspace_id,
                "name": name,
                "status": status,
            }
        )
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
        _WCDoc.workspace == workspace_id,
        _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
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


async def list_pocket_connectors(workspace_id: str, pocket_id: str) -> list[PocketConnectorInfo]:
    """List the connectors enabled + bound to ONE pocket, with their actions.

    The agent-facing companion to ``list_connectors``: where that returns the
    whole catalog with workspace state, this returns only the connectors a
    specific room (pocket) can actually use, each with its action surface
    classified by trust level so the agent MCP server can show read actions as
    callable and write actions as "needs approval (v2)".

    Tenant-filtered on ``workspace`` (cloud rule §7); matches rows enabled for
    THIS pocket (``scope == "pocket"`` + ``pocket_id``) OR workspace-wide
    (``scope == "workspace"`` — available from every pocket in the tenant).
    The Beanie read lives here (not in the MCP layer) so the OSS-EE boundary
    holds: the MCP server imports this service, never the
    ``WorkspaceConnector`` doc.

    Trust gating: ``auto``-trust actions are read-first (``is_read=True``);
    ``confirm`` / ``restricted`` actions are write-shaped and marked
    ``is_read=False`` so the caller blocks them in v1.
    """
    enabled_docs = await _WCDoc.find(
        _WCDoc.workspace == workspace_id,
        Or(
            _WCDoc.scope == "workspace",
            And(_WCDoc.scope == "pocket", _WCDoc.pocket_id == pocket_id),
        ),
        _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
    ).to_list()
    if not enabled_docs:
        return []

    reg = _get_registry()
    available = {a.name: a for a in _available_from_registry()}
    out: list[PocketConnectorInfo] = []
    seen: set[str] = set()
    for doc in enabled_docs:
        if doc.name in seen:
            continue
        seen.add(doc.name)
        a = available.get(doc.name)
        defn = reg.get_definition(doc.name)
        if a is None or defn is None:
            continue
        adapter = _adapter_for_definition(defn, doc.name)
        try:
            schemas = await adapter.actions()
        except Exception as exc:  # noqa: BLE001 — a bad adapter shouldn't drop the list
            logger.warning("actions() raised for %s: %s", doc.name, exc)
            continue
        action_infos = tuple(
            ConnectorActionInfo(
                name=s.name,
                description=s.description,
                trust_level=str(s.trust_level),
                execution_mode=str(s.execution_mode),
                is_read=str(s.trust_level) == "auto",
            )
            for s in schemas
        )
        out.append(
            PocketConnectorInfo(
                name=a.name,
                display_name=a.display_name,
                type=a.type,
                icon=a.icon,
                actions=action_infos,
            )
        )
    return out


async def get_action_trust(name: str, action: str) -> ConnectorActionInfo | None:
    """Look up one action's trust classification on a connector.

    Used by the agent MCP server's ``connector_execute`` gate to decide read
    (auto → execute) vs write (confirm/restricted → block) BEFORE calling
    ``execute``. Registry-only (no tenant read) — the action schema is static
    catalog metadata. Returns ``None`` when the connector or action is unknown.
    """
    reg = _get_registry()
    defn = reg.get_definition(name)
    if defn is None:
        return None
    adapter = _adapter_for_definition(defn, name)
    schemas = await adapter.actions()
    schema = next((s for s in schemas if s.name == action), None)
    if schema is None:
        return None
    return ConnectorActionInfo(
        name=schema.name,
        description=schema.description,
        trust_level=str(schema.trust_level),
        execution_mode=str(schema.execution_mode),
        is_read=str(schema.trust_level) == "auto",
    )


async def is_connector_bound_to_pocket(workspace_id: str, pocket_id: str, name: str) -> bool:
    """True when ``name`` is enabled for this pocket or workspace-wide.

    The tenant gate the MCP ``connector_execute`` tool checks first: an agent in
    pocket A must not run a connector only bound to pocket B. Workspace-scoped
    rows pass for every pocket in the tenant — workspace scope IS the tenant
    boundary, so there is no cross-pocket leak. Tenant-filtered (cloud rule §7).
    """
    doc = await _WCDoc.find_one(
        _WCDoc.workspace == workspace_id,
        Or(
            _WCDoc.scope == "workspace",
            And(_WCDoc.scope == "pocket", _WCDoc.pocket_id == pocket_id),
        ),
        _WCDoc.name == name,
        _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
    )
    return doc is not None


async def is_connector_enabled_for_workspace(workspace_id: str, name: str) -> bool:
    """True when ``name`` is a real registry connector AND enabled for the workspace.

    The validation gate ``pockets.service.set_pocket_backend`` uses before it
    binds a pocket to ``backend_type="connector"``: a connector backend must
    name a connector the registry knows and that this workspace has actually
    enabled (any scope). Keeping the ``WorkspaceConnector`` Beanie read HERE
    (not in the pockets service) holds the boundary — the pockets service owns
    the pocket/backend docs, this service owns the connector docs. Both stay
    sole writers/readers of their own collection.

    Tenant-filtered on ``workspace`` (cloud rule §7). An unknown registry name
    or a workspace with no enabled row for it returns ``False``.
    """
    available = {a.name for a in _available_from_registry()}
    if name not in available:
        return False
    doc = await _WCDoc.find_one(
        _WCDoc.workspace == workspace_id,
        _WCDoc.name == name,
        _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
    )
    return doc is not None


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

    # CLOUD path — run in-process through the registry's durable seam (CS-3).
    # ``ensure_connected`` rehydrates the adapter from the WorkspaceConnector
    # row's config on a fresh process, so execute works with no prior
    # /connect call. Pocket-keyed lookup is gated on the tenant bind check —
    # ``pocket:<pocket_id>`` alone carries no workspace filter, so an
    # unverified foreign pocket_id must never select another tenant's config.
    exec_adapter = None
    if body.pocket_id and await is_connector_bound_to_pocket(workspace_id, body.pocket_id, name):
        exec_adapter = await reg.ensure_connected(name, f"pocket:{body.pocket_id}")
    if exec_adapter is None:
        exec_adapter = await reg.ensure_connected(name, f"ws:{workspace_id}")
    if exec_adapter is None:
        # Legacy fallback — no enabled row with usable config (or the
        # reconnect failed). Preserve the pre-CS-3 semantics: one-shot
        # adapter, best-effort connect with whatever the workspace row
        # carries, and let the adapter surface its own failure on execute.
        doc = await _WCDoc.find_one(_WCDoc.workspace == workspace_id, _WCDoc.name == name)
        config = dict(doc.config) if doc else {}
        pocket_key = body.pocket_id or workspace_id
        exec_adapter = adapter
        await exec_adapter.connect(pocket_key, config)

    result = await exec_adapter.execute(body.action, body.params)
    return ExecuteActionResponse(
        success=result.success,
        data=result.data,
        error=result.error,
        records_affected=result.records_affected,
        execution_mode=ExecutionMode.CLOUD.value,
    )


async def disconnect_member(workspace_id: str, member_id: str) -> dict:
    """Disconnect a member's own per-user connectors and purge their data.

    The member-facing "disconnect my accounts" path (Phase B chunk 7). A
    member connected their PERSONAL Gmail/calendar as a per-user connector and
    we ingested it into their private ``user:{member_id}`` KB scope; when they
    disconnect, all of that — KB scope, per-user OAuth tokens, connector rows,
    ingest-state — must be deleted. It's their personal data.

    ``member_id`` is bound to the authenticated caller at the router (a member
    only ever disconnects THEIR OWN accounts), so the scope this touches is a
    pure function of the caller's id — no member can purge another's data.

    Idempotent: ``purge_member_data`` is safe to call when nothing exists, so a
    double-tap or a re-disconnect is a clean no-op. Returns the purge summary.
    """
    # Lazy import — keeps the connectors service free of a member_ingest
    # dependency at module load (member_ingest already reads WorkspaceConnector
    # cross-entity, so a top-level import here would risk a cycle).
    from pocketpaw_ee.cloud.member_ingest.purge import purge_member_data

    # purge_member_data deletes the member's user-scoped connector rows along
    # with their tokens, KB scope, and ingest-state — so a self-disconnect is
    # exactly "purge everything keyed on this member". The MemberDataPurged
    # event it emits is the canonical signal the home surface reacts to.
    return await purge_member_data(workspace_id, member_id)


__all__ = [
    "disable_connector",
    "disconnect_member",
    "enable_connector",
    "execute",
    "get_action_trust",
    "get_connector",
    "is_connector_bound_to_pocket",
    "is_connector_enabled_for_workspace",
    "list_connectors",
    "list_pocket_connectors",
    "list_widget_recipes",
    "record_sync",
    "update_config",
]
