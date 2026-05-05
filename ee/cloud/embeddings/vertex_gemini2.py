# vertex_gemini2.py — Gemini Embedding 2 adapter (preview, 3072-dim, multimodal).
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Uses google-genai (already in pyproject for the gemini-flash extractor).
# Native multimodal across text/image/pdf/audio/video; truncates via
# Matryoshka representation learning so callers can drop to 1024 / 768 / 512
# without re-running the model.
"""VertexGeminiEmbedding2 — google-genai SDK adapter.

The model identifier on google-genai is ``gemini-embedding-001`` (the
"Gemini Embedding 2" preview surfaces under that name in the SDK as of
2026-04). 3072-dim native output; we Matryoshka-truncate to
``settings.embedding_dim`` so the per-scope kb-go vector index stays at
the deployment's chosen size.

Cost estimation is a flat $0.000025 per 1k input tokens. No public
Vertex pricing exists for this preview model yet — we use the
``gemini-embedding-001`` GA price as a stand-in. The listener treats
the returned cost as a soft cap, not a billing source.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from ee.cloud.embeddings.adapter import EmbeddingResult

logger = logging.getLogger(__name__)

# Per-1k-token cost in USD. Gemini Embedding 2 preview pricing isn't
# published yet; use the GA gemini-embedding-001 list price as a stand-in
# so the cap tracker has a number to work with. Off by a factor is fine —
# the cap is a soft guard, not a billing source.
_COST_PER_1K_TOKENS_USD = 0.000025
# Coarse token estimate: 1 token ~ 4 chars for text. For files we use the
# byte count as a worst-case proxy.
_CHARS_PER_TOKEN = 4
# Native model dim. Matryoshka truncation lets callers pick something
# smaller without re-running the model.
_NATIVE_DIM = 3072


class VertexGeminiEmbedding2:
    """Gemini Embedding 2 (preview) via google-genai. Multimodal."""

    name = "vertex-gemini-embedding-2"
    supports_modalities = {"text", "image", "pdf", "audio", "video"}
    requires_network = True

    def __init__(
        self,
        api_key: str,
        dim: int = 1024,
        model: str = "gemini-embedding-001",
    ) -> None:
        # Lazy-import so the file imports in environments without
        # google-genai (mirrors GeminiFlashExtractor). Tests patch
        # ``google.genai.Client``.
        from google import genai

        if dim < 1 or dim > _NATIVE_DIM:
            raise ValueError(f"embedding_dim={dim} out of range [1, {_NATIVE_DIM}]")
        self.dim = dim
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def embed_file(self, path: Path, mime: str) -> EmbeddingResult:
        """Embed a file. Reads bytes, builds a Part, calls embed_content."""
        from google.genai import types

        data = path.read_bytes()
        if mime.startswith("text/") or mime in {
            "application/json",
            "application/xml",
        }:
            # Plain text: skip the inline-Part path and embed as text.
            return await self.embed_query(text=data.decode("utf-8", errors="replace"))

        contents: list = [types.Part.from_bytes(data=data, mime_type=mime)]
        response = await asyncio.to_thread(
            self._client.models.embed_content,
            model=self._model,
            contents=contents,
        )
        vector = self._unwrap_vector(response)
        cost = self._cost_for_bytes(len(data))
        return self._matryoshka(vector, cost)

    async def embed_query(self, text: str, image_bytes: bytes | None = None) -> EmbeddingResult:
        """Embed a (text, image?) interleaved query.

        When ``image_bytes`` is ``None`` we embed plain text. When set, the
        text and image go to the model in one call as an interleaved input
        — this is the property the chat-with-image path relies on.
        """
        from google.genai import types

        if image_bytes is None:
            contents: list = [text or ""]
            cost = self._cost_for_text(text or "")
        else:
            contents = []
            if text:
                contents.append(text)
            # MIME guess based on magic bytes — kept light so we don't pull
            # ``filetype`` for one inline call. PNG / JPEG cover almost all
            # paste-from-clipboard cases.
            mime = _guess_image_mime(image_bytes)
            contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime))
            cost = self._cost_for_bytes(len(image_bytes)) + self._cost_for_text(text or "")

        response = await asyncio.to_thread(
            self._client.models.embed_content,
            model=self._model,
            contents=contents,
        )
        vector = self._unwrap_vector(response)
        return self._matryoshka(vector, cost)

    def estimate_cost(self, path: Path | None, mime: str | None) -> float:
        """Cheap pre-call estimate. No I/O when ``path`` is None."""
        if path is None:
            return 0.0
        try:
            size = path.stat().st_size
        except OSError:
            return 0.0
        return self._cost_for_bytes(size)

    # --- internals -------------------------------------------------------

    @staticmethod
    def _cost_for_text(text: str) -> float:
        tokens = max(1, len(text) // _CHARS_PER_TOKEN)
        return (tokens / 1000.0) * _COST_PER_1K_TOKENS_USD

    @staticmethod
    def _cost_for_bytes(n_bytes: int) -> float:
        # File embeddings are billed per token in practice; we proxy bytes
        # to tokens at the same chars-per-token ratio so a 100kB image
        # comes out to ~$0.000625. Exactness doesn't matter here.
        tokens = max(1, n_bytes // _CHARS_PER_TOKEN)
        return (tokens / 1000.0) * _COST_PER_1K_TOKENS_USD

    @staticmethod
    def _unwrap_vector(response) -> list[float]:
        """Pull the embedding values out of an embed_content response.

        google-genai's response shape varies by SDK version: older builds
        return ``response.embedding.values``, newer ones expose
        ``response.embeddings[0].values``. Accept both so the adapter
        survives an SDK bump.
        """
        if hasattr(response, "embedding") and getattr(response.embedding, "values", None):
            return list(response.embedding.values)
        if hasattr(response, "embeddings"):
            embeds = response.embeddings
            if embeds and getattr(embeds[0], "values", None):
                return list(embeds[0].values)
        # Last-resort: response itself might be subscriptable in test mocks.
        try:
            return list(response["embedding"]["values"])  # type: ignore[index]
        except (KeyError, TypeError):
            pass
        raise RuntimeError("could not locate embedding values on Gemini response")

    def _matryoshka(self, vector: list[float], cost: float) -> EmbeddingResult:
        """Truncate to ``self.dim`` if the model returned more.

        Gemini Embedding 2 is trained with Matryoshka representation
        learning, so the first N dims are a valid lower-dim embedding.
        No re-normalization step required for the listener-side index —
        kb-go's cosine search handles unnormalized vectors. (When kb-go
        gets stricter we'll add an L2 step here.)
        """
        truncated = vector[: self.dim] if len(vector) > self.dim else vector
        return EmbeddingResult(
            vector=truncated,
            dim=len(truncated),
            model=self._model,
            estimated_cost_usd=cost,
        )


def _guess_image_mime(image_bytes: bytes) -> str:
    """Tiny magic-byte sniffer for PNG / JPEG. Defaults to PNG.

    Used by the interleaved query path when the chat layer hands us raw
    bytes without a MIME hint. Most paste-from-clipboard images are PNG;
    camera uploads are usually JPEG.
    """
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


__all__ = ["VertexGeminiEmbedding2"]


# A tiny helper so other modules can base64-encode bytes if needed.
# (Kept here rather than util.py — only the chat path uses it.)
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
