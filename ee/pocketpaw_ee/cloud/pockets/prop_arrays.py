# prop_arrays.py — closed allowlist of widget prop-arrays the Tier-2
# array-item edit ops are permitted to touch.
# Created: 2026-05-14. Reworked onto the pocketpaw_ee layout from PR #1106.
# Changes: 2026-05-21 — regenerated from the @ripple-ui/svelte widget
# manifest. Now covers every widget with an object-element array prop
# (63 widgets / 101 props, was 9). Fixes drift: the old hand-written
# table had `tabs.items` / `form-layout.fields`, but the manifest's real
# props are `tabs.tabs` / `form-layout.sections`; `feed` / `nav` no
# longer exist as widget types.
"""Closed allowlist of ``(widget_type, prop_name)`` pairs that support the
Tier-2 array-element ops (``set_prop_array_item`` /
``append_prop_array_item`` / ``remove_prop_array_item``).

What's enrolled: every widget prop in the renderer manifest whose type is
an array of OBJECTS — the row-style content collections an agent edits
item-by-item (``table.rows``, ``chart.data``, ``calendar.events``,
``checklist-layout.items``, ``kanban.columns`` …). Scalar arrays
(``chart.colors`` is ``string[]``, ``kbd.keys``, heatmap axis-label
lists) are deliberately excluded — there is no record to merge into.

Why a closed list rather than a live manifest lookup: ``is_allowed`` is a
hot, synchronous check; the manifest is fetched async with a TTL. A
static table avoids threading the manifest through every call site. The
cost is that this must be regenerated when the widget catalog changes.

Regenerating: for each widget in the ``@ripple-ui/svelte`` manifest,
enroll each prop whose ``type`` denotes an array of objects — the type
string contains ``Array<`` or ``[]`` AND ``{`` or ``Record<``.
"""

from __future__ import annotations

from typing import Final

_ALLOWED: Final[dict[str, frozenset[str]]] = {
    "accordion": frozenset({"items"}),
    "analytics-dashboard": frozenset({"secondaryMetrics", "topItems"}),
    "audit-log": frozenset({"entries"}),
    "avatar-group": frozenset({"users"}),
    "breadcrumb": frozenset({"items"}),
    "bulk-action-bar": frozenset({"actions"}),
    "c4": frozenset({"diagram"}),
    "calendar": frozenset({"events"}),
    "chart": frozenset({"data"}),
    "checklist-layout": frozenset({"items"}),
    "coachmark": frozenset({"steps"}),
    "combobox": frozenset({"options"}),
    "command-palette": frozenset({"commands"}),
    "comment-thread": frozenset({"comments"}),
    "comparison-layout": frozenset({"features", "items"}),
    "comparison-table": frozenset({"columns", "rows"}),
    "context-menu": frozenset({"items"}),
    "data-grid": frozenset({"columns", "rows"}),
    "definition-list": frozenset({"items"}),
    "dropdown-menu": frozenset({"items"}),
    "entity-detail": frozenset({"actions", "kpis", "meta", "tags"}),
    "exec-dashboard": frozenset({"actions", "activity", "kpis", "primaryChart", "table"}),
    "filter-bar": frozenset({"options"}),
    "form-layout": frozenset({"sections"}),
    "funnel": frozenset({"data"}),
    "gantt": frozenset({"tasks"}),
    "heatmap": frozenset({"cells"}),
    "invoice-layout": frozenset({"actions", "lines", "paymentMethods", "summary"}),
    "invoice-lines": frozenset({"lines", "summary"}),
    "kanban": frozenset({"columns", "value"}),
    "kv-table": frozenset({"rows"}),
    "map": frozenset({"markers", "paths", "polygons", "trackers"}),
    "master-detail": frozenset({"items"}),
    "multi-select": frozenset({"options"}),
    "notification-center": frozenset({"value"}),
    "ops-dashboard": frozenset({"deploys", "incidents", "metrics", "services"}),
    "order-status": frozenset({"actions", "events", "steps"}),
    "people-picker": frozenset({"people"}),
    "permission-matrix": frozenset({"permissions", "roles"}),
    "pipeline-dashboard": frozenset({"conversion", "deals", "funnel", "leaderboard", "ticker"}),
    "pricing-table": frozenset({"tiers"}),
    "project-dashboard": frozenset({"burndown", "meta", "milestones", "team", "updates"}),
    "radio-group": frozenset({"options"}),
    "report-layout": frozenset({"actions", "meta"}),
    "sankey": frozenset({"links", "nodes"}),
    "saved-views": frozenset({"views"}),
    "search": frozenset({"results"}),
    "segmented": frozenset({"options"}),
    "select": frozenset({"options"}),
    "settings-list": frozenset({"items"}),
    "sidebar": frozenset({"items"}),
    "sources-bar": frozenset({"sources"}),
    "steps": frozenset({"steps"}),
    "table": frozenset({"columns", "rows"}),
    "tabs": frozenset({"tabs"}),
    "terminal": frozenset({"lines"}),
    "ticker": frozenset({"items"}),
    "timeline": frozenset({"events"}),
    "tree": frozenset({"nodes"}),
    "tree-table": frozenset({"columns", "rows"}),
    "treemap": frozenset({"data"}),
    "wizard-layout": frozenset({"steps"}),
    "workflow": frozenset({"edges", "nodes"}),
}


def is_allowed(widget_type: str, prop: str) -> bool:
    """Return True iff ``prop`` is in the array-edit allowlist for ``widget_type``."""
    return prop in _ALLOWED.get(widget_type, frozenset())


def allowed_props_for(widget_type: str) -> tuple[str, ...]:
    """Return the sorted tuple of allowed prop names for ``widget_type``,
    or an empty tuple if unknown."""
    return tuple(sorted(_ALLOWED.get(widget_type, frozenset())))


__all__ = ["allowed_props_for", "is_allowed"]
