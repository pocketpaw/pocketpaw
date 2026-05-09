"""Settings resolution for the pocket specialist runtime.

Pure logic — no I/O, no side effects. Lets us test model fallback without
spinning up an actual backend.
"""

from __future__ import annotations

from pocketpaw.config import Settings


def resolve_specialist_model(settings: Settings) -> str:
    """Pick the model id for a specialist run.

    Order:
      1. ``settings.pocket_specialist_model`` if non-empty (explicit override).
      2. ``settings.<backend>_model`` for the chosen backend (e.g. ``deep_agents_model``).
      3. Empty string when the backend has no ``*_model`` field — caller must
         fall back to the backend's own internal default.
    """
    explicit = settings.pocket_specialist_model
    if explicit:
        return explicit
    field_name = f"{settings.pocket_specialist_backend}_model"
    return getattr(settings, field_name, "") or ""
