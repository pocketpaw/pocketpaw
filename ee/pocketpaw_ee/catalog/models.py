# ee/pocketpaw_ee/catalog/models.py — the Model Catalog data contract (MCG-1).
#
# ModelCatalogEntry is the single shape clients consume for a model the platform
# can route to. Its ``id`` is the canonical key == the LiteLLM ``model_name``
# ("<provider>/<model>"). ``modality`` is the coarse capability bucket the picker
# groups by (chat / embedding / image / audio_tts / audio_stt / video — music is
# explicitly out of scope). Pricing is per-million-tokens for text modalities;
# None whenever the proxy/models.dev don't expose a cost. ``status`` is
# "available" unless the proxy signals the model can't currently be served.
# ``availability`` is per-workspace and per-request: the router overlays it from
# ``catalog.availability`` (out of credit after a provider 402, or ok) on a copy
# of the cached entry. It is None when the caller has no workspace session.
#
# These are plain Pydantic models (no Beanie / Mongo) — the catalog is assembled
# from upstream reads on demand and TTL-cached in-process, never persisted.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1): the catalog DTOs.

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Modality(StrEnum):
    """Coarse capability bucket the catalog groups + filters by.

    Maps from LiteLLM's per-model ``mode`` (see litellm_client.MODE_TO_MODALITY).
    ``music`` is deliberately absent — out of scope for MCG-1.
    """

    CHAT = "chat"
    EMBEDDING = "embedding"
    IMAGE = "image"
    AUDIO_TTS = "audio_tts"
    AUDIO_STT = "audio_stt"
    VIDEO = "video"


class Pricing(BaseModel):
    """Per-model cost. For text modalities these are USD per MILLION tokens
    (LiteLLM exposes per-token cost; the mapper multiplies by 1e6). A field is
    None when the upstream doesn't expose that side of the cost. The whole
    Pricing object is None on the entry when nothing is known."""

    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


class ModelAvailability(BaseModel):
    """Whether this workspace can run the model right now.

    ``out_of_credit`` means a recent run on this provider (or on the workspace's
    own key) failed for lack of credit. It clears itself after a short TTL, or at
    once on the next run that succeeds. ``since`` is when it was first seen;
    ``message`` is a plain sentence for the picker, never provider text.
    """

    status: Literal["ok", "out_of_credit"] = "ok"
    since: datetime | None = None
    message: str | None = None


class ModelCatalogEntry(BaseModel):
    """One routable (or known-but-disabled) model, grouped by modality.

    ``id`` is the canonical key — the LiteLLM ``model_name`` "<provider>/<model>".
    A client fetches one entry via GET /catalog/models/{id} with ``id``
    URL-encoded (the slash becomes %2F).
    """

    id: str
    display_name: str
    provider: str
    modality: Modality
    context: int | None = None
    max_output_tokens: int | None = None
    pricing: Pricing | None = None
    capabilities: list[str] = Field(default_factory=list)
    logo: str | None = None
    description: str | None = None
    status: str = "available"  # "available" | "disabled"
    availability: ModelAvailability | None = None


class ModelCatalogList(BaseModel):
    """Envelope for the list endpoint — a flat, already-filtered list plus the
    total count, so a client can render modality groups without a second call."""

    models: list[ModelCatalogEntry]
    total: int


__all__ = [
    "Modality",
    "Pricing",
    "ModelAvailability",
    "ModelCatalogEntry",
    "ModelCatalogList",
]
