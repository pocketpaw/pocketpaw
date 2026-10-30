# dto.py — Wire schemas the client attaches to chat requests.
#
# ``SurfaceMetaRequest`` mirrors ``SurfaceMeta`` field for field for inbound
# validation, and ``SurfaceRequest`` is the {surface, surface_meta} composite the
# client stamps. ``resolve_surface_context`` validates inbound dicts through
# ``SurfaceRequest.model_validate`` rather than trusting the wire. A hint added
# to ``SurfaceMeta`` must be mirrored here (and passed through in
# service._meta_from_request), or it validates away silently and the handler
# always sees ``None``.
#
# Request-only by design: the surface context is consumed in-process by
# chat/agent_service, so there is no response DTO.

from __future__ import annotations

from typing import Any

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
    brief_id: str | None = None
    # Sites refine hint — mirror SurfaceMeta. The Build/Chat toggle in the
    # /sites/[siteId] refine chat: "chat" answers with no mutation, "build" (the
    # default, preserving today's behavior) refines the site. Optional.
    mode: str | None = "build"
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
    # Concierge action registry hint (C1) — mirror SurfaceMeta. Set server-side by
    # ``concierge_chat`` from the widget spec (never by an untrusted client on the
    # public path), so the concierge run allow-lists + surfaces exactly this
    # widget's declared verbs. A JSON list of {verb, policy, args, label}.
    pawbar_actions: list[dict[str, Any]] | None = None
    # Concierge catalog hint (C1) — mirror SurfaceMeta. Set server-side from the
    # catalog store (the turn's retrieved items) so the preamble can name real
    # products.
    pawbar_catalog: list[dict[str, Any]] | None = None
    # Studio editor hint — mirror SurfaceMeta. The open timeline, projected to
    # ids + times. Carries state rather than an identifier because the document
    # lives in the browser and there is no server copy to fetch.
    timeline: dict[str, Any] | None = None
    # Craft studio hints — mirror SurfaceMeta.vector / .photo / .design.
    vector: dict[str, Any] | None = None
    photo: dict[str, Any] | None = None
    design: dict[str, Any] | None = None
    # Otherhand hints — mirror SurfaceMeta. Stamped by the /other-hand page on
    # every turn. ``snapshot_path`` is the absolute path the snapshot endpoint
    # returned for this page's PNG (the client echoes it back, it never invents
    # one); ``free_y`` is the y below which the page is empty, as a string to
    # match the other scalar hints. Both optional.
    snapshot_path: str | None = None
    free_y: str | None = None
    # Book mode (2026-08-26): the read-only source page beside the notebook.
    book_path: str | None = None
    # ``mark_box`` is "x1,y1,x2,y2" in the page's logical space: exactly where
    # the reader's pen went on the book. The client already knows this, so we
    # TELL the agent rather than make it re-derive a circled region from a
    # rasterised page of dense body text — which it does badly.
    mark_box: str | None = None
    # The marked region re-rendered at high resolution (scans, figures,
    # equations) and the exact words under the mark, read off the PDF's own
    # text layer (born-digital pages). Two channels because they fail in
    # different places: no text layer on a scan, no crop worth reading on a
    # pure-text page.
    mark_image_path: str | None = None
    mark_text: str | None = None
    # Compact JSON of what is already ON the page (text content with exact
    # coordinates, shape/user-ink bounding boxes), measured client-side from
    # the live stroke model AFTER the placement guard. The agent anchors its
    # annotations to these coordinates rather than to its memory of what it
    # emitted — which the guard may have shifted.
    scene: str | None = None


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
