# test_kb_query_with_image.py — interleaved-image query path tests.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Verifies that when image_bytes is set on _get_kb_context, the embedder
# is consulted, kb is invoked with --hybrid --query-vec, and a transient
# embed failure falls back to BM25 mode without crashing the prompt build.
"""Tests for the interleaved-image query path in AgentContextBuilder."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pocketpaw.bootstrap.context_builder import AgentContextBuilder
from pocketpaw.config import Settings


def _stub_settings(monkeypatch, **overrides: Any) -> None:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    for k, v in overrides.items():
        setattr(base, k, v)
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda force_reload=False: base,
    )


class _FakeEmbedder:
    name = "vertex-mm-001"
    dim = 8
    supports_modalities = {"text", "image"}
    requires_network = True

    def __init__(self, *, vector=None, fail=False):
        self._vector = vector or [0.7] * 8
        self._fail = fail
        self.queries: list[tuple[str, bytes | None]] = []

    async def embed_query(self, text, image_bytes=None):
        self.queries.append((text, image_bytes))
        if self._fail:
            raise RuntimeError("upstream blew up")
        from ee.cloud.embeddings import EmbeddingResult

        return EmbeddingResult(
            vector=list(self._vector),
            dim=len(self._vector),
            model=self.name,
            estimated_cost_usd=0.0001,
        )

    async def embed_file(self, path, mime):  # pragma: no cover
        raise NotImplementedError

    def estimate_cost(self, path, mime):
        return 0.0001


class _TextOnlyEmbedder(_FakeEmbedder):
    supports_modalities = {"text"}


@pytest.mark.asyncio
async def test_image_query_uses_hybrid_with_query_vec(monkeypatch):
    """image_bytes provided → embed_query called → kb invoked --hybrid --query-vec."""
    embedder = _FakeEmbedder()
    monkeypatch.setattr(
        "ee.cloud.embeddings.build_embedder", lambda s: embedder
    )
    _stub_settings(
        monkeypatch,
        kb_vectors_enabled=True,
        embedding_adapter="vertex-mm-001",
        kb_scopes=["workspace:w1"],
        kb_limit=2,
        kb_binary="kb",
    )

    captured: dict = {}

    async def fake_subprocess_exec(*args, **kwargs):
        captured["argv"] = list(args)

        class _Proc:
            returncode = 0

            async def communicate(self):
                # JSON shape kb-go returns for hybrid mode.
                rows = [
                    {"id": "a1", "title": "Slide 3", "summary": "matrix diagram"},
                    {"id": "a2", "title": "Notes", "summary": "follow-up tasks"},
                ]
                return json.dumps(rows).encode("utf-8"), b""

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess_exec)

    out = await AgentContextBuilder._get_kb_context(
        "find the slide", image_bytes=b"\x89PNG\r\n\x1a\n"
    )

    # Embedder was called once with (text, image_bytes).
    assert embedder.queries == [("find the slide", b"\x89PNG\r\n\x1a\n")]

    # kb subprocess was hybrid-mode.
    argv = captured["argv"]
    assert argv[0] == "kb"
    assert argv[1] == "search"
    assert "--hybrid" in argv
    assert "--query-vec" in argv
    qv_idx = argv.index("--query-vec") + 1
    assert argv[qv_idx].endswith(".json")  # temp file path
    assert "--scope" in argv
    assert argv[argv.index("--scope") + 1] == "workspace:w1"
    assert "--topk" in argv

    # The rendered section pulled title + summary from JSON rows.
    assert "Slide 3" in out
    assert "matrix diagram" in out
    assert "### From workspace:w1" in out


@pytest.mark.asyncio
async def test_image_query_falls_back_to_bm25_when_embed_fails(monkeypatch):
    failing = _FakeEmbedder(fail=True)
    monkeypatch.setattr(
        "ee.cloud.embeddings.build_embedder", lambda s: failing
    )
    _stub_settings(
        monkeypatch,
        kb_vectors_enabled=True,
        embedding_adapter="vertex-mm-001",
        kb_scopes=["workspace:w1"],
        kb_limit=1,
        kb_binary="kb",
    )

    captured_argvs: list[list[str]] = []

    async def fake_subprocess_exec(*args, **kwargs):
        captured_argvs.append(list(args))

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"plain BM25 hit", b""

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess_exec)

    out = await AgentContextBuilder._get_kb_context(
        "find slide", image_bytes=b"\x89PNG"
    )

    assert "plain BM25 hit" in out
    # Plain BM25 path: no --hybrid / --query-vec.
    assert len(captured_argvs) == 1
    argv = captured_argvs[0]
    assert "--hybrid" not in argv
    assert "--query-vec" not in argv
    assert "--context" in argv


@pytest.mark.asyncio
async def test_image_query_falls_back_when_embedder_lacks_image_modality(monkeypatch):
    text_only = _TextOnlyEmbedder()
    monkeypatch.setattr(
        "ee.cloud.embeddings.build_embedder", lambda s: text_only
    )
    _stub_settings(
        monkeypatch,
        kb_vectors_enabled=True,
        embedding_adapter="text-only",
        kb_scopes=["workspace:w1"],
        kb_limit=1,
        kb_binary="kb",
    )

    captured: list[list[str]] = []

    async def fake_subprocess_exec(*args, **kwargs):
        captured.append(list(args))

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"BM25 only", b""

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess_exec)

    await AgentContextBuilder._get_kb_context(
        "any query", image_bytes=b"\x89PNG"
    )

    # Embedder lacks "image" modality → BM25 path.
    assert text_only.queries == []
    assert "--hybrid" not in captured[0]


@pytest.mark.asyncio
async def test_image_query_skips_when_vectors_disabled(monkeypatch):
    embedder = _FakeEmbedder()
    monkeypatch.setattr(
        "ee.cloud.embeddings.build_embedder", lambda s: embedder
    )
    _stub_settings(
        monkeypatch,
        kb_vectors_enabled=False,
        embedding_adapter="vertex-mm-001",
        kb_scopes=["workspace:w1"],
        kb_limit=1,
        kb_binary="kb",
    )

    async def fake_subprocess_exec(*args, **kwargs):
        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"BM25", b""

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess_exec)

    await AgentContextBuilder._get_kb_context(
        "query", image_bytes=b"\x89PNG"
    )

    assert embedder.queries == []  # never called


@pytest.mark.asyncio
async def test_no_image_keeps_bm25_call_shape(monkeypatch):
    """Regression: without image_bytes the call is the Phase 1 BM25 shape."""
    embedder_called = False

    def _build(_s):
        nonlocal embedder_called
        embedder_called = True
        return _FakeEmbedder()

    monkeypatch.setattr("ee.cloud.embeddings.build_embedder", _build)
    _stub_settings(
        monkeypatch,
        kb_vectors_enabled=True,
        embedding_adapter="vertex-mm-001",
        kb_scopes=["workspace:w1"],
        kb_limit=1,
        kb_binary="kb",
    )

    captured: list[list[str]] = []

    async def fake_subprocess_exec(*args, **kwargs):
        captured.append(list(args))

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"plain hit", b""

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess_exec)

    out = await AgentContextBuilder._get_kb_context("just text query")

    assert "plain hit" in out
    assert not embedder_called  # build_embedder not consulted at all
    assert "--hybrid" not in captured[0]
