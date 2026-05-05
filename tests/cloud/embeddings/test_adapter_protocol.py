# test_adapter_protocol.py — Protocol conformance for both shipped adapters.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Verifies VertexGeminiEmbedding2 and VertexMultimodal001 satisfy the
# EmbeddingAdapter Protocol at runtime — `isinstance(_, EmbeddingAdapter)`
# uses the @runtime_checkable shape so the duck-typing stays honest as new
# adapters land.
"""Protocol conformance tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ee.cloud.embeddings import EmbeddingAdapter, EmbeddingResult


def test_embedding_result_basic_shape() -> None:
    """EmbeddingResult is a Pydantic model with the required fields."""
    r = EmbeddingResult(vector=[0.1, 0.2, 0.3], dim=3, model="test", estimated_cost_usd=0.001)
    assert r.vector == [0.1, 0.2, 0.3]
    assert r.dim == 3
    assert r.model == "test"
    assert r.estimated_cost_usd == pytest.approx(0.001)


def test_embedding_result_cost_defaults_to_zero() -> None:
    r = EmbeddingResult(vector=[0.0], dim=1, model="x")
    assert r.estimated_cost_usd == 0.0


def test_vertex_gemini2_satisfies_protocol(monkeypatch) -> None:
    """VertexGeminiEmbedding2 instance must satisfy EmbeddingAdapter."""
    from google import genai

    fake_client = MagicMock()
    monkeypatch.setattr(genai, "Client", lambda api_key: fake_client)

    from ee.cloud.embeddings.vertex_gemini2 import VertexGeminiEmbedding2

    adapter = VertexGeminiEmbedding2(api_key="fake", dim=512)
    assert isinstance(adapter, EmbeddingAdapter)
    assert adapter.name == "vertex-gemini-embedding-2"
    assert adapter.dim == 512
    assert "image" in adapter.supports_modalities
    assert "text" in adapter.supports_modalities
    assert adapter.requires_network is True


def test_vertex_mm001_satisfies_protocol(monkeypatch) -> None:
    """VertexMultimodal001 instance satisfies EmbeddingAdapter when SDK is mocked."""
    # Stub vertexai + vertexai.vision_models so the lazy import succeeds.
    import sys
    import types as _types

    fake_image_cls = MagicMock()
    fake_model = MagicMock()
    fake_model.from_pretrained = MagicMock(return_value=fake_model)

    fake_vision = _types.ModuleType("vertexai.vision_models")
    fake_vision.MultiModalEmbeddingModel = fake_model  # type: ignore[attr-defined]
    fake_vision.Image = fake_image_cls  # type: ignore[attr-defined]
    fake_vertexai = _types.ModuleType("vertexai")
    fake_vertexai.init = MagicMock()  # type: ignore[attr-defined]
    fake_vertexai.vision_models = fake_vision  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.vision_models", fake_vision)

    from ee.cloud.embeddings.vertex_mm001 import VertexMultimodal001

    adapter = VertexMultimodal001(project_id="test-proj", location="us-central1", dim=512)
    assert isinstance(adapter, EmbeddingAdapter)
    assert adapter.name == "vertex-mm-001"
    # 512 is in the valid set so it's chosen as-is.
    assert adapter.dim == 512
    assert adapter.supports_modalities == {"text", "image"}
    assert adapter.requires_network is True
