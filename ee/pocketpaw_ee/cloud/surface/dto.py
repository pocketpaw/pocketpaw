# dto.py — Wire schemas the client attaches to chat requests.
#
# Created: 2026-05-24 — The chat agent's per-turn context grows a
# {surface, surface_meta} hint. ``SurfaceMetaRequest`` mirrors
# ``SurfaceMeta`` for inbound validation; ``SurfaceRequest`` is the
# composite the client stamps. ``resolve_surface_context`` validates
# arbitrary inbound dicts through ``SurfaceRequest.model_validate``
# rather than trusting whatever the wire produced.
#
# Per the entity rules — DTOs separate input (Request) from any future
# response shape. There is no response DTO here because the surface
# context is consumed in-process by ``chat/agent_service`` (no HTTP
# round-trip).

from __future__ import annotations

from pydantic import BaseModel, Field


class SurfaceMetaRequest(BaseModel):
    """Inbound shape for the client's ``surface_meta`` hint.

    Mirror of the domain ``SurfaceMeta`` — every field optional. The
    handlers fetch heavy state server-side; this hint only carries
    cheap identifiers (which pocket is open, which widget is focused).
    """

    pocket_id: str | None = None
    widget_id: str | None = None
    focus_node_id: str | None = None
    agent_id: str | None = None
    file_id: str | None = None
    route_path: str | None = None
    # Foresight hints — mirror SurfaceMeta. Set by the paw-enterprise
    # sidebar's surface stamp on /foresight routes. All optional.
    run_id: str | None = None
    scenario_id: str | None = None
    panel: str | None = None


class SurfaceRequest(BaseModel):
    """The full ``{surface, meta}`` hint the client stamps on a chat send.

    Unknown ``surface`` strings (typos, future surfaces a client knows
    about but the backend doesn't yet) fall back to ``SurfaceKind.GENERIC``
    in the resolver — the schema deliberately accepts any string here so
    a client roll-out can ship the new surface name before the backend
    handler ships.
    """

    surface: str | None = None
    meta: SurfaceMetaRequest = Field(default_factory=SurfaceMetaRequest)


__all__ = ["SurfaceMetaRequest", "SurfaceRequest"]
