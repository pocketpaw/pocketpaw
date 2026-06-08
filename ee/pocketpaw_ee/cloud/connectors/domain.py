# Connectors — domain value objects.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation. Frozen
# dataclasses constructed from Beanie docs in service.py. Tenancy is
# required at construction (workspace_id has no default) per the
# ee/cloud rule §3.
# Updated: 2026-06-07 (M3 connector→skill auto-authoring) — ``AvailableConnector``
#   grows an optional ``surface_profile`` field carrying the connector's
#   skill/tool contribution (mirrors the OSS ``ConnectorSurfaceProfile``). The
#   pure derivation helper in ``derivation.py`` folds these across a pocket's
#   enabled connectors into a ``PocketSurfaceProfile``.
# Updated: 2026-06-08 (connector-mcp-execution / keystone) — added
#   ``ConnectorActionInfo`` + ``PocketConnectorInfo`` value objects so
#   ``service.list_pocket_connectors`` can hand the agent MCP server a
#   JSON-friendly view of each enabled pocket-scoped connector and its actions
#   (name + description + trust level + read/write classification) without the
#   MCP layer importing Beanie or the OSS registry types.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ConnectorSurfaceContribution:
    """A connector's surface-profile contribution, in the cloud domain.

    JSON-friendly mirror of the OSS ``yaml_engine.ConnectorSurfaceProfile``
    (tuples instead of the raw YAML lists). Built in ``service.py`` from the
    registry definition and carried on ``AvailableConnector`` so the derivation
    helper never reaches across into the OSS registry types.
    """

    skill: str | None = None
    allow_tools: tuple[str, ...] = field(default_factory=tuple)
    deny_tools: tuple[str, ...] = field(default_factory=tuple)


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
    # M3 — the connector's surface-profile contribution (skill + tool patterns),
    # or ``None`` when the YAML has no ``surface_profile:`` block. Consumed by
    # the derivation helper when the connector is bound to a pocket.
    surface_profile: ConnectorSurfaceContribution | None = None


@dataclass(frozen=True)
class ConnectorActionInfo:
    """One action on a connector, classified for the agent's tool surface.

    Built in ``service.list_pocket_connectors`` from the adapter's
    ``ActionSchema``. ``is_read`` is the v1 gate: ``auto``-trust actions are
    read-first and the agent may execute them; ``confirm`` / ``restricted``
    actions are write-shaped and BLOCKED in v1 (listed, never executed).
    """

    name: str
    description: str
    trust_level: str  # "auto" | "confirm" | "restricted"
    execution_mode: str  # "cloud" | "local" | "sandbox"
    is_read: bool  # True when trust_level == "auto" — agent may execute


@dataclass(frozen=True)
class PocketConnectorInfo:
    """One enabled, pocket-scoped connector plus its actions.

    Returned by ``service.list_pocket_connectors`` for the agent MCP server.
    The MCP layer renders this straight to JSON — it never touches the Beanie
    doc or the OSS registry.
    """

    name: str
    display_name: str
    type: str
    icon: str
    actions: tuple[ConnectorActionInfo, ...] = field(default_factory=tuple)
