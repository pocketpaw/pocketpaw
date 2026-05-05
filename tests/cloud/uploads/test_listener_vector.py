# test_listener_vector.py — listener vector-path coverage.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Covers the post-text-ingest hook: vectors enabled → embedder called →
# kb subprocess invoked with --vec; cap hit → vector skipped, text still
# ingests; embedder failure → text-only path stays intact; modality
# mismatch → vector skipped quietly. Mocks the kb-go subprocess (it
# isn't on the test runner's PATH).
"""Tests for the vector path inside ``index_uploaded_file``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ee.cloud._core.realtime.events import FileReady
from ee.cloud.embeddings import EmbeddingResult
from ee.cloud.extraction.adapter import ExtractionResult


class _FakeChain:
    def __init__(self, result: ExtractionResult):
        self._result = result

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        return self._result


class _FakeAdapter:
    def __init__(self, *, local_path_value: Path | None = None):
        self._local = local_path_value

    def local_path(self, key: str) -> Path | None:
        return self._local

    async def open(self, key: str):
        if False:  # pragma: no cover
            yield b""


class _FakeEmbedder:
    name = "vertex-mm-001"
    dim = 1408
    supports_modalities = {"text", "image"}
    requires_network = True

    def __init__(self, *, fail: bool = False, vector: list[float] | None = None):
        self._fail = fail
        self._vector = vector or [0.1] * 8
        self.calls: list[tuple[Path, str]] = []

    async def embed_file(self, path: Path, mime: str) -> EmbeddingResult:
        self.calls.append((path, mime))
        if self._fail:
            raise RuntimeError("upstream blew up")
        return EmbeddingResult(
            vector=list(self._vector),
            dim=len(self._vector),
            model=self.name,
            estimated_cost_usd=0.0001,
        )

    async def embed_query(self, text, image_bytes=None):  # pragma: no cover
        raise NotImplementedError

    def estimate_cost(self, path, mime):
        return 0.0001


class _FakeCostTracker:
    def __init__(self, *, allow: bool = True, cap: float = 10.0):
        self._allow = allow
        self.cap_usd = cap
        self.spent_this_month = 0.0
        self.recorded: list[float] = []

    def can_spend(self, estimated_cost):
        return self._allow

    def record(self, cost):
        self.recorded.append(cost)


def _settings_with_vectors(**overrides):
    """Build a minimal stub settings object covering both pipelines."""
    base = {
        "kb_vectors_enabled": True,
        "embedding_adapter": "vertex-mm-001",
        "embedding_dim": 8,
        "embedding_monthly_cap_usd": 10.0,
        "vertex_project_id": "p",
        "vertex_location": "us-central1",
        "gemini_api_key": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def _patch_pipeline(
    monkeypatch,
    *,
    chain,
    adapter,
    ingest,
    embedder,
    cost_tracker,
    settings,
    subprocess_returncode: int = 0,
    subprocess_calls: list | None = None,
):
    from ee.cloud.uploads import listeners

    monkeypatch.setattr(
        "ee.cloud.extraction.build_chain", lambda settings: chain
    )
    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: adapter)

    from ee.cloud.agents import knowledge as kn

    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)

    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda force_reload=False: settings,
    )
    monkeypatch.setattr(
        "ee.cloud.embeddings.build_embedder",
        lambda s: embedder,
    )
    monkeypatch.setattr(
        "ee.cloud.embeddings.get_cost_tracker",
        lambda s: cost_tracker,
    )

    if subprocess_calls is not None:
        async def _fake_subprocess_exec(*args, **kwargs):
            subprocess_calls.append(list(args))

            class _Proc:
                returncode = subprocess_returncode

                async def communicate(self):
                    return b"{}", b""

            return _Proc()

        monkeypatch.setattr(
            "asyncio.create_subprocess_exec", _fake_subprocess_exec
        )


@pytest.mark.asyncio
async def test_vector_path_runs_when_enabled(monkeypatch, tmp_path):
    """Vectors enabled → embedder called → kb subprocess invoked with --vec."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "diagram.png"
    direct.write_bytes(b"\x89PNG\r\n\x1a\n")

    chain = _FakeChain(ExtractionResult(text="caption", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value={"id": "art-99"})
    embedder = _FakeEmbedder()
    cost_tracker = _FakeCostTracker(allow=True)
    settings = _settings_with_vectors()
    subprocess_calls: list = []

    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=embedder,
        cost_tracker=cost_tracker,
        settings=settings,
        subprocess_calls=subprocess_calls,
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "diagram.png",
                "mime": "image/png",
                "storage_key": "ws/w1/diagram.png",
            }
        )
    )

    # Text ingest happened first.
    ingest.assert_awaited_once_with(
        scope="workspace:w1", text="caption", source="diagram.png"
    )
    # Embedder ran on the same path the chain saw.
    assert embedder.calls == [(direct, "image/png")]
    # Cost was recorded.
    assert cost_tracker.recorded == [pytest.approx(0.0001)]
    # kb subprocess was called with the right shape.
    assert len(subprocess_calls) == 1
    argv = subprocess_calls[0]
    # asyncio.create_subprocess_exec args: (program, *flags)
    assert argv[1] == "ingest"
    assert "--vec" in argv
    vec_idx = argv.index("--vec") + 1
    vec_path = argv[vec_idx]
    # The temp vec file is unlinked by the time we observe; we can only
    # assert the shape, not contents. But the suffix tells the story.
    assert vec_path.endswith(".json")
    assert "--id" in argv
    id_idx = argv.index("--id") + 1
    assert argv[id_idx] == "art-99"
    assert "--scope" in argv
    scope_idx = argv.index("--scope") + 1
    assert argv[scope_idx] == "workspace:w1"


@pytest.mark.asyncio
async def test_cap_hit_skips_vector_but_keeps_text_ingest(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "x.png"
    direct.write_bytes(b"\x89PNG")

    chain = _FakeChain(ExtractionResult(text="text content", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value={"id": "art-1"})
    embedder = _FakeEmbedder()
    cost_tracker = _FakeCostTracker(allow=False)
    settings = _settings_with_vectors()
    subprocess_calls: list = []
    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=embedder,
        cost_tracker=cost_tracker,
        settings=settings,
        subprocess_calls=subprocess_calls,
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.png",
                "mime": "image/png",
                "storage_key": "k",
            }
        )
    )

    ingest.assert_awaited_once()
    assert embedder.calls == []  # cap blocked the embed call
    assert subprocess_calls == []  # …and the kb call


@pytest.mark.asyncio
async def test_vectors_disabled_skips_embed(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "x.png"
    direct.write_bytes(b"\x89PNG")
    chain = _FakeChain(ExtractionResult(text="text", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value={"id": "art-1"})
    embedder = _FakeEmbedder()
    settings = _settings_with_vectors(kb_vectors_enabled=False)
    subprocess_calls: list = []
    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=embedder,
        cost_tracker=_FakeCostTracker(),
        settings=settings,
        subprocess_calls=subprocess_calls,
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.png",
                "mime": "image/png",
                "storage_key": "k",
            }
        )
    )
    ingest.assert_awaited_once()
    assert embedder.calls == []
    assert subprocess_calls == []


@pytest.mark.asyncio
async def test_no_embedder_returned_skips_quietly(monkeypatch, tmp_path):
    """build_embedder → None → vector path is a no-op (logged at INFO)."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "x.png"
    direct.write_bytes(b"\x89PNG")
    chain = _FakeChain(ExtractionResult(text="text", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value={"id": "art-1"})
    settings = _settings_with_vectors()
    subprocess_calls: list = []
    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=None,
        cost_tracker=_FakeCostTracker(),
        settings=settings,
        subprocess_calls=subprocess_calls,
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.png",
                "mime": "image/png",
                "storage_key": "k",
            }
        )
    )
    ingest.assert_awaited_once()
    assert subprocess_calls == []


@pytest.mark.asyncio
async def test_modality_mismatch_skips_vector(monkeypatch, tmp_path):
    """Audio file but text+image-only embedder → vector path skipped."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "x.wav"
    direct.write_bytes(b"RIFF...")
    chain = _FakeChain(ExtractionResult(text="audio transcript", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value={"id": "art-1"})
    embedder = _FakeEmbedder()  # supports text/image only
    settings = _settings_with_vectors()
    subprocess_calls: list = []
    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=embedder,
        cost_tracker=_FakeCostTracker(),
        settings=settings,
        subprocess_calls=subprocess_calls,
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.wav",
                "mime": "audio/wav",
                "storage_key": "k",
            }
        )
    )
    ingest.assert_awaited_once()
    # WAV is "audio" modality; embedder only does text/image.
    assert embedder.calls == []
    assert subprocess_calls == []


@pytest.mark.asyncio
async def test_embed_failure_does_not_break_text_path(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "x.png"
    direct.write_bytes(b"\x89PNG")
    chain = _FakeChain(ExtractionResult(text="text", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value={"id": "art-1"})
    embedder = _FakeEmbedder(fail=True)
    settings = _settings_with_vectors()
    subprocess_calls: list = []
    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=embedder,
        cost_tracker=_FakeCostTracker(),
        settings=settings,
        subprocess_calls=subprocess_calls,
    )

    # Must not raise.
    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.png",
                "mime": "image/png",
                "storage_key": "k",
            }
        )
    )

    ingest.assert_awaited_once()
    assert embedder.calls != []
    # Subprocess never reached because embed failed.
    assert subprocess_calls == []


@pytest.mark.asyncio
async def test_kb_subprocess_failure_does_not_break_text_path(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "x.png"
    direct.write_bytes(b"\x89PNG")
    chain = _FakeChain(ExtractionResult(text="text", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value={"id": "art-1"})
    embedder = _FakeEmbedder()
    settings = _settings_with_vectors()
    subprocess_calls: list = []
    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=embedder,
        cost_tracker=_FakeCostTracker(),
        settings=settings,
        subprocess_calls=subprocess_calls,
        subprocess_returncode=2,
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.png",
                "mime": "image/png",
                "storage_key": "k",
            }
        )
    )
    ingest.assert_awaited_once()
    # Embedder ran, subprocess attempted, but exit-2 didn't propagate.
    assert embedder.calls != []
    assert len(subprocess_calls) == 1


@pytest.mark.asyncio
async def test_no_article_id_skips_vector(monkeypatch, tmp_path):
    """When kb-go ingest returns no id (legacy build), the vector path skips."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "x.png"
    direct.write_bytes(b"\x89PNG")
    chain = _FakeChain(ExtractionResult(text="text", backend="local"))
    adapter = _FakeAdapter(local_path_value=direct)
    ingest = AsyncMock(return_value="bare-stdout-string")  # not a dict
    embedder = _FakeEmbedder()
    settings = _settings_with_vectors()
    subprocess_calls: list = []
    _patch_pipeline(
        monkeypatch,
        chain=chain,
        adapter=adapter,
        ingest=ingest,
        embedder=embedder,
        cost_tracker=_FakeCostTracker(),
        settings=settings,
        subprocess_calls=subprocess_calls,
    )

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.png",
                "mime": "image/png",
                "storage_key": "k",
            }
        )
    )
    ingest.assert_awaited_once()
    assert embedder.calls == []
    assert subprocess_calls == []


def test_extract_article_id_helper():
    """Spot-check the parser used to read kb's ingest response."""
    from ee.cloud.uploads.listeners import _extract_article_id

    assert _extract_article_id({"id": "abc"}) == "abc"
    assert _extract_article_id({"article_id": "xyz"}) == "xyz"
    assert _extract_article_id({"id": 42}) is None
    assert _extract_article_id("nope") is None
    assert _extract_article_id(None) is None


def test_modality_for_mime_helper():
    from ee.cloud.uploads.listeners import _modality_for_mime

    assert _modality_for_mime("image/png") == "image"
    assert _modality_for_mime("application/pdf") == "pdf"
    assert _modality_for_mime("audio/wav") == "audio"
    assert _modality_for_mime("video/mp4") == "video"
    assert _modality_for_mime("text/plain") == "text"
    assert _modality_for_mime("application/json") == "text"
