# service.py — Surface context resolver and handler dispatch.
#
# Created: 2026-05-24 — ``resolve_surface_context(workspace_id, user_id,
# body)`` validates the client's ``{surface, meta}`` hint, maps the
# string to a ``SurfaceKind``, and dispatches to a per-kind handler that
# builds the preamble. Per Rule 5 of the cloud entity rules, this is a
# module-level async function (not a class) and validation runs at entry
# via ``SurfaceRequest.model_validate``.
#
# Failure stays inert: any handler error logs and returns a
# ``GENERIC`` context with empty preamble. The chat path is the consumer
# — never let a surface failure break a chat send.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.surface.domain import SurfaceContext, SurfaceKind, SurfaceMeta
from pocketpaw_ee.cloud.surface.dto import SurfaceMetaRequest, SurfaceRequest

logger = logging.getLogger(__name__)


# Handler registry: SurfaceKind -> async callable returning the preamble.
# Built lazily on first use so import-time failures in a handler module
# don't block the rest of the resolver.
_HANDLERS: dict[SurfaceKind, Any] | None = None


def _load_handlers() -> dict[SurfaceKind, Any]:
    """Lazy import every per-kind handler. Missing handlers are skipped.

    We tolerate missing handler modules instead of raising at import time
    because the surface module ships independently of the surfaces it
    knows about — a fresh deploy that drops a handler module shouldn't
    take the whole chat path down.
    """
    from pocketpaw_ee.cloud.surface.handlers import (
        activity,
        audit,
        calendar,
        files,
        foresight as foresight_handler,
        generic,
        home,
        knowledge,
        mission_control,
        pocket,
        pocket_widget,
        pockets_list,
        quickask,
        settings,
        sidepanel,
    )
    from pocketpaw_ee.cloud.surface.handlers import (
        agent as agent_handler,
    )
    from pocketpaw_ee.cloud.surface.handlers import (
        agents as agents_handler,
    )
    from pocketpaw_ee.cloud.surface.handlers import (
        chat as chat_handler,
    )

    return {
        SurfaceKind.HOME: home.build_preamble,
        SurfaceKind.POCKETS_LIST: pockets_list.build_preamble,
        SurfaceKind.POCKET: pocket.build_preamble,
        SurfaceKind.POCKET_WIDGET: pocket_widget.build_preamble,
        SurfaceKind.MISSION_CONTROL: mission_control.build_preamble,
        SurfaceKind.FILES: files.build_preamble,
        SurfaceKind.AUDIT: audit.build_preamble,
        SurfaceKind.ACTIVITY: activity.build_preamble,
        SurfaceKind.AGENTS: agents_handler.build_preamble,
        SurfaceKind.AGENT: agent_handler.build_preamble,
        SurfaceKind.KNOWLEDGE: knowledge.build_preamble,
        SurfaceKind.CALENDAR: calendar.build_preamble,
        SurfaceKind.CHAT: chat_handler.build_preamble,
        SurfaceKind.QUICKASK: quickask.build_preamble,
        SurfaceKind.SETTINGS: settings.build_preamble,
        SurfaceKind.SIDEPANEL: sidepanel.build_preamble,
        SurfaceKind.FORESIGHT: foresight_handler.build_preamble,
        SurfaceKind.GENERIC: generic.build_preamble,
    }


def _resolve_kind(value: str | None) -> SurfaceKind:
    """Map an inbound string to a ``SurfaceKind``. Unknown -> ``GENERIC``.

    Stay liberal in what we accept (clients can ship a new surface name
    before the backend ships its handler) and conservative in what we
    emit (the agent always gets a usable preamble).
    """
    if value is None:
        return SurfaceKind.GENERIC
    try:
        return SurfaceKind(value)
    except ValueError:
        logger.debug("unknown surface kind %r — falling back to GENERIC", value)
        return SurfaceKind.GENERIC


def _meta_from_request(req: SurfaceMetaRequest) -> SurfaceMeta:
    """Pydantic -> domain meta. Trivial pass-through."""
    return SurfaceMeta(
        pocket_id=req.pocket_id,
        widget_id=req.widget_id,
        focus_node_id=req.focus_node_id,
        agent_id=req.agent_id,
        file_id=req.file_id,
        route_path=req.route_path,
        run_id=req.run_id,
        scenario_id=req.scenario_id,
        panel=req.panel,
    )


async def resolve_surface_context(
    workspace_id: str, user_id: str, body: dict[str, Any] | SurfaceRequest | None
) -> SurfaceContext:
    """Resolve a client's surface hint into a rendered ``SurfaceContext``.

    Always returns a context — never raises. Errors are absorbed:

      * Invalid body shape (wrong fields, bad types) -> ``GENERIC`` with
        empty preamble.
      * Unknown surface kind -> ``GENERIC`` (still gets a tiny preamble).
      * Handler raised -> ``GENERIC`` with empty preamble; the error is
        logged at ``exception`` so it's discoverable but doesn't break
        the chat send.

    The dispatcher passes the validated meta and the tenancy tuple to
    every handler so individual handlers don't have to re-derive them.
    """
    global _HANDLERS

    # Step 1: validate the body. Bad input is logged at debug and the
    # caller gets a GENERIC context with empty preamble.
    try:
        validated = SurfaceRequest.model_validate(body or {})
    except Exception:
        logger.debug("surface body failed validation; using GENERIC", exc_info=True)
        return SurfaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=SurfaceKind.GENERIC,
            meta=SurfaceMeta(),
            preamble="",
        )

    kind = _resolve_kind(validated.surface)
    meta = _meta_from_request(validated.meta)

    # Step 2: lazy-load the handler registry. Import errors here propagate
    # because they indicate a broken deploy — surface a clear failure
    # rather than silently swallowing every surface preamble.
    if _HANDLERS is None:
        _HANDLERS = _load_handlers()
    handler = _HANDLERS.get(kind)
    if handler is None:
        # Resolver has a SurfaceKind without a handler. Treat the same as
        # an unknown surface — graceful GENERIC fall-back, no crash.
        logger.warning("no handler registered for surface kind %s", kind.value)
        return SurfaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=SurfaceKind.GENERIC,
            meta=meta,
            preamble="",
        )

    # Step 3: render the preamble. Handler exceptions are absorbed —
    # we'd rather ship a chat with no surface context than fail the send.
    try:
        preamble = await handler(workspace_id, user_id, meta)
    except Exception:
        logger.exception("surface handler %s failed; using GENERIC preamble", kind.value)
        return SurfaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=SurfaceKind.GENERIC,
            meta=meta,
            preamble="",
        )

    return SurfaceContext(
        workspace_id=workspace_id,
        user_id=user_id,
        kind=kind,
        meta=meta,
        preamble=preamble or "",
    )


__all__ = ["resolve_surface_context"]
