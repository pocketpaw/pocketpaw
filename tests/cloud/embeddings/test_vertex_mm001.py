# test_vertex_mm001.py — VertexMultimodal001 unit tests.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Mocks vertexai and vertexai.vision_models so the lazy import succeeds
# without google-cloud-aiplatform installed. Pins the request shape
# (dimension keyword, contextual_text, Image), the dim-snapping logic
# (128/256/512/1408), and the unsupported-mime error.
"""Tests for ``ee.cloud.embeddings.vertex_mm001.VertexMultimodal001``."""

from __future__ import annotations

import sys
import types as _types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_vertex(monkeypatch):
    """Provide stubbed vertexai + vertexai.vision_models modules."""
    fake_image_cls = MagicMock()
    fake_image_cls.load_from_file = MagicMock(return_value=MagicMock(name="image"))

    fake_embed = MagicMock()
    fake_embed.image_embedding = [0.1] * 1408
    fake_embed.text_embedding = [0.5] * 1408

    fake_model = MagicMock()
    fake_model.get_embeddings = MagicMock(return_value=fake_embed)
    fake_model_cls = MagicMock()
    fake_model_cls.from_pretrained = MagicMock(return_value=fake_model)

    fake_vision = _types.ModuleType("vertexai.vision_models")
    fake_vision.MultiModalEmbeddingModel = fake_model_cls  # type: ignore[attr-defined]
    fake_vision.Image = fake_image_cls  # type: ignore[attr-defined]

    fake_vertexai = _types.ModuleType("vertexai")
    fake_vertexai.init = MagicMock()  # type: ignore[attr-defined]
    fake_vertexai.vision_models = fake_vision  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.vision_models", fake_vision)

    return {
        "vertexai": fake_vertexai,
        "Image": fake_image_cls,
        "model": fake_model,
        "embed": fake_embed,
    }


def test_init_calls_vertexai_init_and_loads_model(mock_vertex) -> None:
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    VertexMultimodal001(project_id="proj-1", location="us-central1", dim=512)
    mock_vertex["vertexai"].init.assert_called_once_with(project="proj-1", location="us-central1")


def test_dim_snaps_to_valid_native_choice(mock_vertex) -> None:
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    a512 = VertexMultimodal001(project_id="p", dim=512)
    a1408 = VertexMultimodal001(project_id="p", dim=1408)
    a300 = VertexMultimodal001(project_id="p", dim=300)
    a64 = VertexMultimodal001(project_id="p", dim=64)
    a2000 = VertexMultimodal001(project_id="p", dim=2000)

    assert a512.dim == 512
    assert a1408.dim == 1408
    # 300 → 256 (closest valid at-or-below)
    assert a300.dim == 256
    # 64 → 128 (smallest valid)
    assert a64.dim == 128
    # 2000 → 1408 (largest valid)
    assert a2000.dim == 1408


def test_init_raises_clear_error_when_sdk_missing(monkeypatch) -> None:
    """Lazy import path: ImportError surfaces with install hint."""
    monkeypatch.setitem(sys.modules, "vertexai", None)
    monkeypatch.setitem(sys.modules, "vertexai.vision_models", None)

    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    with pytest.raises(ImportError, match="google-cloud-aiplatform"):
        VertexMultimodal001(project_id="p")


@pytest.mark.asyncio
async def test_embed_file_image(mock_vertex, tmp_path: Path) -> None:
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    adapter = VertexMultimodal001(project_id="p", dim=512)
    result = await adapter.embed_file(img, "image/png")

    mock_vertex["Image"].load_from_file.assert_called_once_with(str(img))
    call_kwargs = mock_vertex["model"].get_embeddings.call_args.kwargs
    assert call_kwargs["dimension"] == 512
    assert "image" in call_kwargs

    assert result.dim == 1408  # mock returns 1408-dim regardless of dimension arg
    assert result.estimated_cost_usd == pytest.approx(0.0001)
    assert result.model == "multimodalembedding@001"


@pytest.mark.asyncio
async def test_embed_file_rejects_non_image_mime(mock_vertex, tmp_path: Path) -> None:
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    adapter = VertexMultimodal001(project_id="p", dim=512)
    with pytest.raises(ValueError, match="text and image only"):
        await adapter.embed_file(pdf, "application/pdf")


@pytest.mark.asyncio
async def test_embed_query_with_text_and_image(mock_vertex) -> None:
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    adapter = VertexMultimodal001(project_id="p", dim=1408)
    image_bytes = b"\x89PNG\r\n\x1a\n"
    result = await adapter.embed_query(text="similar diagram", image_bytes=image_bytes)

    call_kwargs = mock_vertex["model"].get_embeddings.call_args.kwargs
    assert call_kwargs["dimension"] == 1408
    assert call_kwargs["contextual_text"] == "similar diagram"
    assert "image" in call_kwargs
    # Text + image cost: 0.0001 image + 0.00002 per 1k chars (1 unit min).
    assert result.estimated_cost_usd > 0.0001


@pytest.mark.asyncio
async def test_embed_query_text_only_returns_text_embedding(mock_vertex) -> None:
    """When the call is pure text the SDK returns only a text embedding;
    the adapter must fall back to it instead of returning empty."""
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    # Drop image_embedding so the fallback path is exercised.
    mock_vertex["embed"].image_embedding = None

    adapter = VertexMultimodal001(project_id="p", dim=512)
    result = await adapter.embed_query(text="lonely text")

    assert len(result.vector) == 1408
    # text path uses 1k-char text cost only — no image dollars.
    assert result.estimated_cost_usd > 0


@pytest.mark.asyncio
async def test_embed_query_raises_when_both_embeddings_empty(mock_vertex) -> None:
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    mock_vertex["embed"].image_embedding = None
    mock_vertex["embed"].text_embedding = None

    adapter = VertexMultimodal001(project_id="p", dim=512)
    with pytest.raises(RuntimeError, match="empty embedding"):
        await adapter.embed_query(text="anything")


def test_estimate_cost_for_image_returns_per_image_rate(mock_vertex, tmp_path) -> None:
    from pocketpaw_ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    adapter = VertexMultimodal001(project_id="p", dim=512)
    assert adapter.estimate_cost(img, "image/png") == pytest.approx(0.0001)
    assert adapter.estimate_cost(img, "application/pdf") == 0.0
    assert adapter.estimate_cost(None, None) == 0.0
