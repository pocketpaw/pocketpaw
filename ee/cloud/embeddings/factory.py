# factory.py — build_embedder(settings): pick the configured adapter.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Mirrors ee.cloud.extraction.chain.build_chain — settings → adapter
# instance, with cred / dep checks short-circuiting to None so the
# listener degrades gracefully (text still ingests, vector skipped).
"""Embedder factory.

The settings shape:

    settings.kb_vectors_enabled: bool
    settings.embedding_adapter: str  # "" | "vertex-gemini-embedding-2" | "vertex-mm-001"
    settings.embedding_dim: int
    settings.gemini_api_key: str | None       # for vertex-gemini-embedding-2
    settings.vertex_project_id: str | None    # for vertex-mm-001
    settings.vertex_location: str | None      # for vertex-mm-001

Returns ``None`` (vectors disabled) when:

  * ``kb_vectors_enabled`` is False
  * ``embedding_adapter`` is empty
  * required credentials aren't set
  * the adapter's SDK isn't installed (vertex-mm-001 needs
    google-cloud-aiplatform; if absent, ImportError is caught and we
    return None with an info log)

Raises ``ValueError`` for an unknown adapter name. That's a config bug,
not a runtime degradation, so it must be loud.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ee.cloud.embeddings.adapter import EmbeddingAdapter

logger = logging.getLogger(__name__)


def build_embedder(settings) -> EmbeddingAdapter | None:
    """Return the configured adapter, or ``None`` when embeddings are disabled."""
    if not getattr(settings, "kb_vectors_enabled", False):
        return None

    name = (getattr(settings, "embedding_adapter", "") or "").strip()
    if not name:
        return None

    dim = int(getattr(settings, "embedding_dim", 1024))

    if name == "vertex-gemini-embedding-2":
        api_key = getattr(settings, "gemini_api_key", None)
        if not api_key:
            logger.info("vertex-gemini-embedding-2 requires gemini_api_key; embeddings disabled")
            return None
        from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

        return VertexGeminiEmbedding2(api_key=api_key, dim=dim)

    if name == "vertex-mm-001":
        project_id = getattr(settings, "vertex_project_id", None)
        if not project_id:
            logger.info("vertex-mm-001 requires vertex_project_id; embeddings disabled")
            return None
        location = getattr(settings, "vertex_location", None) or "us-central1"
        try:
            from ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

            return VertexMultimodal001(
                project_id=project_id,
                location=location,
                dim=dim,
            )
        except ImportError:
            logger.info(
                "vertex-mm-001 needs google-cloud-aiplatform; not installed, embeddings disabled"
            )
            return None

    raise ValueError(f"unknown embedding adapter: {name!r}")


__all__ = ["build_embedder"]
