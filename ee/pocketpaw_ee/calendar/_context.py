# Calendar module — RequestContext value object.
# Created: 2026-05-19 (feat/calendar-module).
#
# Tiny carrier struct so service functions can take a single ctx parameter
# (per ee/cloud convention rule 5). The cloud module doesn't expose a shared
# RequestContext type yet — when it does, swap this for the canonical one.
# Kept private (underscore prefix) so the router stays the single mounting
# point for callers outside this module.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RequestContext(BaseModel):
    """Per-request tenancy + actor envelope.

    Carries the workspace_id (multi-tenant filter) and user_id (actor for
    audit + access checks) into service functions. Frozen so handlers can
    pass it around without worrying about mutation.
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    user_id: str
