"""BuiltInWidget document — system-level widget definitions seeded into every
new user's home pocket.

These are the "built-in" widgets the frontend's AddWidgetPicker "Built-in"
section and the home page's first-provision seed render. Storing them in a
dedicated system collection means they come from the database (not hardcoded
in frontend JS), are versionable via the same branching primitive, and an
operator can add/remove built-in widgets without a frontend deploy.

Created: 2026-07-13 (feat/builtin-widgets-from-db) — extracted from the
previously hardcoded BUILT_IN_WIDGETS array in AddWidgetPicker.svelte,
DEFAULT_MISSION_WIDGETS in +page.svelte, and widget-data.ts placeholder
entries.
"""

from __future__ import annotations

from beanie import Document, Indexed
from pydantic import BaseModel, Field


class BuiltInWidgetPosition(BaseModel):
    """Grid position hint for the home widget grid."""

    row: int = 0
    col: int = 0
    w: int = 1
    h: int = 1


class BuiltInWidget(Document):
    """A system-level built-in widget definition.

    Every entry is keyed on a unique ``slug`` (kebab-case, e.g.
    ``"mission-tray"``). The slug is the canonical id — the name, icon,
    color, and grid position are the renderer's display contract. The
    frontend's NATIVE_WIDGETS registry (HomeWidgetGrid.svelte) resolves a
    widget to its Svelte component by ``widget.name``, so the name here
    MUST match that registry exactly.

    ``pocket_name`` is the subtitle shown on the home widget tile.

    ``widget_type`` is always ``"native"`` for built-in tiles — they
    render as live Svelte components, not Ripple specs.

    ``auto_seed`` controls whether this widget is pre-pinned onto a
    brand-new home pocket during ``ensure_home_pocket``. Only widgets that
    every user should see by default (like Intent of the Day) should be
    auto-seeded; the rest remain discoverable in the AddWidgetPicker
    "Built-in" section for the user to pin manually.

    Collection: ``builtin_widgets``
    """

    slug: Indexed(str, unique=True)  # type: ignore[valid-type]
    name: str
    widget_type: str = Field(default="native", alias="type")
    icon: str = Field(default="activity")
    color: str = Field(default="#0A84FF")
    pocket_name: str = Field(default="Mission Control")
    position: BuiltInWidgetPosition = Field(default_factory=BuiltInWidgetPosition)
    # Whether this widget is active (shown in picker + seeded on provision).
    # Soft-delete for operators — never hard-delete a row a provisioned
    # pocket may reference.
    enabled: bool = Field(default=True)
    # When True, this widget is pre-pinned onto every brand-new home pocket
    # during first provision. When False, it only appears in the
    # AddWidgetPicker "Built-in" section for the user to pin manually.
    auto_seed: bool = Field(default=False)
    # Sort order in the picker UI (lower = first).
    sort_order: int = Field(default=0)

    class Settings:
        name = "builtin_widgets"
        validate_on_save = True


__all__ = ["BuiltInWidget", "BuiltInWidgetPosition"]
