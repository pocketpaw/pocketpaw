# test_factory.py — build_embedder(settings) factory tests.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Pins the disabled / missing-creds / unknown-name behaviour. Verifies
# that the lazy import for vertex-mm-001 returns None (with an info log)
# instead of raising when google-cloud-aiplatform isn't installed.
"""Tests for ``ee.cloud.embeddings.factory.build_embedder``."""

from __future__ import annotations

import sys
import types as _types
from unittest.mock import MagicMock

import pytest

from ee.cloud.embeddings import build_embedder


class _Stub:
    """Plain object stand-in for Settings — only the fields we read."""

    def __init__(self, **kwargs):
        defaults = dict(
            kb_vectors_enabled=True,
            embedding_adapter="",
            embedding_dim=1024,
            gemini_api_key=None,
            vertex_project_id=None,
            vertex_location=None,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


def test_returns_none_when_vectors_disabled() -> None:
    s = _Stub(kb_vectors_enabled=False, embedding_adapter="vertex-gemini-embedding-2")
    assert build_embedder(s) is None


def test_returns_none_when_adapter_empty() -> None:
    s = _Stub(kb_vectors_enabled=True, embedding_adapter="")
    assert build_embedder(s) is None


def test_returns_none_when_gemini_creds_missing() -> None:
    s = _Stub(
        kb_vectors_enabled=True,
        embedding_adapter="vertex-gemini-embedding-2",
        gemini_api_key=None,
    )
    assert build_embedder(s) is None


def test_returns_none_when_vertex_project_id_missing() -> None:
    s = _Stub(
        kb_vectors_enabled=True,
        embedding_adapter="vertex-mm-001",
        vertex_project_id=None,
    )
    assert build_embedder(s) is None


def test_returns_none_when_vertex_sdk_missing(monkeypatch) -> None:
    """Lazy import: ImportError → factory returns None instead of raising."""
    monkeypatch.setitem(sys.modules, "vertexai", None)
    monkeypatch.setitem(sys.modules, "vertexai.vision_models", None)

    s = _Stub(
        kb_vectors_enabled=True,
        embedding_adapter="vertex-mm-001",
        vertex_project_id="proj-1",
        vertex_location="us-central1",
    )
    assert build_embedder(s) is None


def test_unknown_adapter_name_raises() -> None:
    s = _Stub(kb_vectors_enabled=True, embedding_adapter="open-models-rocks")
    with pytest.raises(ValueError, match="unknown embedding adapter"):
        build_embedder(s)


def test_builds_gemini_embedder_when_key_set(monkeypatch) -> None:
    from google import genai

    monkeypatch.setattr(genai, "Client", lambda api_key: MagicMock())
    s = _Stub(
        kb_vectors_enabled=True,
        embedding_adapter="vertex-gemini-embedding-2",
        gemini_api_key="fake",
        embedding_dim=512,
    )
    adapter = build_embedder(s)
    assert adapter is not None
    assert adapter.name == "vertex-gemini-embedding-2"
    assert adapter.dim == 512


def test_builds_vertex_mm_embedder_when_creds_set(monkeypatch) -> None:
    """Stub the vertex SDK so the lazy import succeeds."""
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

    s = _Stub(
        kb_vectors_enabled=True,
        embedding_adapter="vertex-mm-001",
        vertex_project_id="proj-1",
        vertex_location="us-central1",
        embedding_dim=512,
    )
    adapter = build_embedder(s)
    assert adapter is not None
    assert adapter.name == "vertex-mm-001"
    assert adapter.dim == 512


def test_vertex_default_location_falls_back_to_us_central1(monkeypatch) -> None:
    fake_image_cls = MagicMock()
    fake_model = MagicMock()
    fake_model.from_pretrained = MagicMock(return_value=fake_model)
    fake_vision = _types.ModuleType("vertexai.vision_models")
    fake_vision.MultiModalEmbeddingModel = fake_model  # type: ignore[attr-defined]
    fake_vision.Image = fake_image_cls  # type: ignore[attr-defined]
    fake_vertexai = _types.ModuleType("vertexai")
    init_mock = MagicMock()
    fake_vertexai.init = init_mock  # type: ignore[attr-defined]
    fake_vertexai.vision_models = fake_vision  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.vision_models", fake_vision)

    s = _Stub(
        kb_vectors_enabled=True,
        embedding_adapter="vertex-mm-001",
        vertex_project_id="proj-1",
        vertex_location=None,  # no location set
        embedding_dim=1408,
    )
    adapter = build_embedder(s)
    assert adapter is not None
    init_mock.assert_called_once_with(project="proj-1", location="us-central1")
