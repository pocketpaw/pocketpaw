# test_vertex_gemini2.py — VertexGeminiEmbedding2 unit tests.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Mocks google.genai.Client to avoid network calls. Pins request shape
# (model name, contents structure with Part for the image), Matryoshka
# truncation behaviour, the empty-response failure path, and the
# multiple-response-shape unwrap helper.
"""Tests for ``ee.cloud.embeddings.vertex_gemini2.VertexGeminiEmbedding2``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_genai(monkeypatch):
    """Patch google.genai.Client and return a (client, response) tuple."""
    from google import genai

    response = MagicMock()
    response.embedding.values = [float(i) / 100.0 for i in range(3072)]
    client = MagicMock()
    client.models.embed_content.return_value = response
    monkeypatch.setattr(genai, "Client", lambda api_key: client)
    return client, response


@pytest.fixture
def tmp_image(tmp_path: Path) -> Path:
    p = tmp_path / "diagram.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    return p


def test_dim_out_of_range_raises(monkeypatch, mock_genai) -> None:
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    with pytest.raises(ValueError, match="out of range"):
        VertexGeminiEmbedding2(api_key="fake", dim=4096)
    with pytest.raises(ValueError, match="out of range"):
        VertexGeminiEmbedding2(api_key="fake", dim=0)


@pytest.mark.asyncio
async def test_embed_file_image_calls_with_part(mock_genai, tmp_image) -> None:
    """Image file → contents=[Part(image bytes, mime)] sent to embed_content."""
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    client, _ = mock_genai
    adapter = VertexGeminiEmbedding2(api_key="fake", dim=1024)
    result = await adapter.embed_file(tmp_image, "image/png")

    client.models.embed_content.assert_called_once()
    kwargs = client.models.embed_content.call_args.kwargs
    assert kwargs["model"] == "gemini-embedding-001"
    contents = kwargs["contents"]
    assert len(contents) == 1
    part = contents[0]
    # google.genai types.Part stores bytes on inline_data.
    assert getattr(part.inline_data, "mime_type", None) == "image/png"
    assert getattr(part.inline_data, "data", None) == tmp_image.read_bytes()

    assert result.dim == 1024
    assert len(result.vector) == 1024
    assert result.model == "gemini-embedding-001"
    assert result.estimated_cost_usd > 0


@pytest.mark.asyncio
async def test_embed_file_text_skips_part_path(mock_genai, tmp_path) -> None:
    """text/plain files go through embed_query, not the inline-Part path."""
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    client, _ = mock_genai
    adapter = VertexGeminiEmbedding2(api_key="fake", dim=512)
    p = tmp_path / "notes.txt"
    p.write_text("hello world")
    await adapter.embed_file(p, "text/plain")

    kwargs = client.models.embed_content.call_args.kwargs
    contents = kwargs["contents"]
    assert contents == ["hello world"]


@pytest.mark.asyncio
async def test_matryoshka_truncates_when_dim_smaller_than_native(mock_genai, tmp_image) -> None:
    """When dim < 3072 the result vector is the first dim values."""
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    _, response = mock_genai
    response.embedding.values = list(range(3072))  # 0..3071

    adapter = VertexGeminiEmbedding2(api_key="fake", dim=8)
    result = await adapter.embed_file(tmp_image, "image/png")

    assert result.vector == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    assert result.dim == 8


@pytest.mark.asyncio
async def test_embed_query_text_only(mock_genai) -> None:
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    client, _ = mock_genai
    adapter = VertexGeminiEmbedding2(api_key="fake", dim=64)
    result = await adapter.embed_query(text="diagram of arrows")

    kwargs = client.models.embed_content.call_args.kwargs
    assert kwargs["contents"] == ["diagram of arrows"]
    assert result.dim == 64


@pytest.mark.asyncio
async def test_embed_query_with_image_bytes(mock_genai) -> None:
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    client, _ = mock_genai
    adapter = VertexGeminiEmbedding2(api_key="fake", dim=64)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    await adapter.embed_query(text="find similar slide", image_bytes=png_bytes)

    kwargs = client.models.embed_content.call_args.kwargs
    contents = kwargs["contents"]
    # [text, Part(image)]
    assert len(contents) == 2
    assert contents[0] == "find similar slide"
    part = contents[1]
    assert getattr(part.inline_data, "mime_type", None) == "image/png"


@pytest.mark.asyncio
async def test_embed_query_with_jpeg_bytes_picks_jpeg_mime(mock_genai) -> None:
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    client, _ = mock_genai
    adapter = VertexGeminiEmbedding2(api_key="fake", dim=32)
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
    await adapter.embed_query(text="", image_bytes=jpeg)

    kwargs = client.models.embed_content.call_args.kwargs
    contents = kwargs["contents"]
    # Empty text is dropped; only the image Part comes through.
    assert len(contents) == 1
    assert getattr(contents[0].inline_data, "mime_type", None) == "image/jpeg"


@pytest.mark.asyncio
async def test_unwrap_handles_embeddings_list_shape(mock_genai, tmp_image) -> None:
    """Newer google-genai versions return response.embeddings[0].values."""
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    client, response = mock_genai
    # Drop the singular .embedding shape, expose the plural.
    delattr(response, "embedding")
    plural_entry = MagicMock()
    plural_entry.values = [0.1, 0.2, 0.3]
    response.embeddings = [plural_entry]

    adapter = VertexGeminiEmbedding2(api_key="fake", dim=2)
    result = await adapter.embed_file(tmp_image, "image/png")
    assert result.vector == [0.1, 0.2]


@pytest.mark.asyncio
async def test_unwrap_raises_on_unknown_shape(mock_genai, tmp_image) -> None:
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    _, response = mock_genai
    # Strip every shape we know how to unwrap.
    delattr(response, "embedding")
    response.embeddings = []
    # Mock cannot subscript: raise TypeError.

    class _UnsubscriptableResponse:
        embeddings: list = []

    adapter = VertexGeminiEmbedding2(api_key="fake", dim=2)
    # Force the embed_content mock to return the unsubscriptable instance.
    adapter._client.models.embed_content.return_value = _UnsubscriptableResponse()
    with pytest.raises(RuntimeError, match="could not locate embedding"):
        await adapter.embed_file(tmp_image, "image/png")


def test_estimate_cost_handles_missing_path(mock_genai) -> None:
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    adapter = VertexGeminiEmbedding2(api_key="fake", dim=64)
    assert adapter.estimate_cost(None, None) == 0.0


def test_estimate_cost_uses_file_size(mock_genai, tmp_image) -> None:
    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    adapter = VertexGeminiEmbedding2(api_key="fake", dim=64)
    cost = adapter.estimate_cost(tmp_image, "image/png")
    assert cost > 0
