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


class StudioModelParam(BaseModel):
    """One declarative per-model parameter knob (the edit composer renders a
    control for each). Mirrors paw-enterprise ``core/studio/types.ts``
    ``StudioModelParam`` — ``key``/``label``/``type``/``default`` plus the
    optional ``min``/``max``/``step``/``options``/``hint``/``advanced`` fields.
    ``type`` is one of ``stepper`` | ``text`` | ``select`` | ``slider`` |
    ``toggle``; ``default`` is the value the knob resets to."""

    key: str
    label: str
    type: str
    default: Any = None
    hint: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    step: int | float | None = None
    options: list[str] | None = None
    advanced: bool = False


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
    # Per-model declarative knobs the edit composer surfaces (empty for models
    # that expose no extra edit controls).
    params: list[StudioModelParam] = Field(default_factory=list)


class StudioStyleLook(BaseModel):
    """The still-side signature of a style (mood / art / light / palette / grade)."""

    mood: str | None = None
    artStyle: str | None = None
    lighting: str | None = None
    colorPalette: list[str] = Field(default_factory=list)
    colorGrading: str | None = None
    medium: str | None = None


class StudioStyleMotion(BaseModel):
    """The camera-and-cutting signature of a style (cannot derive from a still)."""

    camera: str | None = None
    shots: str | None = None
    pace: str | None = None
    energy: int | None = None


class StudioStyleConfig(BaseModel):
    """The full visual treatment a curated style prescribes (look + motion +
    reference works). Mirrors openstory's style-config v2 schema."""

    version: int = 2
    look: StudioStyleLook = Field(default_factory=StudioStyleLook)
    motion: StudioStyleMotion = Field(default_factory=StudioStyleMotion)
    references: list[str] = Field(default_factory=list)


class StudioStyle(BaseModel):
    """A one-tap style/template. ``promptSuffix`` is appended to the user's
    prompt so the effect stays transparent (same convention as the mock).
    Curated styles additionally carry ``category`` / ``tags`` / ``config`` so the
    movie-maker can render a full detail card (palette, mood, lighting, camera,
    color grading, reference films) and a "Use this style" CTA."""

    id: str
    label: str
    description: str | None = None
    thumbnailUrl: str | None = None
    promptSuffix: str = ""
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    config: StudioStyleConfig | None = None


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
    # Reference images for the curated image models' edit path (character /
    # location / element consistency) — the backend resolves these to ``data:``
    # URLs and dispatches the model's EDIT endpoint via fal.
    referenceImageUrls: list[str] | None = None


class MusicRequest(BaseModel):
    """Body of ``POST /studio/music`` — generate a music/audio track.

    ``model`` may be a catalog key (``elevenlabs_music``) or a ``fal-ai/...``
    endpoint id; ``lyrics``/``instrumental``/``durationSec``/``steps`` map onto
    each music endpoint's own contract (see ``fal_music``)."""

    prompt: str
    model: str | None = None
    lyrics: str | None = None
    instrumental: bool = True
    durationSec: int | None = None
    steps: int | None = None
    tags: list[str] = Field(default_factory=list)


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
    # The rail edit composer's per-model parameter values (keyed by the model's
    # ``params`` keys — e.g. ``num_images`` / ``seed``). Only the ``edit`` op
    # reads them today.
    params: dict[str, Any] | None = None


class VideoElementsRequest(BaseModel):
    """Body of ``POST /studio/video-elements`` — the "Edit video" panel.

    Drives Kling Elements (``fal-ai/kling-video/v1.6/standard/elements``): a
    prompt plus up to 20 element/reference images, and optionally a source video
    to edit. ``sourceDurationSec`` is echoed by the frontend so the backend can
    enforce the 30-second source cap defensively (the frontend already reads the
    real duration from the video element, but never trust the client)."""

    prompt: str
    videoUrl: str | None = None
    inputImageUrls: list[str] | None = None
    aspectRatio: str = "16:9"
    durationSec: int | None = None
    sourceDurationSec: float | None = None
    model: str | None = None


class VideoMotionRequest(BaseModel):
    """Body of ``POST /studio/video-motion-control`` — the "Motion control" panel.

    Drives Kling Motion Control (``fal-ai/kling-video/v2.6/standard/motion-control``):
    a character image (visible face and body) is animated to follow a reference
    motion video. ``characterOrientation`` controls whether the character keeps
    the motion clip's orientation ("video") or its own ("image")."""

    imageUrl: str
    videoUrl: str
    characterOrientation: str = "video"
    aspectRatio: str = "16:9"
    durationSec: int | None = None
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
    "StudioModelParam",
    "StudioStyle",
    "StudioStyleLook",
    "StudioStyleMotion",
    "StudioStyleConfig",
    "GeneratedAsset",
    "GenerationParams",
    "Generation",
    "GenerateRequest",
    "MusicRequest",
    "EditRequest",
    "VideoElementsRequest",
    "VideoMotionRequest",
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
