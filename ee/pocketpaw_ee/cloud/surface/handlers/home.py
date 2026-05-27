# home.py — Home-surface preamble.
#
# Created: 2026-05-24 — Replaces the client-side ``home-context.ts``
# snapshot pattern. Reads the user's home pocket via
# ``pockets_service.ensure_home_pocket`` (idempotent — provisions on
# first call) and renders the pinned widgets, a tiny live snapshot, the
# list of available data tools, and recent activity. Total preamble is
# capped at ~1500 chars to keep token spend in check.
#
# All reads respect ``workspace_id`` (the pocket service enforces it
# per the tenancy-on-every-read rule). Audit reads use the canonical
# ``audit_service.agent_list_audit`` which also enforces tenancy.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import (
    composio_tool_names,
    format_widget_line,
    truncate_preamble,
)

logger = logging.getLogger(__name__)

# How many widgets to list before we collapse the tail into a count.
WIDGET_LIST_LIMIT = 12
# How many audit entries to surface.
ACTIVITY_LIMIT = 5


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Build the home-surface preamble — pinned widgets, snapshot, tools."""
    widgets_block = await _build_widgets_block(workspace_id, user_id)
    snapshot_line = _build_snapshot_line(widgets_block["widget_count"])
    tools_line = await _build_tools_line()
    activity_block = await _build_activity_block(workspace_id, user_id)

    parts = [
        '<surface kind="home" route="/" />',
        widgets_block["text"],
        f"<live-snapshot>{snapshot_line}</live-snapshot>",
    ]
    if tools_line:
        parts.append(f"<available-data-tools>{tools_line}</available-data-tools>")
    if activity_block:
        parts.append(activity_block)
    return truncate_preamble("\n".join(parts))


async def _build_widgets_block(workspace_id: str, user_id: str) -> dict:
    """Resolve the home pocket and render its widget list.

    On failure (missing pocket, fetch error) we still return a usable
    block so the rest of the preamble keeps its shape — empty workspaces
    just see a zero-count tag.
    """
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket, _created = await pockets_service.ensure_home_pocket(workspace_id, user_id)
    except Exception:
        logger.debug("home_handler: ensure_home_pocket failed", exc_info=True)
        return {
            "text": '<pinned-widgets count="0">(home pocket unavailable)</pinned-widgets>',
            "widget_count": 0,
        }

    widgets = pocket.get("widgets", []) or []
    total = len(widgets)
    if total == 0:
        return {
            "text": '<pinned-widgets count="0">(empty — no widgets pinned yet)</pinned-widgets>',
            "widget_count": 0,
        }

    # Use the helper to format each row — duck-typed against either dict
    # or domain Widget. The widget dicts coming off ``_resolved_wire_dict``
    # carry the same field names so attribute-access via a thin wrapper
    # gets the marker logic for free.
    rows = [format_widget_line(_AttrDict(w)) for w in widgets[:WIDGET_LIST_LIMIT]]
    if total > WIDGET_LIST_LIMIT:
        rows.append(f"... (+{total - WIDGET_LIST_LIMIT} more)")

    body = "\n".join(rows)
    return {
        "text": f'<pinned-widgets count="{total}">\n{body}\n</pinned-widgets>',
        "widget_count": total,
    }


class _AttrDict:
    """Tiny attr-access wrapper so dict rows feed into ``format_widget_line``.

    The widget objects on the home pocket wire dict are plain dicts (camel-
    or snake-cased depending on which service path produced them). The
    helper expects attribute access — wrap once here instead of patching
    the helper.
    """

    def __init__(self, source: dict) -> None:
        self._source = source

    def __getattr__(self, key: str):
        return self._source.get(key)


def _build_snapshot_line(widget_count: int) -> str:
    """One-line live snapshot.

    Kept intentionally tiny — we don't have a unified workspace-wide
    metrics view yet. The widget count is the only live number we have
    on hand without an extra round-trip; future iterations can fold in
    mission_control counts once the wiring stabilizes.
    """
    return f"home.pinned_widgets={widget_count}"


async def _build_tools_line() -> str:
    """Comma-joined list of data-tool names the agent can reach."""
    names = ["WebSearch", "WebFetch"]
    composio = await composio_tool_names()
    names.extend(composio)
    return " · ".join(names)


async def _build_activity_block(workspace_id: str, user_id: str) -> str:
    """Render the last few audit entries as a compact list.

    Audit reads go through the canonical service which carries the
    tenancy filter; on failure we omit the block entirely (the agent
    doesn't need to know audit is down).
    """
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
        from pocketpaw_ee.cloud.audit import service as audit_service

        ctx = RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="surface-home",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )
        resp = await audit_service.agent_list_audit(ctx, {"limit": ACTIVITY_LIMIT})
    except Exception:
        logger.debug("home_handler: audit lookup failed", exc_info=True)
        return ""

    entries = list(getattr(resp, "entries", []) or [])
    if not entries:
        return ""
    rows = [f"- {e.timestamp}: {e.actor} {e.action}" for e in entries[:ACTIVITY_LIMIT]]
    body = "\n".join(rows)
    return f'<recent-activity count="{len(rows)}">\n{body}\n</recent-activity>'


__all__ = ["build_preamble"]
