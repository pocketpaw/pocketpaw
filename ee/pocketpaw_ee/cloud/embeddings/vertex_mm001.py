# vertex_mm001.py — multimodalembedding@001 adapter (GA, 1408-dim, text+image).
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Uses google-cloud-aiplatform's vertexai.vision_models surface (lazy
# imported; the package is not in pyproject.toml yet — captain decides
# whether to add it). Adapter raises a clear ImportError at construction
# time so build_embedder can return None and surface a single info log.
"""VertexMultimodal001 — google-cloud-aiplatform adapter.

The model (``multimodalembedding@001``, GA) embeds text and images into a
shared 1408-dim space. Native dim choices are 128 / 256 / 512 / 1408 —
the SDK accepts the 4 explicit values, and we pin to the closest one at
or below ``settings.embedding_dim``. PDFs / audio / video are not
supported by this model, so the adapter advertises only ``{"text", "image"}``.

Cost is billed per image at $0.0001 each; text is ~$0.00002 per 1k chars.
The estimator returns a flat per-image cost when given a path that looks
like an image; it returns the text estimate otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pocketpaw_ee.cloud.embeddings.adapter import EmbeddingResult

logger = logging.getLogger(__name__)

# Native dim choices for multimodalembedding@001. The SDK accepts these
# four values explicitly; anything else is silently rejected.
_VALID_DIMS = (128, 256, 512, 1408)
_DEFAULT_DIM = 1408

# Per-call cost constants. Vertex publishes per-image and per-text-token
# pricing for multimodalembedding@001; we proxy text by 1k characters.
_COST_PER_IMAGE_USD = 0.0001
_COST_PER_1K_CHARS_USD = 0.00002

# MIME prefixes the model will accept on the image side. PDF / audio /
# video are not in this model's repertoire — the listener consults
# ``supports_modalities`` to decide.
_IMAGE_MIME_PREFIX = "image/"


def _snap_to_valid_dim(requested: int) -> int:
    """Pick the closest valid native dim at or below ``requested``."""
    eligible = [d for d in _VALID_DIMS if d <= requested]
    if eligible:
        return max(eligible)
    return min(_VALID_DIMS)  # below 128 → snap up to 128


class VertexMultimodal001:
    """Vertex multimodalembedding@001 (GA). Text + image."""

    name = "vertex-mm-001"
    supports_modalities = {"text", "image"}
    requires_network = True

    def __init__(
        self,
        project_id: str,
        location: str = "us-central1",
        dim: int = 1408,
        model: str = "multimodalembedding@001",
    ) -> None:
        # Lazy-import so build_embedder can catch the ImportError and skip
        # the adapter cleanly when google-cloud-aiplatform isn't installed.
        try:
            import vertexai
            from vertexai.vision_models import MultiModalEmbeddingModel
        except ImportError as exc:
            raise ImportError(
                "vertex-mm-001 requires google-cloud-aiplatform. "
                "Install it with: pip install 'google-cloud-aiplatform>=1.50'"
            ) from exc

        self._vertexai = vertexai
        self._MultiModalEmbeddingModel = MultiModalEmbeddingModel  # noqa: N806

        self.dim = _snap_to_valid_dim(dim) if dim else _DEFAULT_DIM
        self._project_id = project_id
        self._location = location
        self._model_name = model

        # init() is idempotent and cheap — calling it once per adapter
        # instance keeps the call shape identical across multiple
        # embeddings even when the SDK caches its own state.
        vertexai.init(project=project_id, location=location)
        self._model = MultiModalEmbeddingModel.from_pretrained(model)

    async def embed_file(self, path: Path, mime: str) -> EmbeddingResult:
        """Embed an image file. Non-image MIMEs raise — the listener
        consults ``supports_modalities`` first, so this is defensive."""
        if not mime.startswith(_IMAGE_MIME_PREFIX):
            raise ValueError(f"vertex-mm-001 supports text and image only; got mime={mime!r}")
        from vertexai.vision_models import Image  # type: ignore[import-not-found]

        image = Image.load_from_file(str(path))
        embedding = await asyncio.to_thread(
            self._model.get_embeddings,
            image=image,
            dimension=self.dim,
        )
        vector = list(getattr(embedding, "image_embedding", []) or [])
        if not vector:
            raise RuntimeError("vertex-mm-001 returned empty image embedding")
        return EmbeddingResult(
            vector=vector,
            dim=len(vector),
            model=self._model_name,
            estimated_cost_usd=_COST_PER_IMAGE_USD,
        )

    async def embed_query(self, text: str, image_bytes: bytes | None = None) -> EmbeddingResult:
        """Embed an interleaved (text, image?) query."""
        from vertexai.vision_models import Image  # type: ignore[import-not-found]

        kwargs: dict = {"dimension": self.dim}
        cost = 0.0
        if text:
            kwargs["contextual_text"] = text
            cost += self._cost_for_text(text)
        if image_bytes is not None:
            kwargs["image"] = Image(image_bytes=image_bytes)
            cost += _COST_PER_IMAGE_USD

        embedding = await asyncio.to_thread(self._model.get_embeddings, **kwargs)
        # When both text and image are sent the SDK returns both
        # embeddings; we prefer the image embedding (visual queries are
        # the killer feature of this model). Fall back to text when the
        # caller passed only text.
        vector = list(
            getattr(embedding, "image_embedding", None)
            or getattr(embedding, "text_embedding", None)
            or []
        )
        if not vector:
            raise RuntimeError("vertex-mm-001 returned an empty embedding")
        return EmbeddingResult(
            vector=vector,
            dim=len(vector),
            model=self._model_name,
            estimated_cost_usd=cost,
        )

    def estimate_cost(self, path: Path | None, mime: str | None) -> float:
        """Pre-call cost estimate. No I/O on the path."""
        if path is None:
            return 0.0
        if mime and mime.startswith(_IMAGE_MIME_PREFIX):
            return _COST_PER_IMAGE_USD
        return 0.0

    # --- internals -------------------------------------------------------

    @staticmethod
    def _cost_for_text(text: str) -> float:
        units = max(1, len(text) // 1000)
        return units * _COST_PER_1K_CHARS_USD


__all__ = ["VertexMultimodal001"]
