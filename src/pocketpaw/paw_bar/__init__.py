# Paw Bar — embeddable customer-facing widget layer for Paw OS.
# Updated: 2026-07-08 — Renamed widget "Paw Print" → "Paw Bar" (module paw_print→paw_bar,
#   PawPrint*→PawBar*, /api/v1/paw-print→/paw-bar, X-Paw-Print-Token→X-Paw-Bar-Token,
#   tables/db paw_print→paw_bar). The separate one-word audit feed (the past-tense
#   record, spelled as one word) is a DIFFERENT feature and is left untouched.
# Created: 2026-04-13 (Move 3 PR-A) — The full-stack decision loop Palantir
# cannot offer: customer interactions on a Paw Bar widget flow back into
# a Pocket in real time, Instinct nudges the owner, approved actions feed
# back to the widget. This module is the backend side of that loop.
# Updated: 2026-06-10 (W0b security fix) — Export PawBarWidgetPublic, the
# token-free response projection used by the list/read endpoints.
# Updated: 2026-06-11 (gap2 — close the customer decision loop) — Export
# DecisionStatus + DecisionState, the deliverable that carries the owner's
# decision back out to the customer surface (the back-half of the loop).

from pocketpaw.paw_bar.models import (
    DecisionState,
    DecisionStatus,
    PawBarBlock,
    PawBarEvent,
    PawBarEventMapping,
    PawBarSpec,
    PawBarWidget,
    PawBarWidgetPublic,
)
from pocketpaw.paw_bar.store import PawBarStore

__all__ = [
    "DecisionState",
    "DecisionStatus",
    "PawBarBlock",
    "PawBarEvent",
    "PawBarEventMapping",
    "PawBarSpec",
    "PawBarStore",
    "PawBarWidget",
    "PawBarWidgetPublic",
]
