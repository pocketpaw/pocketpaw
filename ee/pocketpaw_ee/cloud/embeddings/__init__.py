# __init__.py — Public surface for the embeddings package.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Exposes EmbeddingAdapter, EmbeddingResult, build_embedder, CostTracker,
# get_cost_tracker. Concrete adapters are imported on demand from
# build_embedder so this module imports in environments missing
# google-cloud-aiplatform (the lazy-import pattern matches extraction/).
"""Pluggable multimodal embedding adapters.

Public API:
  - ``EmbeddingResult`` — output model every adapter returns.
  - ``EmbeddingAdapter`` — Protocol that adapters implement.
  - ``build_embedder(settings)`` — factory; returns ``None`` when vectors
    are disabled, credentials missing, or required SDK not installed.
  - ``CostTracker`` / ``get_cost_tracker(settings)`` — soft monthly cap
    on embedding spend.

Concrete adapters (``VertexGeminiEmbedding2``, ``VertexMultimodal001``)
are imported on demand from ``build_embedder`` so this package can be
imported in environments missing the underlying SDKs.
"""

from pocketpaw_ee.cloud.embeddings.adapter import EmbeddingAdapter, EmbeddingResult
from pocketpaw_ee.cloud.embeddings.cost_tracker import (
    CostTracker,
    get_cost_tracker,
    reset_cost_tracker_for_tests,
)
from pocketpaw_ee.cloud.embeddings.factory import build_embedder

__all__ = [
    "CostTracker",
    "EmbeddingAdapter",
    "EmbeddingResult",
    "build_embedder",
    "get_cost_tracker",
    "reset_cost_tracker_for_tests",
]
