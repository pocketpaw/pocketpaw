# prop_arrays.py — closed allowlist of widget prop-arrays the Tier-2
# array-item edit ops are permitted to touch.
# Created: 2026-05-14. Reworked onto the pocketpaw_ee layout from PR #1106.
"""Closed allowlist of ``(widget_type, prop_name)`` pairs that support the
Tier-2 array-element ops (``set_prop_array_item`` /
``append_prop_array_item`` / ``remove_prop_array_item``).

Why a closed list:

  * The renderer's manifest already enumerates every prop a widget
    accepts, but only a small subset of those are user-editable
    arrays the agent should surgically poke at item-level. Many props
    happen to be lists (e.g. ``chart.colors``) but aren't intended
    for row-style edits.
  * Locking the surface here means a typo (``cloud_set_prop_array_item
    target=stat prop=value``) is rejected up-front with a clear error,
    rather than silently mangling a scalar prop.

Keep this in sync with:

  * ``backend/docs/plans/2026-05-12-pocket-edit-api-design.md`` (Tier 2 table)
  * the edit-specialist prompt examples in ``pocketpaw.ripple._pockets``

Adding a new widget? Add the row here AND extend the edit-specialist
prompt's cheatsheet so the agent knows the prop is targetable.
"""

from __future__ import annotations

from typing import Final

_ALLOWED: Final[dict[str, frozenset[str]]] = {
    "chart": frozenset({"data"}),
    "table": frozenset({"rows", "columns"}),
    "kanban": frozenset({"columns"}),
    "calendar": frozenset({"events"}),
    "feed": frozenset({"items"}),
    "tabs": frozenset({"items"}),
    "nav": frozenset({"items"}),
    "select": frozenset({"options"}),
    "form-layout": frozenset({"fields"}),
}


def is_allowed(widget_type: str, prop: str) -> bool:
    """Return True iff ``prop`` is in the array-edit allowlist for ``widget_type``."""
    return prop in _ALLOWED.get(widget_type, frozenset())


def allowed_props_for(widget_type: str) -> tuple[str, ...]:
    """Return the sorted tuple of allowed prop names for ``widget_type``,
    or an empty tuple if unknown."""
    return tuple(sorted(_ALLOWED.get(widget_type, frozenset())))


__all__ = ["allowed_props_for", "is_allowed"]
