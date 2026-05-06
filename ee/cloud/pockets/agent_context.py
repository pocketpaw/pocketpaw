"""Agent-facing pocket helpers — back the in-process MCP write tools the
cloud SSE chat agent uses to read/write the pocket it lives inside.

This module is a thin MCP-shape wrapper around ``pockets.service``:
it formats the ``{ok, error}`` returns the SDK expects, looks up
workspace/user/session identity from the per-stream ``ContextVar``s,
and pushes ``pocket_mutation`` / ``pocket_created`` events onto the
active SSE stream's queue. All Beanie reads/writes live in the
service modules.
"""

from __future__ import annotations

import logging
from typing import Any

from ee.cloud.pockets import service as pockets_service

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
    from ee.cloud.chat.agent_service import current_user_id, current_workspace_id
    from ee.cloud.pockets import service as pockets_service

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


async def _validate_ripple_spec(ripple_spec: dict[str, Any] | None) -> None:
    """Pre-persist guard against agent prop-name drift.

    Fetches the same manifest the ``get_widget_spec`` MCP tool uses,
    walks the rippleSpec tree, and (a) auto-rewrites known inner-item
    aliases (e.g. ``feed.items[].title`` -> ``text``) and (b) logs every
    mismatch at WARN level so we can spot new drift in production.

    Best-effort — if the manifest is unavailable, the spec passes
    through unchanged. Mutates ``ripple_spec`` in place when aliases
    apply.
    """
    if not isinstance(ripple_spec, dict):
        return
    try:
        from ee.ripple.manifest import get_manifest, validate_against_manifest
        from pocketpaw.config import get_settings

        settings = get_settings()
        manifest = await get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
        if manifest is None:
            return
        issues = validate_against_manifest(ripple_spec, manifest, apply_aliases=True)
        for issue in issues:
            logger.warning(
                "ripple manifest drift: %s (%s) unknown=%s item_issues=%s",
                issue["path"],
                issue["type"],
                issue["unknown_props"],
                issue["item_issues"],
            )
    except Exception:
        logger.debug("ripple manifest validation skipped (non-fatal)", exc_info=True)


def _push_replace(pocket_view: dict[str, Any]) -> None:
    """Push a ``pocket_mutation`` event for a successful update / widget
    change. Imported lazily so the cloud-chat dependency stays optional
    for callers that never go through the SSE path."""
    try:
        from ee.cloud.chat.agent_service import push_pocket_mutation

        push_pocket_mutation(
            {
                "action": "replace",
                "pocket_id": pocket_view.get("_id", ""),
                "pocket": pocket_view,
            }
        )
    except Exception:
        logger.debug("push_pocket_mutation failed (non-fatal)", exc_info=True)


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
    await _validate_ripple_spec(ripple_spec)
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
    _push_replace(view)
    return {"ok": True, "pocket": view}


async def add_widget_for_agent(pocket_id: str, widget: dict[str, Any]) -> dict[str, Any]:
    """Append a widget to the pocket's embedded widget list."""
    view, err = await pockets_service.agent_add_widget(pocket_id, widget)
    if err is not None:
        return {"ok": False, "error": err}
    _push_replace(view)
    return {"ok": True, "pocket": view}


async def update_widget_for_agent(
    pocket_id: str, widget_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a single embedded widget."""
    view, err = await pockets_service.agent_update_widget(pocket_id, widget_id, fields)
    if err is not None:
        return {"ok": False, "error": err}
    _push_replace(view)
    return {"ok": True, "pocket": view}


async def remove_widget_for_agent(pocket_id: str, widget_id: str) -> dict[str, Any]:
    """Remove a widget from the pocket's embedded widget list."""
    view, err = await pockets_service.agent_remove_widget(pocket_id, widget_id)
    if err is not None:
        return {"ok": False, "error": err}
    _push_replace(view)
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
    from ee.cloud.chat.agent_service import (
        current_session_mongo_id,
        current_user_id,
        current_workspace_id,
        push_sse_event,
    )
    from ee.cloud.sessions import service as sessions_service

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

    await _validate_ripple_spec(ripple_spec)
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
                from ee.cloud.realtime.emit import emit
                from ee.cloud.realtime.events import SessionUpdated

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
                logger.debug(
                    "SessionUpdated emit after pocket-link failed", exc_info=True
                )

    # Push a ``pocket_created`` SSE event onto the active stream so the
    # frontend mounts the new pocket without waiting for a sidebar refresh.
    try:
        push_sse_event(
            "pocket_created",
            {
                "pocket_id": pocket_id,
                "pocket": view,
                "session_id": session_mongo_id,
            },
        )
    except Exception:
        logger.debug("push_sse_event(pocket_created) failed", exc_info=True)

    return {"ok": True, "pocket": view}


__all__ = [
    "add_widget_for_agent",
    "create_pocket_for_agent",
    "fetch_pocket_for_agent",
    "list_pockets_for_agent",
    "remove_widget_for_agent",
    "update_pocket_for_agent",
    "update_widget_for_agent",
]
