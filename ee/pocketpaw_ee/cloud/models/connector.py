# WorkspaceConnector Beanie document — per-workspace enabled flags + sync state.
# Created: 2026-05-03 — PR-1 of the Phase 1 connector consolidation.
# One row per (workspace, connector_name) tracking the enabled state, last
# sync timestamp, scope (pocket | workspace | user) the OAuth token was
# granted at, and a free-form config blob the connector adapter reads on
# execute. Token bytes themselves stay in src/pocketpaw/clients/
# token_store.py for Phase 1 — only the *reference* + scope live here.

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceConnector(TimestampedDocument):
    """One connector configuration for one workspace.

    Tenancy: ``workspace`` is required and indexed; every read in
    ``service.py`` filters on it. ``name`` is the registry key (e.g.
    ``"gmail"``, ``"stripe"``); paired with ``workspace`` it is unique
    per workspace, enforced at the service layer (no Mongo unique index
    yet so re-enabling is idempotent and forgiving on race conditions).

    The ``status`` field is intentionally minimal — adapter ``health()``
    output is computed live and merged at read time in ``service.list``.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    enabled: bool = True
    scope: str = "workspace"  # "pocket" | "workspace" | "user"
    pocket_id: str | None = None  # set when scope == "pocket"
    user_id: str | None = None  # set when scope == "user"
    config: dict[str, Any] = Field(default_factory=dict)
    last_sync_at: datetime | None = None
    last_sync_status: str = "never"  # "never" | "ok" | "error"
    last_sync_error: str = ""

    class Settings(TimestampedDocument.Settings):
        name = "workspace_connectors"
