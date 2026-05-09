"""Settings resolution for the pocket specialist runtime.

Pure logic — no I/O, no side effects. Lets us test model fallback without
spinning up an actual backend.
"""

from __future__ import annotations

from pocketpaw.config import Settings

# Some backends' Settings fields don't follow the strict
# ``<backend_name>_model`` convention (e.g. agent_backend "claude_agent_sdk"
# is configured via ``claude_sdk_model``, not ``claude_agent_sdk_model``).
# When you change the registry, update this map too.
_BACKEND_MODEL_FIELD: dict[str, str] = {
    "claude_agent_sdk": "claude_sdk_model",
}


def resolve_specialist_model(settings: Settings) -> str:
    """Pick the model id for a specialist run.

    Order:
      1. ``settings.pocket_specialist_model`` if non-empty (explicit override).
      2. ``settings.<field>_model`` for the chosen backend, where ``<field>``
         is normally ``<backend>_model`` but may be remapped via
         ``_BACKEND_MODEL_FIELD`` for backends whose Settings field name
         doesn't match the backend name.
      3. Empty string when the backend has no ``*_model`` field — caller
         must fall back to the backend's own internal default.
    """
    explicit = settings.pocket_specialist_model
    if explicit:
        return explicit
    backend = settings.pocket_specialist_backend
    field_name = _BACKEND_MODEL_FIELD.get(backend, f"{backend}_model")
    return getattr(settings, field_name, "") or ""
