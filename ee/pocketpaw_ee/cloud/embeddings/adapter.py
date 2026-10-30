# adapter.py — EmbeddingAdapter Protocol + EmbeddingResult model.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Defines the pluggable adapter contract for multimodal embedding so the
# listener vector path and the interleaved-query path in context_builder
# can swap models (Gemini Embedding 2 vs Vertex multimodalembedding@001)
# without touching their callsites.
"""Embedding adapter protocol.

Mirrors the shape of ``ee.cloud.extraction.adapter.ExtractionAdapter``.
Each adapter declares the modalities it handles (text / image / pdf /
audio / video) and whether it needs network. Adapters return an
:class:`EmbeddingResult` so the listener can record per-call cost without
re-deriving it from the model name.

Two implementations ship in Stage 2.D:

* :class:`ee.cloud.embeddings.vertex_gemini2.VertexGeminiEmbedding2` —
  preview, 3072-dim, multimodal (text/image/pdf/audio/video).
* :class:`ee.cloud.embeddings.vertex_mm001.VertexMultimodal001` — GA,
  1408-dim max, text + image only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class EmbeddingResult(BaseModel):
    """Output shape every adapter returns.

    vector: the embedding (length == ``dim``).
    dim: length of ``vector`` (after Matryoshka truncation if the adapter
        applied one). May be smaller than the model's native size.
    model: the model identifier the call used (e.g. ``"gemini-embedding-001"``).
    estimated_cost_usd: rough $ for this call. NOT billing-grade — the
        listener uses it as a soft cap. Real billing comes from the
        provider's dashboard.
    """

    vector: list[float]
    dim: int
    model: str
    estimated_cost_usd: float = Field(default=0.0)


@runtime_checkable
class EmbeddingAdapter(Protocol):
    """Duck-typed embedding adapter.

    Attributes:
        name: stable identifier (``"vertex-gemini-embedding-2"``,
            ``"vertex-mm-001"``). Used in settings and logs.
        dim: default output dimensionality (after Matryoshka truncation).
            The listener uses this to know what kb-go expects in the
            per-scope vector index — all vectors in a single index must
            agree on dim.
        supports_modalities: set of modality strings; valid members are
            ``"text"``, ``"image"``, ``"pdf"``, ``"audio"``, ``"video"``.
            The listener consults this before calling ``embed_file`` /
            ``embed_query`` so an unsupported file type skips embedding
            instead of raising.
        requires_network: when True the listener may skip when the host
            is offline. (Today both shipped adapters always require
            network; reserved for a future local-MiniLM fallback.)
    """

    name: str
    dim: int
    supports_modalities: set[str]
    requires_network: bool

    async def embed_file(self, path: Path, mime: str) -> EmbeddingResult: ...

    async def embed_query(self, text: str, image_bytes: bytes | None = None) -> EmbeddingResult: ...

    def estimate_cost(self, path: Path | None, mime: str | None) -> float: ...
