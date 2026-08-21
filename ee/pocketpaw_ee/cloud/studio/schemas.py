# ee/pocketpaw_ee/cloud/studio/schemas.py — wire DTOs for the /studio surface.
#
# These shapes mirror paw-enterprise ``src/lib/core/studio/types.ts`` exactly —
# the frontend client (``core/studio/api.ts``) maps each endpoint's envelope
# straight onto its typed ``StudioModel`` / ``Generation`` / ``GenerateRequest``
# etc. Field names are camelCase on the wire (Pydantic serializes the aliases
# below), matching the TS interfaces.
#
# Created 2026-08-17 (studio-real-backend): new DTO module.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ── Model / style catalog ───────────────────────────────────────────────────


class StudioModel(BaseModel):
    """One selectable generation model in the composer picker."""

    id: str
    label: str
    kind: str  # "image" | "video"
    provider: str
    description: str | None = None
    thumbnailUrl: str | None = None
    aspectRatios: list[str]
    maxCount: int = 1
    supportsNegativePrompt: bool = False
    durationsSec: list[int] | None = None
    credits: int | None = None
    tags: list[str] = Field(default_factory=list)
    default: bool | None = None


class StudioStyle(BaseModel):
    """A one-tap style/template. ``promptSuffix`` is appended to the user's
    prompt so the effect stays transparent (same convention as the mock)."""

    id: str
    label: str
    description: str | None = None
    thumbnailUrl: str | None = None
    promptSuffix: str = ""


# ── Generation domain ───────────────────────────────────────────────────────


class GeneratedAsset(BaseModel):
    """One produced image/video file. ``url`` is a backend-relative path
    (``/api/v1/media/<name>``) that the frontend resolves via ``mediaUrl()``."""

    id: str
    url: str
    mime: str
    width: int | None = None
    height: int | None = None
    posterUrl: str | None = None


class GenerationParams(BaseModel):
    """The exact parameters a generation ran with — echoed back so the UI can
    label results and power one-tap remix."""

    kind: str
    model: str
    aspectRatio: str
    count: int
    styleId: str | None = None
    negativePrompt: str | None = None
    seed: int | None = None
    durationSec: int | None = None
    inputImageCount: int | None = None


class Generation(BaseModel):
    """One Generate click — status + (once ready) its assets. Async models sit
    in ``queued``/``running``; the direct studio path resolves image
    generations to ``succeeded`` synchronously."""

    id: str
    prompt: str
    status: str  # "queued" | "running" | "succeeded" | "failed"
    kind: str
    model: str
    params: GenerationParams
    assets: list[GeneratedAsset] = Field(default_factory=list)
    createdAt: int
    error: str | None = None
    sourceGenerationId: str | None = None


class GenerateRequest(BaseModel):
    """Body of ``POST /studio/generate``. ``prompt`` is the RAW user prompt
    (no style suffix); the backend re-applies style by ``styleId``. ``inputImageUrls``
    switches a video request to image-to-video — the flow's Video node sends the
    result URLs of the Image/Picture nodes wired into it, and EVERY image goes to
    the fal model in one call."""

    prompt: str
    kind: str = "image"
    model: str
    aspectRatio: str = "1:1"
    count: int = 1
    styleId: str | None = None
    negativePrompt: str | None = None
    seed: int | None = None
    durationSec: int | None = None
    referenceAssetUrl: str | None = None
    inputImageUrls: list[str] | None = None


class EditRequest(BaseModel):
    """Body of ``POST /studio/edit``. The studio canvas exposes inpaint /
    expand / upscale / variations / remove-bg; each op takes a source asset and
    returns a NEW generation."""

    op: str
    sourceUrl: str
    prompt: str | None = None
    maskDataUrl: str | None = None
    direction: str | None = None
    factor: float | None = None
    model: str | None = None


class PromptSuggestion(BaseModel):
    """Response of ``POST /studio/suggest-prompt``: a sentence in, an enriched
    prompt + the media kind it implies (powers the flow editor's Text node)."""

    prompt: str
    kind: str


class SuggestPromptRequest(BaseModel):
    """Body of ``POST /studio/suggest-prompt`` (the frontend sends ``{sentence}``)."""

    sentence: str


# ── Flow projects (persisted server-side, workspace-scoped) ─────────────────
# These mirror paw-enterprise ``core/studio/flow.svelte.ts``'s StudioFlowProject.
# ``data`` / ``position`` on a node are OPAQUE — the backend stores whatever the
# @xyflow/svelte canvas needs and never inspects it, so the node schema can grow
# without a backend change.


class FlowNode(BaseModel):
    """A @xyflow/svelte node snapshot (id, kind, position, and its data bag)."""

    id: str
    type: str
    position: dict[str, Any]
    data: dict[str, Any]


class FlowEdge(BaseModel):
    """A @xyflow/svelte edge snapshot (source → target handles)."""

    id: str
    source: str
    target: str
    sourceHandle: str | None = None
    targetHandle: str | None = None


class FlowProject(BaseModel):
    """A saved flow canvas — its name plus the full node/edge graph."""

    id: str
    name: str
    createdAt: int
    updatedAt: int
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)


class FlowProjectSave(BaseModel):
    """Body of ``PUT /studio/flow-projects/{id}`` — the full project state.
    The endpoint UPSERTS (creates the project if ``{id}`` is unknown), so the
    frontend can fire-and-forget a debounced save without tracking existence."""

    name: str | None = None
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)


# ── Envelopes ───────────────────────────────────────────────────────────────


class StudioModelsResponse(BaseModel):
    models: list[StudioModel]


class StudioStylesResponse(BaseModel):
    styles: list[StudioStyle]


class GenerationsResponse(BaseModel):
    generations: list[Generation]


class FlowProjectsResponse(BaseModel):
    """Response of ``GET /studio/flow-projects`` — every project in the
    workspace, most-recently-updated first."""

    projects: list[FlowProject]


# ── Media list (reuses the /media router's shape) ───────────────────────────


class MediaFile(BaseModel):
    name: str
    url: str
    mime: str
    size: int
    modified: int


class MediaListResponse(BaseModel):
    media: list[MediaFile]


__all__ = [
    "StudioModel",
    "StudioStyle",
    "GeneratedAsset",
    "GenerationParams",
    "Generation",
    "GenerateRequest",
    "EditRequest",
    "PromptSuggestion",
    "SuggestPromptRequest",
    "StudioModelsResponse",
    "StudioStylesResponse",
    "GenerationsResponse",
    "FlowNode",
    "FlowEdge",
    "FlowProject",
    "FlowProjectSave",
    "FlowProjectsResponse",
    "MediaFile",
    "MediaListResponse",
]
