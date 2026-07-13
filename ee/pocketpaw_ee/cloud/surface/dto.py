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
#
# Updated: 2026-06-04 (feat/sites-refine-surface) — mirror ``SurfaceMeta``'s
# new ``site_id`` hint so the /sites/[siteId] refine chat can stamp the
# published site id on the wire and the sites handler can branch to refine.
# Updated: 2026-06-04 (feat/sites-svelte-engine) — mirror ``SurfaceMeta``'s new
# ``engine`` hint so the /sites create UI's "Use Svelte pages" toggle can stamp
# ``engine="svelte"`` on the wire and the sites handler routes the create
# preamble to the svelte-track skill instead of the ripple/default one.
# Updated: 2026-06-10 (feat/belt-console-backend, SC-1) — mirror ``SurfaceMeta``'s
# new ``repo`` + ``base_branch`` Belt console hints so the /belt page can stamp
# the bound repo + branch on the wire and the belt handler injects them into the
# preamble (agent stops asking for the repo).

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
    # Sites hint — mirror SurfaceMeta. Set by the /sites/[siteId] refine chat
    # alongside pocket_id so the handler refines the existing site. Optional.
    site_id: str | None = None
    # Sites create hint — mirror SurfaceMeta. Set by the /sites create UI's
    # "Use Svelte pages" toggle: "ripple" (default) | "svelte". Selects which
    # create-site authoring skill the preamble prefers. Optional.
    engine: str | None = None
    # Belt console hints — mirror SurfaceMeta. Set by the /belt page once the
    # user has bound a repo + branch for the run. ``repo`` is the absolute repo
    # path; ``base_branch`` is the branch to base the change off. Optional.
    repo: str | None = None
    base_branch: str | None = None
    # Code surface hints — mirror SurfaceMeta. Set by the /code page's
    # SurfaceMetaProvider. ``current_dir`` is the working directory;
    # ``project_name`` is the project name; ``storage_root`` is the cloud
    # storage key prefix; ``is_cloud_storage`` is ``"true"`` for S3-only
    # projects (no local filesystem path). ``workspace_vm`` is ``"true"``
    # when the project runs inside a shared Daytona sandbox. All optional.
    current_dir: str | None = None
    project_name: str | None = None
    storage_root: str | None = None
    is_cloud_storage: str | None = None
    workspace_vm: str | None = None


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
