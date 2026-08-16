# Helper: ask a FastAPI app which route paths it actually serves.
# Created: 2026-08-16 (chore/refresh-python-deps).
#
# WHY THIS EXISTS. Four tests asked "is route X registered?" by reading
# ``r.path`` off ``app.routes``. FastAPI 0.141 changed what that list holds: a
# router mounted with ``include_router`` now appears as ONE private
# ``fastapi.routing._IncludedRouter`` object with no ``.path``, instead of its
# routes being flattened into the parent list. On the real dashboard app that
# took the readable set from 128 entries down to 14, so the assertions stopped
# finding routes that were mounted and serving perfectly well.
#
# Two of the four already guarded with ``hasattr(r, "path")``, which is why they
# failed as a missing route rather than an AttributeError — the quieter and more
# misleading of the two failure modes.
#
# ``app.openapi()`` is the public, documented way to enumerate an app's paths,
# it applies every ``include_router`` prefix for us, and it does not depend on
# the shape of a private class. Walking ``_IncludedRouter.original_router``
# ourselves would work today and re-break on the next FastAPI refactor.
#
# Limitation, stated rather than discovered later: OpenAPI omits routes excluded
# from the schema (``include_in_schema=False``) and non-HTTP routes such as
# WebSockets. Every current caller asserts on ordinary documented HTTP
# endpoints. A test that needs a hidden or WebSocket route must ask a different
# question — do not "fix" it by widening this.

from __future__ import annotations

from typing import Any


def registered_paths(app: Any) -> set[str]:
    """Every documented HTTP path *app* serves, with router prefixes applied."""
    return set(app.openapi().get("paths", {}))


def has_path(app: Any, fragment: str) -> bool:
    """True when any registered path contains *fragment*.

    Substring rather than equality because the callers spot-check a mount
    ("/api/v1/sessions" covers "/api/v1/sessions/{id}") rather than pin an
    exact route list.
    """
    return any(fragment in p for p in registered_paths(app))
