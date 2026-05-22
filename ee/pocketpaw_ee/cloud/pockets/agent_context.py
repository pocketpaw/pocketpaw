"""Agent-facing pocket helpers — back the in-process MCP write tools the
cloud SSE chat agent uses to read/write the pocket it lives inside.

This module is a thin MCP-shape wrapper around ``pockets.service``:
it formats the ``{ok, error}`` returns the SDK expects, looks up
workspace/user/session identity from the per-stream ``ContextVar``s,
and pushes ``pocket_mutation`` / ``pocket_created`` events onto the
active SSE stream's queue. All Beanie reads/writes live in the
service modules.

Changes: 2026-05-22 (RFC 04 alpha follow-up) — added the
``set_source_for_agent`` / ``remove_source_for_agent`` wrappers so the
edit specialist can author the pocket's ``rippleSpec.sources`` block.
They emit a full-document ``replace`` ``pocket_mutation`` (the
frontend's ``applyMutation`` already understands ``replace``) so no
paw-enterprise change is needed.
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.pockets import service as pockets_service

logger = logging.getLogger(__name__)


async def list_pockets_for_agent() -> dict[str, Any]:
    """Return the workspace's pockets as a compact list, or an error dict.

    Identity (workspace, user) comes from the per-stream ``ContextVar``s
    set by ``agent_router._run_agent_stream``. The list-before-create
    gate in the system prompt fires this on every creation flow; the
    payload is intentionally light (no rippleSpec) so the round-trip
    is cheap.

    Shape on success: ``{"ok": True, "pockets": [{...}, ...]}``.
    Shape on failure: ``{"ok": False, "error": "..."}``.
    """
    from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return {
            "ok": False,
            "error": (
                "no active workspace/user — list_pockets can only be called "
                "from inside a cloud SSE chat stream"
            ),
        }
    pockets = await pockets_service.agent_list(workspace_id, user_id)
    return {"ok": True, "pockets": pockets}


async def fetch_pocket_for_agent(pocket_id: str) -> dict[str, Any]:
    """Return the full pocket document for an agent, or an error dict.

    Shape on success: ``{"ok": True, "pocket": {...}}``.
    Shape on failure: ``{"ok": False, "error": "..."}``.
    """
    view, err = await pockets_service.agent_view(pocket_id)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, "pocket": view}


async def _validate_ripple_spec(ripple_spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Pre-persist guard against agent prop-name drift.

    Fetches the same manifest the ``get_widget_spec`` MCP tool uses,
    walks the rippleSpec tree, and (a) auto-rewrites known inner-item
    aliases (e.g. ``feed.items[].title`` -> ``text``) and (b) logs every
    mismatch at WARN level so we can spot new drift in production.

    Returns the issues list so callers can surface them to the agent.
    Best-effort — if the manifest is unavailable, returns ``[]``. Mutates
    ``ripple_spec`` in place when aliases apply.
    """
    if not isinstance(ripple_spec, dict):
        return []
    try:
        from pocketpaw.config import get_settings
        from pocketpaw.ripple.manifest import get_manifest, validate_against_manifest

        settings = get_settings()
        manifest = await get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
        if manifest is None:
            return []
        issues = validate_against_manifest(ripple_spec, manifest, apply_aliases=True)
        for issue in issues:
            logger.warning(
                "ripple manifest drift: %s (%s) unknown=%s item_issues=%s",
                issue["path"],
                issue["type"],
                issue["unknown_props"],
                issue["item_issues"],
            )
        return issues
    except Exception:
        logger.debug("ripple manifest validation skipped (non-fatal)", exc_info=True)
        return []


def _format_manifest_warnings_for_agent(issues: list[dict[str, Any]]) -> str | None:
    """Turn ``validate_against_manifest`` issues into an agent-readable
    warnings string. ``None`` when there are no unknown-prop issues
    (item-alias issues are auto-applied and don't need agent action).

    The specialist sees this string on the MCP tool result so it can
    fix prop names on its next turn instead of re-shipping the same
    invented props.
    """
    actionable = [i for i in issues if i.get("unknown_props")]
    if not actionable:
        return None
    lines = [
        "The rippleSpec was persisted but contains widgets with invented props "
        "that the renderer ignores (cells/series render as `undefined`):",
    ]
    for issue in actionable[:10]:
        wtype = issue.get("type", "?")
        path = issue.get("path", "?")
        unknown = ", ".join(f"`{p}`" for p in issue.get("unknown_props", []))
        allowed = ", ".join(f"`{p}`" for p in issue.get("allowed_props", []))
        lines.append(f"  • {path} ({wtype}): unknown props {unknown}")
        lines.append(f"      allowed: {allowed}")
    if len(actionable) > 10:
        lines.append(f"  • …and {len(actionable) - 10} more")
    lines.append(
        "Re-emit each widget using ONLY props in its `allowed` list. "
        "Common offenders: `chart` does NOT accept `series`/`xAxis`/`dataKey`/"
        "`categoryKey` — its data is `[{label, value}]` directly, and "
        "multi-series uses `series: {key: val}` on EACH data point."
    )
    return "\n".join(lines)


def _merge_warnings(*parts: str | None) -> str | None:
    """Join non-empty warning strings with a blank line between sections.
    Returns ``None`` when every part is empty so callers can use the
    standard ``if warnings:`` guard."""
    chunks = [p for p in parts if p]
    return "\n\n".join(chunks) if chunks else None


async def _resolved_view_for_frontend(pocket_view: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``pocket_view`` with rippleSpec ``$source`` markers
    resolved against the active stream's user/workspace context.

    Used before any SSE push to the frontend. The agent itself sees raw
    markers (so it preserves them on edit) — only the rendering surface
    needs resolution. Falls back to the raw view on resolver failure or
    when ContextVars aren't set.
    """
    spec = pocket_view.get("rippleSpec")
    if not spec:
        return pocket_view
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        workspace_id = current_workspace_id()
        user_id = current_user_id()
    except Exception:
        return pocket_view
    if not workspace_id or not user_id:
        return pocket_view
    try:
        from pocketpaw_ee.cloud import ripple_sources  # noqa: F401  — register sources
        from pocketpaw_ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec

        resolved = await resolve_ripple_spec(
            spec,
            ResolveCtx(
                workspace_id=workspace_id,
                user_id=user_id,
                pocket_id=str(pocket_view.get("_id", "")),
            ),
        )
    except Exception:
        logger.warning(
            "ripple_resolver: resolve failed for pocket %s; pushing raw spec",
            pocket_view.get("_id", ""),
            exc_info=True,
        )
        return pocket_view
    return {**pocket_view, "rippleSpec": resolved}


async def _push_replace(pocket_view: dict[str, Any]) -> None:
    """Push a ``pocket_mutation`` event for a successful update / widget
    change. Imported lazily so the cloud-chat dependency stays optional
    for callers that never go through the SSE path. The frontend gets a
    resolved spec; the agent's caller still has the raw view."""
    resolved = await _resolved_view_for_frontend(pocket_view)
    try:
        from pocketpaw_ee.cloud.chat.agent_service import push_pocket_mutation

        push_pocket_mutation(
            {
                "action": "replace",
                "pocket_id": resolved.get("_id", ""),
                "pocket": resolved,
            }
        )
    except Exception:
        logger.debug("push_pocket_mutation failed (non-fatal)", exc_info=True)


async def _push_node_op(action: str, pocket_view: dict[str, Any], payload: dict[str, Any]) -> None:
    """Push a granular ``pocket_mutation`` SSE event for one of the
    five node-level ops (``node_added`` / ``node_replaced`` /
    ``node_prop_set`` / ``node_moved`` / ``node_removed``).

    Newer clients apply the op in place using ``payload`` (which carries
    the changed subtree + position info). Older clients ignore the
    unknown action — but they STILL get a full re-render via the
    realtime ``pocket.updated`` event the service layer emits on every
    write, so they stay consistent without code changes.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import push_pocket_mutation

        push_pocket_mutation(
            {
                "action": action,
                "pocket_id": pocket_view.get("_id", ""),
                **payload,
            }
        )
    except Exception:
        logger.debug("push_pocket_mutation(%s) failed (non-fatal)", action, exc_info=True)


def _spec_grammar_warnings(ripple_spec: dict[str, Any] | None) -> str | None:
    """Return an agent-readable warnings summary for the given spec, or
    ``None`` when the spec is fully grammar-clean.

    Run AFTER persistence — the warnings tell the LLM what to fix on the
    next turn, but we don't want to block writes the user can still
    interact with via the defensive widgets in the renderer.
    """
    if not isinstance(ripple_spec, dict):
        return None
    try:
        from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec
        from pocketpaw_ee.cloud.ripple_validator import (
            format_warnings_for_agent,
            validate_ripple_spec,
        )

        # Validate against the normalized shape the renderer will see —
        # not the raw agent-provided one. Otherwise the warnings would
        # reference paths that don't exist post-lift.
        normalized = normalize_ripple_spec(ripple_spec) or ripple_spec
        warnings = validate_ripple_spec(normalized)
        if not warnings:
            return None
        return format_warnings_for_agent(warnings)
    except Exception:
        logger.debug("spec grammar validation skipped (non-fatal)", exc_info=True)
        return None


async def update_pocket_for_agent(
    pocket_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    ripple_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Patch top-level pocket fields. ``ripple_spec`` is normalized.

    Only fields the caller explicitly provides are touched — passing
    ``None`` (the default) leaves the existing value alone.
    """
    manifest_issues = await _validate_ripple_spec(ripple_spec)
    view, err = await pockets_service.agent_update(
        pocket_id,
        name=name,
        description=description,
        icon=icon,
        color=color,
        ripple_spec=ripple_spec,
    )
    if err is not None:
        return {"ok": False, "error": err}
    await _push_replace(view)
    result: dict[str, Any] = {"ok": True, "pocket": view}
    warnings = _merge_warnings(
        _format_manifest_warnings_for_agent(manifest_issues),
        _spec_grammar_warnings(ripple_spec),
    )
    if warnings:
        result["warnings"] = warnings
    return result


async def add_widget_for_agent(pocket_id: str, widget: dict[str, Any]) -> dict[str, Any]:
    """Append a widget to the pocket's embedded widget list."""
    view, err = await pockets_service.agent_add_widget(pocket_id, widget)
    if err is not None:
        return {"ok": False, "error": err}
    await _push_replace(view)
    return {"ok": True, "pocket": view}


async def update_widget_for_agent(
    pocket_id: str, widget_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a single embedded widget."""
    view, err = await pockets_service.agent_update_widget(pocket_id, widget_id, fields)
    if err is not None:
        return {"ok": False, "error": err}
    await _push_replace(view)
    return {"ok": True, "pocket": view}


async def remove_widget_for_agent(pocket_id: str, widget_id: str) -> dict[str, Any]:
    """Remove a widget from the pocket's embedded widget list."""
    view, err = await pockets_service.agent_remove_widget(pocket_id, widget_id)
    if err is not None:
        return {"ok": False, "error": err}
    await _push_replace(view)
    return {"ok": True, "pocket": view}


async def create_pocket_for_agent(
    *,
    name: str,
    description: str = "",
    type_: str = "custom",
    icon: str = "",
    color: str = "",
    ripple_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a brand-new pocket owned by the currently-streaming user
    in their active workspace.

    Workspace and owner identity come from the per-stream ``ContextVar``s
    set by ``agent_router._run_agent_stream`` because the in-process MCP
    tool channel doesn't reach the FastAPI request scope. Returns the
    same ``{ok, pocket}`` shape as the other helpers and pushes a
    ``pocket_created`` SSE event so connected frontends mount the new
    pocket immediately.
    """
    from pocketpaw_ee.cloud.chat.agent_service import (
        current_session_mongo_id,
        current_user_id,
        current_workspace_id,
        push_sse_event,
    )
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return {
            "ok": False,
            "error": (
                "no active workspace/user — create_pocket can only be called "
                "from inside a cloud SSE chat stream"
            ),
        }

    manifest_issues = await _validate_ripple_spec(ripple_spec)
    view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name=name,
        description=description,
        type_=type_,
        icon=icon,
        color=color,
        ripple_spec=ripple_spec,
    )
    if err is not None or view is None or pocket_id is None:
        return {"ok": False, "error": err or "create failed"}

    # Link the active chat session to the newly-created pocket so the
    # conversation that built it shows up in the pocket's session list.
    session_mongo_id = current_session_mongo_id()
    linked_session_oid: str | None = None
    if session_mongo_id:
        linked_session_oid = await sessions_service.attach_pocket_to_session_doc(
            session_mongo_id, user_id, pocket_id
        )
        if linked_session_oid:
            try:
                from pocketpaw_ee.cloud.realtime.emit import emit
                from pocketpaw_ee.cloud.realtime.events import SessionUpdated

                await emit(
                    SessionUpdated(
                        data={
                            "session_id": linked_session_oid,
                            "user_id": user_id,
                            "pocket_id": pocket_id,
                        }
                    )
                )
            except Exception:
                logger.debug("SessionUpdated emit after pocket-link failed", exc_info=True)

    # Push a ``pocket_created`` SSE event onto the active stream so the
    # frontend mounts the new pocket without waiting for a sidebar refresh.
    # Resolve $source markers for the frontend payload; the agent's
    # return value below still has the raw view so it can preserve markers
    # on subsequent edits.
    try:
        view_for_frontend = await _resolved_view_for_frontend(view)
        push_sse_event(
            "pocket_created",
            {
                "pocket_id": pocket_id,
                "pocket": view_for_frontend,
                "session_id": session_mongo_id,
            },
        )
    except Exception:
        logger.debug("push_sse_event(pocket_created) failed", exc_info=True)

    result: dict[str, Any] = {"ok": True, "pocket": view}
    warnings = _merge_warnings(
        _format_manifest_warnings_for_agent(manifest_issues),
        _spec_grammar_warnings(ripple_spec),
    )
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# Granular rippleSpec.ui mutations (the "Read/Edit/Write" surface)
# ---------------------------------------------------------------------------


async def add_node_for_agent(
    pocket_id: str,
    parent_id: str,
    spec: dict[str, Any],
    after_id: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Insert a new node into the pocket's UI tree."""
    result, err = await pockets_service.agent_add_node(
        pocket_id, parent_id=parent_id, spec=spec, after_id=after_id, index=index
    )
    if err is not None or result is None:
        return {"ok": False, "error": err or "add_node failed"}
    pocket_view = result.get("pocket") or {}
    subtree = result.get("subtree") or {}
    await _push_node_op(
        "node_added",
        pocket_view,
        {
            "parent_id": parent_id,
            "after_id": after_id,
            "index": index,
            "subtree": subtree,
        },
    )
    return {"ok": True, "node_id": subtree.get("id"), "subtree": subtree}


async def replace_node_for_agent(
    pocket_id: str,
    node_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Replace the subtree at ``node_id``."""
    result, err = await pockets_service.agent_replace_node(pocket_id, node_id=node_id, spec=spec)
    if err is not None or result is None:
        return {"ok": False, "error": err or "replace_node failed"}
    pocket_view = result.get("pocket") or {}
    subtree = result.get("subtree") or {}
    await _push_node_op(
        "node_replaced",
        pocket_view,
        {"node_id": node_id, "subtree": subtree},
    )
    return {"ok": True, "subtree": subtree}


async def set_node_prop_for_agent(
    pocket_id: str,
    node_id: str,
    prop: str,
    value: Any,
) -> dict[str, Any]:
    """Set a single prop on a node."""
    result, err = await pockets_service.agent_set_node_prop(
        pocket_id, node_id=node_id, prop=prop, value=value
    )
    if err is not None or result is None:
        return {"ok": False, "error": err or "set_node_prop failed"}
    pocket_view = result.get("pocket") or {}
    subtree = result.get("subtree") or {}
    old_value = result.get("old_value")
    await _push_node_op(
        "node_prop_set",
        pocket_view,
        {"node_id": node_id, "prop": prop, "value": value, "subtree": subtree},
    )
    return {"ok": True, "subtree": subtree, "old_value": old_value}


async def set_prop_array_item_for_agent(
    pocket_id: str,
    node_id: str,
    prop: str,
    match: dict[str, Any],
    partial: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``partial`` into one matched item inside a node prop-array
    (chart.data, table.rows, …) without forcing the agent to re-ship the
    whole array. Locked to the ``prop_arrays`` allowlist at the service
    layer."""
    result, err = await pockets_service.agent_set_prop_array_item(
        pocket_id, node_id=node_id, prop=prop, match=match, partial=partial
    )
    if err is not None or result is None:
        return {"ok": False, "error": err or "set_prop_array_item failed"}
    pocket_view = result.get("pocket") or {}
    item_index = result.get("item_index")
    item = result.get("item")
    old_item = result.get("old_item")
    await _push_node_op(
        "node_prop_array_item_set",
        pocket_view,
        {
            "node_id": node_id,
            "prop": prop,
            "item_index": item_index,
            "item": item,
        },
    )
    return {
        "ok": True,
        "item_index": item_index,
        "item": item,
        "old_item": old_item,
    }


async def append_prop_array_item_for_agent(
    pocket_id: str,
    node_id: str,
    prop: str,
    value: Any,
    after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append ``value`` to a node prop-array, or insert immediately after
    a matched item when ``after`` is given. Creates the array if missing.
    Locked to the ``prop_arrays`` allowlist at the service layer."""
    result, err = await pockets_service.agent_append_prop_array_item(
        pocket_id, node_id=node_id, prop=prop, value=value, after=after
    )
    if err is not None or result is None:
        return {"ok": False, "error": err or "append_prop_array_item failed"}
    pocket_view = result.get("pocket") or {}
    item_index = result.get("item_index")
    item = result.get("item")
    await _push_node_op(
        "node_prop_array_item_appended",
        pocket_view,
        {
            "node_id": node_id,
            "prop": prop,
            "item_index": item_index,
            "item": item,
        },
    )
    return {"ok": True, "item_index": item_index, "item": item}


async def remove_prop_array_item_for_agent(
    pocket_id: str,
    node_id: str,
    prop: str,
    match: dict[str, Any],
) -> dict[str, Any]:
    """Remove the matched item from a node prop-array. Refuses ambiguous
    matches (the agent must disambiguate). Locked to the ``prop_arrays``
    allowlist at the service layer."""
    result, err = await pockets_service.agent_remove_prop_array_item(
        pocket_id, node_id=node_id, prop=prop, match=match
    )
    if err is not None or result is None:
        return {"ok": False, "error": err or "remove_prop_array_item failed"}
    pocket_view = result.get("pocket") or {}
    removed_index = result.get("removed_index")
    removed_item = result.get("removed_item")
    await _push_node_op(
        "node_prop_array_item_removed",
        pocket_view,
        {
            "node_id": node_id,
            "prop": prop,
            "removed_index": removed_index,
            "removed_item": removed_item,
        },
    )
    return {
        "ok": True,
        "removed_index": removed_index,
        "removed_item": removed_item,
    }


async def move_node_for_agent(
    pocket_id: str,
    node_id: str,
    new_parent_id: str,
    after_id: str | None = None,
) -> dict[str, Any]:
    """Move a subtree under a new parent."""
    result, err = await pockets_service.agent_move_node(
        pocket_id,
        node_id=node_id,
        new_parent_id=new_parent_id,
        after_id=after_id,
    )
    if err is not None or result is None:
        return {"ok": False, "error": err or "move_node failed"}
    pocket_view = result.get("pocket") or {}
    subtree = result.get("subtree") or {}
    await _push_node_op(
        "node_moved",
        pocket_view,
        {
            "node_id": node_id,
            "new_parent_id": new_parent_id,
            "after_id": after_id,
            "subtree": subtree,
        },
    )
    return {"ok": True, "subtree": subtree}


async def remove_node_for_agent(pocket_id: str, node_id: str) -> dict[str, Any]:
    """Remove a subtree by id."""
    result, err = await pockets_service.agent_remove_node(pocket_id, node_id=node_id)
    if err is not None or result is None:
        return {"ok": False, "error": err or "remove_node failed"}
    pocket_view = result.get("pocket") or {}
    await _push_node_op(
        "node_removed",
        pocket_view,
        {
            "node_id": node_id,
            "parent_id": result.get("parent_id"),
            "index": result.get("index"),
        },
    )
    return {"ok": True, "removed_id": node_id}


# ---------------------------------------------------------------------------
# Granular rippleSpec.state mutations (the "data" surface)
# ---------------------------------------------------------------------------


async def _push_state_op(action: str, pocket_view: dict[str, Any], payload: dict[str, Any]) -> None:
    """Push a granular ``pocket_mutation`` SSE event for one of the four
    state-level ops (``state_set`` / ``state_appended`` / ``state_removed`` /
    ``state_patched``). Mirrors ``_push_node_op``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import push_pocket_mutation

        push_pocket_mutation(
            {
                "action": action,
                "pocket_id": pocket_view.get("_id", ""),
                **payload,
            }
        )
    except Exception:
        logger.debug("push_pocket_mutation(%s) failed (non-fatal)", action, exc_info=True)


async def set_state_for_agent(pocket_id: str, path: str, value: Any) -> dict[str, Any]:
    """Write a single state value at ``path``."""
    result, err = await pockets_service.agent_set_state(pocket_id, path=path, value=value)
    if err is not None or result is None:
        return {"ok": False, "error": err or "set_state failed"}
    pocket_view = result.get("pocket") or {}
    await _push_state_op(
        "state_set",
        pocket_view,
        {"path": path, "value": value, "old_value": result.get("old_value")},
    )
    return {"ok": True, "old_value": result.get("old_value")}


async def append_state_for_agent(pocket_id: str, path: str, item: Any) -> dict[str, Any]:
    """Append ``item`` to the array at ``path``."""
    result, err = await pockets_service.agent_append_state(pocket_id, path=path, item=item)
    if err is not None or result is None:
        return {"ok": False, "error": err or "append_state failed"}
    pocket_view = result.get("pocket") or {}
    await _push_state_op(
        "state_appended",
        pocket_view,
        {"path": path, "item": item, "new_length": result.get("new_length")},
    )
    return {"ok": True, "new_length": result.get("new_length")}


async def remove_state_for_agent(pocket_id: str, path: str) -> dict[str, Any]:
    """Remove the value at ``path``."""
    result, err = await pockets_service.agent_remove_state(pocket_id, path=path)
    if err is not None or result is None:
        return {"ok": False, "error": err or "remove_state failed"}
    pocket_view = result.get("pocket") or {}
    await _push_state_op(
        "state_removed",
        pocket_view,
        {"path": path, "removed": result.get("removed")},
    )
    return {"ok": True, "removed": result.get("removed")}


async def patch_state_for_agent(pocket_id: str, partial: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge a partial dict into state's top level."""
    result, err = await pockets_service.agent_patch_state(pocket_id, partial=partial)
    if err is not None or result is None:
        return {"ok": False, "error": err or "patch_state failed"}
    pocket_view = result.get("pocket") or {}
    await _push_state_op(
        "state_patched",
        pocket_view,
        {"partial": partial, "previous": result.get("previous")},
    )
    return {"ok": True, "previous": result.get("previous")}


# ---------------------------------------------------------------------------
# rippleSpec.sources mutations — the read-only data-binding surface (RFC 04)
# ---------------------------------------------------------------------------
#
# A ``sources`` declaration is part of the persisted pocket document, so
# unlike the granular node / state ops there is no in-place frontend op for
# it — these wrappers push a full-document ``replace`` ``pocket_mutation``
# via ``_push_replace``. The frontend's ``applyMutation`` already handles
# ``replace``; no paw-enterprise change is needed for this fix.


async def set_source_for_agent(
    pocket_id: str, source_key: str, binding: dict[str, Any]
) -> dict[str, Any]:
    """Declare (or replace) a read-only data source on the pocket."""
    result, err = await pockets_service.agent_set_source(
        pocket_id, source_key=source_key, binding=binding
    )
    if err is not None or result is None:
        return {"ok": False, "error": err or "set_source failed"}
    pocket_view: dict[str, Any] = result.get("pocket") or {}
    await _push_replace(pocket_view)
    return {"ok": True, "source_key": source_key, "binding": result.get("binding")}


async def remove_source_for_agent(pocket_id: str, source_key: str) -> dict[str, Any]:
    """Remove a read-only data source declaration from the pocket."""
    result, err = await pockets_service.agent_remove_source(pocket_id, source_key=source_key)
    if err is not None or result is None:
        return {"ok": False, "error": err or "remove_source failed"}
    pocket_view: dict[str, Any] = result.get("pocket") or {}
    await _push_replace(pocket_view)
    return {"ok": True, "source_key": source_key}


__all__ = [
    "add_node_for_agent",
    "add_widget_for_agent",
    "append_prop_array_item_for_agent",
    "append_state_for_agent",
    "create_pocket_for_agent",
    "fetch_pocket_for_agent",
    "list_pockets_for_agent",
    "move_node_for_agent",
    "patch_state_for_agent",
    "remove_node_for_agent",
    "remove_prop_array_item_for_agent",
    "remove_source_for_agent",
    "remove_state_for_agent",
    "remove_widget_for_agent",
    "replace_node_for_agent",
    "set_node_prop_for_agent",
    "set_prop_array_item_for_agent",
    "set_source_for_agent",
    "set_state_for_agent",
    "update_pocket_for_agent",
    "update_widget_for_agent",
]
