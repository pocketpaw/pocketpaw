# Connectors — domain value objects.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation. Frozen
# dataclasses constructed from Beanie docs in service.py. Tenancy is
# required at construction (workspace_id has no default) per the
# ee/cloud rule §3.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class WorkspaceConnector:
    """One connector enabled for one workspace.

    Constructed by ``service.py`` from the matching Beanie document plus
    the registry definition (display_name / type / icon come from the
    static registry, the rest from Mongo). Consumers outside the service
    only ever see this domain object.
    """

    name: str
    workspace_id: str
    display_name: str
    type: str  # "knowledge" | "data" | "communication" | …
    icon: str
    enabled: bool
    scope: str  # "pocket" | "workspace" | "user"
    pocket_id: str | None
    user_id: str | None
    config: tuple[tuple[str, Any], ...]  # frozen view of the config dict
    last_sync_at: datetime | None
    last_sync_status: str
    last_sync_error: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AvailableConnector:
    """A connector definition exposed by the registry, not yet enabled.

    The ``GET /connectors`` route returns a merge of these (the catalog)
    and ``WorkspaceConnector`` instances (the workspace's selections).
    """

    name: str
    display_name: str
    type: str
    icon: str
    auth_method: str
    actions: tuple[str, ...] = field(default_factory=tuple)
