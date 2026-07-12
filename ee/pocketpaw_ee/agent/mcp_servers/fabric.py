# fabric.py — in-process MCP server exposing read-only Fabric ontology access
# to the claude_agent_sdk cloud chat backend. Created: 2026-06-11
# (feat/fabric-instinct-mcp-providers).
# Updated: 2026-07-11 (FST-7 — provenance on reads) — fabric_query gained an
#   opt-in ``include_provenance`` arg (default false; response shape unchanged
#   without it): attaches per-property trust detail for statement-tracked
#   properties via store.get_object_provenance — disputed/unresolvable flags,
#   freshness tri-state, and the winning value's writer + source. Best-effort
#   per object; a provenance failure never breaks the query.
# Updated: 2026-06-11 (fix/fabric-stats-workspace-scope) — fabric_stats now
# passes the resolved workspace into the store's scoped stats()/list_types(),
# closing the live cross-tenant type-name leak the original instance-wide
# stats had on a shared fabric.db.
#
# Why this exists: on the claude_agent_sdk backend, PocketPaw registry tools
# (BaseTool) never reach the agent — only MCP servers do — and there was no
# fabric MCP provider, so the cloud chat agent had ZERO path to the Fabric
# ontology (a live deployment had to ship its own stdio workaround). This
# module makes Fabric first-class on that backend.
#
# It clones the external_actions.py / belt.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, ContextVar-sourced identity (the same
# ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the sibling servers read), and the
# ``_error_response`` / ``_success_response`` helpers. The tool ids namespace
# as ``mcp__pocketpaw_fabric__<tool>`` so the Claude Code allowlist machinery
# matches them.
#
# Two SDK @tool defs, wrapping the existing registry-tool logic
# (``pocketpaw.tools.builtin.fabric_tools``) — the tool NAMES are pinned
# (``fabric_query`` / ``fabric_stats``): a deployed skill already calls them.
#   * fabric_query — runs a FabricQuery against the fabric store, scoped to the
#     caller's workspace (W4a read filter: the tenant's rows plus legacy
#     NULL-workspace rows). Unlike the BaseTool (formatted text), results are
#     returned JSON-friendly ({total, returned, truncated, objects}) and
#     size-capped: the limit clamps to MAX_QUERY_LIMIT and oversized result
#     sets are truncated from the tail under MAX_RESULT_BYTES.
#   * fabric_stats — ontology counts + type names, scoped to the caller's
#     workspace (fix/fabric-stats-workspace-scope: the original instance-wide
#     stats leaked another tenant's experimental type names into chat on a
#     shared box). Counts mirror fabric_query's visibility exactly; the type
#     list holds only the caller workspace's own types (SZD-2: the
#     fabric_object_types table now carries a workspace_id; a NULL workspace_id
#     is a legacy/global type visible to all — see FabricStore.list_types).
#
# Updated: 2026-07-11 (feat/paw-cli, C2) — the server is no longer read-only:
# three ontology MODIFICATION tools joined the two reads, so an agent on this
# backend can manage the ontology (link CRUD + type editing), mirroring the
# HTTP surface the ontology-operator-ux wave shipped:
#   * fabric_link_create — link two objects. Both endpoints must resolve in the
#     caller's workspace (scoped get_object), and the workspace's DECLARED link
#     schema is enforced through the router's own ``_enforce_link_type`` (one
#     implementation, two surfaces) — same rules as POST /fabric/links.
#   * fabric_link_delete — remove one link. ``FabricStore.unlink`` is unscoped,
#     so the tool resolves the link through the scoped ``get_link`` first; a
#     cross-tenant or unknown id refuses (mirrors DELETE /fabric/links/{id}).
#   * fabric_type_update — rename/additive type editing via the store's
#     versioned ``update_type`` + registry re-registration, mirroring
#     PATCH /fabric/schema/types/{id}. That route is ADMIN-gated, so this tool
#     RBAC-gates on ``fabric.admin`` (workspace_admin.py's check_workspace_action
#     deny-envelope pattern) — a non-admin gets a structured denial, never a
#     write. Destructive property removal stays deferred (store semantics).
# The 2026-06-11 "writes arrive as gated proposals" posture is superseded for
# the MEMBER-tier link writes: the HTTP routes already allow any member to
# create/delete links directly (``fabric.write`` = MEMBER), so the MCP surface
# now matches the REST surface instead of being stricter than it. Every write
# is audited via record_tool_call.
#
# Security: query inputs are DATA — they are bound as SQL parameters by the
# fabric store, never interpolated. The workspace id comes from the session's
# ContextVars, never from the agent's args, so an agent cannot query or WRITE
# another tenant's rows (writes stamp the resolved workspace; deletes resolve
# through scoped reads). Result payloads are size-capped so a huge ontology
# can't blow the model context.
#
# EE→OSS boundary: this module lives in pocketpaw_ee and imports only
# ``pocketpaw`` (OSS) symbols at call time; core never imports this package.
"""Agent-side MCP surface for Fabric ontology access (reads + gated writes)."""

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_fabric"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form. A deployed skill already calls
# ``fabric_query`` / ``fabric_stats`` — keep the tool names stable.
FABRIC_QUERY_TOOL_ID = f"mcp__{SERVER_NAME}__fabric_query"
FABRIC_STATS_TOOL_ID = f"mcp__{SERVER_NAME}__fabric_stats"
FABRIC_LINK_CREATE_TOOL_ID = f"mcp__{SERVER_NAME}__fabric_link_create"
FABRIC_LINK_DELETE_TOOL_ID = f"mcp__{SERVER_NAME}__fabric_link_delete"
FABRIC_TYPE_UPDATE_TOOL_ID = f"mcp__{SERVER_NAME}__fabric_type_update"

FABRIC_TOOL_IDS = (
    FABRIC_QUERY_TOOL_ID,
    FABRIC_STATS_TOOL_ID,
    FABRIC_LINK_CREATE_TOOL_ID,
    FABRIC_LINK_DELETE_TOOL_ID,
    FABRIC_TYPE_UPDATE_TOOL_ID,
)

# Result-size caps. ``MAX_QUERY_LIMIT`` mirrors the registry tool's clamp
# (``min(limit, 50)``); ``MAX_RESULT_BYTES`` bounds the serialized JSON body so
# a wide ontology row set can't blow the model context — objects are dropped
# from the TAIL until the body fits, and the response says so (truncated=true).
MAX_QUERY_LIMIT = 50
MAX_RESULT_BYTES = 48 * 1024


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_user_id,
            current_workspace_id,
        )

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


def _get_fabric_store() -> Any | None:
    """Resolve the fabric store, or None when Fabric isn't available — the
    same lazy-import guard the registry tools use."""
    try:
        from pocketpaw.stores import get_fabric_store

        return get_fabric_store()
    except ImportError:
        return None


def _serialize_object(obj: Any) -> dict[str, Any]:
    """A JSON-friendly projection of a FabricObject — the fields the registry
    tool's text rendering surfaces, structured."""
    return {
        "id": obj.id,
        "type_name": obj.type_name,
        "properties": dict(obj.properties or {}),
        "source_connector": obj.source_connector,
        "source_id": obj.source_id,
    }


def _cap_objects(objects: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Drop objects from the tail until the serialized list fits under
    ``MAX_RESULT_BYTES``. Returns ``(kept, truncated)``."""
    kept = list(objects)
    truncated = False
    while kept and len(json.dumps(kept, default=str).encode("utf-8")) > MAX_RESULT_BYTES:
        kept.pop()
        truncated = True
    return kept, truncated


async def _fabric_query_handler(args: dict) -> dict:
    """MCP handler for ``fabric__fabric_query``.

    Resolves identity, validates inputs, runs the FabricQuery scoped to the
    caller's workspace, and returns ``{total, returned, truncated, objects}``.
    Read-only — never writes. Errors return a plain relayable message.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "fabric_query requires workspace context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=_user_id,
        tool_server="pocketpaw_fabric",
        tool_name="_fabric_query",
        status="ok",
        ok=True,
    )

    # A `filters` map the model stringified into JSON is decoded here so the
    # query runs on the first call instead of erroring back to the agent.
    args = coerce_json_object_args(args, ("filters",))
    type_name = args.get("type_name")
    linked_to = args.get("linked_to")
    link_type = args.get("link_type")
    filters = args.get("filters")
    limit = args.get("limit", 20)
    # FST-7: opt-in provenance — default False so the default response shape
    # (and its byte budget) is unchanged; the agent asks for it when trust
    # matters ("where did this figure come from / is it disputed / stale?").
    include_provenance = bool(args.get("include_provenance", False))

    for field_name, value in (
        ("type_name", type_name),
        ("linked_to", linked_to),
        ("link_type", link_type),
    ):
        if value is not None and not isinstance(value, str):
            return _error_response(f"fabric_query `{field_name}` must be a string.")
    if filters is not None and not isinstance(filters, dict):
        return _error_response(
            "fabric_query `filters` must be a JSON object mapping property names "
            "to a scalar (equality) or an operator map (comparison)."
        )
    if not isinstance(limit, int) or limit < 1:
        return _error_response("fabric_query `limit` must be a positive integer.")

    store = _get_fabric_store()
    if store is None:
        return _error_response("Fabric is not available (enterprise feature).")

    try:
        from pocketpaw.fabric.models import FabricQuery

        result = await store.query(
            FabricQuery(
                type_name=type_name,
                linked_to=linked_to,
                link_type=link_type,
                filters=filters or {},
                limit=min(limit, MAX_QUERY_LIMIT),
            ),
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fabric_query failed (type=%s)", type_name, exc_info=True)
        return _error_response(f"could not query Fabric: {exc}")

    # Best-effort trace emission — same telemetry the registry tool publishes
    # (a no-op unless a proposal trace is actively collecting). Never fails the
    # query response.
    try:
        from pocketpaw.tools.builtin.fabric_tools import _emit_trace_events

        await _emit_trace_events(
            "fabric_query",
            [{"object_id": o.id, "object_type": o.type_name} for o in result.objects],
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the read
        logger.debug("fabric_query trace emission skipped", exc_info=True)

    objects = [_serialize_object(o) for o in result.objects]

    # FST-7: attach per-property provenance (disputed / unresolvable /
    # freshness / winner source) for statement-TRACKED properties only —
    # untracked properties don't appear (single-source, nothing to explain).
    # Best-effort per object: a provenance failure never breaks the query.
    if include_provenance:
        for entry in objects:
            try:
                entry["provenance"] = await store.get_object_provenance(
                    entry["id"], workspace_id=workspace_id
                )
            except Exception:  # noqa: BLE001 — additive surface, never fatal
                logger.debug("fabric_query provenance skipped for %s", entry["id"], exc_info=True)

    objects, truncated = _cap_objects(objects)

    return _success_response(
        {
            "total": result.total,
            "returned": len(objects),
            "truncated": truncated,
            "objects": objects,
        }
    )


async def _fabric_stats_handler(args: dict) -> dict:
    """MCP handler for ``fabric__fabric_stats``.

    Returns ontology counts + type names: ``{types, objects, links,
    type_names}``, scoped to the caller's workspace so stats and fabric_query
    agree (own rows plus legacy NULL-workspace rows). The type list holds only
    types with object rows visible to the workspace — never another tenant's
    experiment names. Read-only.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "fabric_stats requires workspace context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=_user_id,
        tool_server="pocketpaw_fabric",
        tool_name="_fabric_stats",
        status="ok",
        ok=True,
    )

    store = _get_fabric_store()
    if store is None:
        return _error_response("Fabric is not available (enterprise feature).")

    try:
        stats = await store.stats(workspace_id=workspace_id)
        types = await store.list_types(workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fabric_stats failed", exc_info=True)
        return _error_response(f"could not read Fabric stats: {exc}")

    return _success_response(
        {
            "types": stats.get("types", 0),
            "objects": stats.get("objects", 0),
            "links": stats.get("links", 0),
            "type_names": [t.name for t in types],
        }
    )


def _serialize_link(lnk: Any) -> dict[str, Any]:
    """A JSON-friendly projection of a FabricLink."""
    return {
        "id": lnk.id,
        "from_object_id": lnk.from_object_id,
        "to_object_id": lnk.to_object_id,
        "link_type": lnk.link_type,
        "properties": dict(lnk.properties or {}),
    }


def _serialize_type(obj_type: Any) -> dict[str, Any]:
    """A JSON-friendly projection of an ObjectType (schema surface fields)."""
    return {
        "id": obj_type.id,
        "name": obj_type.name,
        "version": getattr(obj_type, "version", 1),
        "description": obj_type.description,
        "properties": [
            {"name": p.name, "type": p.type, "required": p.required}
            for p in obj_type.properties
        ],
    }


async def _load_user(user_id: str) -> Any | None:
    """Load the User Beanie doc for ``user_id`` so ``check_workspace_action``
    has the ``.workspaces`` membership list it reads. Returns ``None`` on a bad
    id / missing user. Lazy imports keep the module's top-level import surface
    minimal (mirrors workspace_admin.py)."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User as _UserDoc

    try:
        return await _UserDoc.get(PydanticObjectId(user_id))
    except Exception:  # noqa: BLE001 — malformed id / no DB
        return None


async def _gate_admin(tool: str, workspace_id: str, user_id: str | None) -> dict | None:
    """RBAC-gate an ADMIN-tier tool on ``fabric.admin``.

    Mirrors workspace_admin.py's gate: the User doc is loaded so
    ``check_workspace_action`` has the membership list, and a ``Forbidden`` is
    CAUGHT and returned as a structured deny envelope (never raised) — the gate
    itself audits the denial. Returns ``None`` on PASS, or a ready-to-return
    response dict on any failure.
    """
    if not user_id:
        return _error_response(f"{tool} could not resolve the calling user for the RBAC check.")

    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    user = await _load_user(user_id)
    if user is None:
        return _error_response(f"{tool} could not resolve the calling user for the RBAC check.")

    try:
        check_workspace_action(user, workspace_id, "fabric.admin")
    except Forbidden as exc:
        logger.info(
            "%s denied: user=%s workspace=%s code=%s", tool, user_id, workspace_id, exc.code
        )
        return _success_response(
            {
                "ok": False,
                "denied": True,
                "code": exc.code,
                "message": f"editing the ontology schema requires an admin role ({exc.code}).",
            }
        )
    return None


async def _fabric_link_create_handler(args: dict) -> dict:
    """MCP handler for ``fabric__fabric_link_create``.

    Links two objects in the caller's workspace. Both endpoints must resolve
    through the SCOPED ``get_object`` (a cross-tenant / unknown id refuses),
    and the workspace's declared link schema is enforced through the router's
    own ``_enforce_link_type`` — identical rules to ``POST /fabric/links``.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "fabric_link_create requires workspace context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=_user_id,
        tool_server="pocketpaw_fabric",
        tool_name="_fabric_link_create",
        status="ok",
        ok=True,
    )

    args = coerce_json_object_args(args, ("properties",))
    from_id = args.get("from_id")
    to_id = args.get("to_id")
    link_type = args.get("link_type")
    properties = args.get("properties")

    for field_name, value in (("from_id", from_id), ("to_id", to_id), ("link_type", link_type)):
        if not value or not isinstance(value, str):
            return _error_response(
                f"fabric_link_create `{field_name}` is required and must be a string."
            )
    if properties is not None and not isinstance(properties, dict):
        return _error_response("fabric_link_create `properties` must be a JSON object.")

    store = _get_fabric_store()
    if store is None:
        return _error_response("Fabric is not available (enterprise feature).")

    try:
        for field_name, obj_id in (("from_id", from_id), ("to_id", to_id)):
            obj = await store.get_object(obj_id, workspace_id=workspace_id)
            if obj is None:
                return _error_response(
                    f"fabric_link_create `{field_name}` {obj_id!r} was not found in this "
                    "workspace."
                )

        # One enforcement implementation, two surfaces: reuse the EE router's
        # declared-link-schema check (no declarations -> no-op).
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.fabric.router import LinkRequest, _enforce_link_type

        req = LinkRequest(
            from_id=from_id, to_id=to_id, link_type=link_type, properties=properties or {}
        )
        try:
            await _enforce_link_type(store, workspace_id, req)
        except CloudError as exc:
            return _error_response(exc.message)

        lnk = await store.link(
            from_id=from_id,
            to_id=to_id,
            link_type=link_type,
            properties=properties or {},
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fabric_link_create failed (link_type=%s)", link_type, exc_info=True)
        return _error_response(f"could not create the link: {exc}")

    return _success_response({"created": True, "link": _serialize_link(lnk)})


async def _fabric_link_delete_handler(args: dict) -> dict:
    """MCP handler for ``fabric__fabric_link_delete``.

    Deletes one link. ``FabricStore.unlink`` is unscoped by design, so the
    tenancy guard lives here: the link is resolved through the SCOPED
    ``get_link`` first — a cross-tenant or unknown id refuses without leaking
    whether it exists elsewhere. Mirrors ``DELETE /fabric/links/{id}``.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "fabric_link_delete requires workspace context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=_user_id,
        tool_server="pocketpaw_fabric",
        tool_name="_fabric_link_delete",
        status="ok",
        ok=True,
    )

    link_id = args.get("link_id")
    if not link_id or not isinstance(link_id, str):
        return _error_response("fabric_link_delete `link_id` is required and must be a string.")

    store = _get_fabric_store()
    if store is None:
        return _error_response("Fabric is not available (enterprise feature).")

    try:
        link = await store.get_link(link_id, workspace_id=workspace_id)
        if link is None:
            return _error_response(f"link {link_id!r} was not found in this workspace.")
        await store.unlink(link_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fabric_link_delete failed (link_id=%s)", link_id, exc_info=True)
        return _error_response(f"could not delete the link: {exc}")

    return _success_response({"deleted": True, "link": _serialize_link(link)})


async def _fabric_type_update_handler(args: dict) -> dict:
    """MCP handler for ``fabric__fabric_type_update``.

    Rename/additive type editing — versions the type and migrates existing
    objects through the store's ``update_type`` (a property rename moves the
    key; an added property with a default is backfilled; destructive removal
    is deferred). Mirrors ``PATCH /fabric/schema/types/{id}`` including its
    gate: that route is ADMIN-only, so this tool RBAC-checks ``fabric.admin``
    and returns a structured denial for a non-admin. The registry is
    re-registered after the write, exactly like the route handler.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "fabric_type_update requires workspace context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=_user_id,
        tool_server="pocketpaw_fabric",
        tool_name="_fabric_type_update",
        status="ok",
        ok=True,
    )

    denied = await _gate_admin("fabric_type_update", workspace_id, _user_id)
    if denied is not None:
        return denied

    args = coerce_json_object_args(args, ("properties", "renames"))
    type_name = args.get("type_name")
    renames = args.get("renames")
    properties = args.get("properties")
    description = args.get("description")

    if not type_name or not isinstance(type_name, str):
        return _error_response("fabric_type_update `type_name` is required and must be a string.")
    if renames is not None and not isinstance(renames, dict):
        return _error_response(
            "fabric_type_update `renames` must be a JSON object mapping "
            "old property names to new ones."
        )
    if properties is not None and not isinstance(properties, list):
        return _error_response(
            "fabric_type_update `properties` must be an array of {name, type} objects."
        )
    if description is not None and not isinstance(description, str):
        return _error_response("fabric_type_update `description` must be a string.")
    if renames is None and properties is None and description is None:
        return _error_response(
            "fabric_type_update needs at least one change: `renames`, `properties`, "
            "or `description`."
        )

    store = _get_fabric_store()
    if store is None:
        return _error_response("Fabric is not available (enterprise feature).")

    try:
        from pocketpaw.fabric.models import PropertyDef

        prop_defs: list[Any] | None = None
        if properties is not None:
            prop_defs = []
            for i, raw in enumerate(properties):
                if not isinstance(raw, dict):
                    return _error_response(
                        f"fabric_type_update properties[{i}] must be a {{name, type}} object."
                    )
                try:
                    prop_defs.append(PropertyDef(**raw))
                except Exception as exc:  # pydantic ValidationError
                    return _error_response(f"invalid property definition at index {i}: {exc}")

        obj_type = await store.get_type_by_name(type_name, workspace_id=workspace_id)
        if obj_type is None:
            return _error_response(f"object type {type_name!r} was not found in this workspace.")

        updated = await store.update_type(
            obj_type.id,
            properties=prop_defs,
            renames=renames,
            description=description,
            workspace_id=workspace_id,
        )
        if updated is None:
            return _error_response(f"object type {type_name!r} was not found in this workspace.")

        # Registry re-registration, mirroring the route handler.
        from pocketpaw_ee.fabric.storage import get_registry_store

        reg = get_registry_store()
        reg.register_entity_type(workspace_id, updated.name)
        for prop in updated.properties:
            reg.register_property(workspace_id, updated.name, prop.name, prop.type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fabric_type_update failed (type=%s)", type_name, exc_info=True)
        return _error_response(f"could not update the type: {exc}")

    return _success_response({"updated": True, "type": _serialize_type(updated)})


def build_fabric_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for Fabric ontology access (two
    reads + three modification tools), or return ``None`` if the Claude Agent
    SDK isn't installed.

    Matches the shape returned by ``build_belt_server`` (``(name, server)`` or
    ``None``) so the backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_fabric MCP disabled")
        return None

    @tool(
        "fabric_query",
        (
            "Query the Fabric ontology to find business objects and their "
            "relationships. Search by object type (e.g. 'Customer', 'Order'), "
            "filter by property values, or traverse links between objects. "
            "READ-ONLY — never creates or modifies anything. Results are scoped "
            "to the current workspace. Args: `type_name` (object type to "
            "search), `linked_to` (find objects linked to this object id), "
            "`link_type` (filter links by type), `filters` (property filters — "
            'a scalar for equality or an operator map like {"rent": {">": '
            "1000}}), `limit` (max results, default 20, cap 50), "
            "`include_provenance` (bool, default false — adds per-property "
            "trust detail for multi-source properties: disputed/unresolvable "
            "flags, freshness fresh|aging|stale, and the winning value's "
            "writer + source; use it when asked where a figure came from or "
            "whether it can be trusted). Returns "
            "{total, returned, truncated, objects:[{id, type_name, properties, "
            "source_connector, source_id, provenance?}]}. An error means relay "
            "the reason."
        ),
        {
            "type": "object",
            "properties": {
                "type_name": {
                    "type": "string",
                    "description": "Object type to search (e.g. 'Customer', 'Order').",
                },
                "linked_to": {
                    "type": "string",
                    "description": "Find objects linked to this object ID.",
                },
                "link_type": {
                    "type": "string",
                    "description": "Filter links by type (e.g. 'has_order', 'belongs_to').",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Filter objects by property values. Scalar = equality; "
                        "operator map = comparison (=, !=, >, >=, <, <=)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20, cap 50).",
                },
                "include_provenance": {
                    "type": "boolean",
                    "description": (
                        "Attach per-property trust detail (disputed, freshness, "
                        "winning source) for multi-source properties. Default false."
                    ),
                },
            },
            "additionalProperties": False,
        },
    )
    async def fabric_query(args):  # type: ignore[no-untyped-def]
        return await _fabric_query_handler(args)

    @tool(
        "fabric_stats",
        (
            "Get statistics about the Fabric ontology: number of object types, "
            "objects, and links, plus the list of type names — scoped to the "
            "current workspace, consistent with fabric_query. READ-ONLY. Takes "
            "no arguments. Returns {types, objects, links, type_names}. An "
            "error means relay the reason."
        ),
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    async def fabric_stats(args):  # type: ignore[no-untyped-def]
        return await _fabric_stats_handler(args)

    @tool(
        "fabric_link_create",
        (
            "Create a LINK between two existing objects in the Fabric "
            "ontology (e.g. connect an Order to its Customer). Both objects "
            "must exist in the current workspace, and if the workspace has "
            "declared link types the link must match one (type name + "
            "endpoint object types), else it is refused with the reason. "
            "Args: `from_id` (source object id), `to_id` (target object id), "
            "`link_type` (relationship name, e.g. 'has_order'), `properties` "
            "(optional JSON object of link metadata). Returns {created, "
            "link:{id, from_object_id, to_object_id, link_type, properties}}. "
            "An error means relay the reason."
        ),
        {
            "type": "object",
            "properties": {
                "from_id": {
                    "type": "string",
                    "description": "Source object ID.",
                },
                "to_id": {
                    "type": "string",
                    "description": "Target object ID.",
                },
                "link_type": {
                    "type": "string",
                    "description": "Relationship type (e.g. 'has_order', 'belongs_to').",
                },
                "properties": {
                    "type": "object",
                    "description": "Optional metadata to store on the link.",
                },
            },
            "required": ["from_id", "to_id", "link_type"],
            "additionalProperties": False,
        },
    )
    async def fabric_link_create(args):  # type: ignore[no-untyped-def]
        return await _fabric_link_create_handler(args)

    @tool(
        "fabric_link_delete",
        (
            "Delete one LINK from the Fabric ontology by its link id (find "
            "ids via fabric_query's linked_to traversal or the links list). "
            "Only removes the relationship — the objects on both ends are "
            "untouched. Scoped to the current workspace: a link id from "
            "another workspace refuses. Args: `link_id`. Returns {deleted, "
            "link}. An error means relay the reason."
        ),
        {
            "type": "object",
            "properties": {
                "link_id": {
                    "type": "string",
                    "description": "ID of the link to delete.",
                },
            },
            "required": ["link_id"],
            "additionalProperties": False,
        },
    )
    async def fabric_link_delete(args):  # type: ignore[no-untyped-def]
        return await _fabric_link_delete_handler(args)

    @tool(
        "fabric_type_update",
        (
            "Edit an object TYPE's schema in the Fabric ontology — rename "
            "properties and/or add new ones (rename + additive only; "
            "removing a property never scrubs existing data). Requires an "
            "ADMIN role in the workspace; non-admins get a structured "
            "denial. The type's version is bumped and existing objects are "
            "migrated (renamed keys move; an added property with a default "
            "is backfilled). Args: `type_name` (which type to edit), "
            "`renames` (JSON object mapping old property name -> new name), "
            "`properties` (array of {name, type, required?, default?} — "
            "REPLACES the declared schema, so include existing properties "
            "you want to keep), `description` (new type description). At "
            "least one change is required. Returns {updated, type:{id, "
            "name, version, description, properties}}. An error means relay "
            "the reason."
        ),
        {
            "type": "object",
            "properties": {
                "type_name": {
                    "type": "string",
                    "description": "Name of the object type to edit (e.g. 'Customer').",
                },
                "renames": {
                    "type": "object",
                    "description": "Map of old property name -> new property name.",
                },
                "properties": {
                    "type": "array",
                    "description": (
                        "Declared schema replacement: array of {name, type, "
                        "required?, default?} objects. Include properties to keep."
                    ),
                    "items": {"type": "object"},
                },
                "description": {
                    "type": "string",
                    "description": "New human-readable description for the type.",
                },
            },
            "required": ["type_name"],
            "additionalProperties": False,
        },
    )
    async def fabric_type_update(args):  # type: ignore[no-untyped-def]
        return await _fabric_type_update_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            fabric_query,
            fabric_stats,
            fabric_link_create,
            fabric_link_delete,
            fabric_type_update,
        ],
    )
    return SERVER_NAME, server


__all__ = [
    "FABRIC_LINK_CREATE_TOOL_ID",
    "FABRIC_LINK_DELETE_TOOL_ID",
    "FABRIC_QUERY_TOOL_ID",
    "FABRIC_STATS_TOOL_ID",
    "FABRIC_TOOL_IDS",
    "FABRIC_TYPE_UPDATE_TOOL_ID",
    "SERVER_NAME",
    "build_fabric_server",
]
