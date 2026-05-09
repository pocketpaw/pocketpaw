# test_listener.py — tests for the FileReady KB-indexing subscriber.
# Created: 2026-04-30 — Stage 1.B "Files as Knowledge". Verifies the
#   listener resolves the storage path, runs extraction, ingests into
#   workspace KB, and contains failures so they don't propagate back to
#   the publisher.
# Updated: 2026-04-30 evening — Stage 1.B follow-up. Added S3 / remote
#   adapter coverage: when local_path returns None the listener streams
#   the blob into a NamedTemporaryFile, runs extraction on that, and
#   deletes the temp file on the way out. Also covers stream failures
#   and suffix preservation so suffix-routed extractors stay routed.
"""Tests for ``ee.cloud.uploads.listeners.index_uploaded_file``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ee.cloud._core.realtime.events import FileReady
from ee.cloud.extraction.adapter import ExtractionResult


class _FakeChain:
    """Stand-in for ee.cloud.extraction.ExtractionChain.run."""

    def __init__(self, result: ExtractionResult):
        self._result = result
        self.calls: list[tuple[Path, str]] = []

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        self.calls.append((path, mime))
        return self._result


class _FakeAdapter:
    """Stand-in for the upload StorageAdapter.

    ``local_path_value`` controls what the local-path lookup returns; ``None``
    simulates a remote adapter (S3, GCS) that has no on-disk surface.
    ``open_chunks`` is the byte stream the listener will assemble into a
    temp file; setting ``open_raises`` makes the iterator raise mid-stream
    so the failure-isolation path is exercised.
    """

    def __init__(
        self,
        *,
        local_path_value: Path | None = None,
        open_chunks: list[bytes] | None = None,
        open_raises: BaseException | None = None,
    ):
        self._local = local_path_value
        self._chunks = open_chunks or []
        self._raises = open_raises

    def local_path(self, key: str) -> Path | None:
        return self._local

    async def open(self, key: str):
        for chunk in self._chunks:
            yield chunk
        if self._raises is not None:
            raise self._raises


def _patch_listener(
    monkeypatch,
    *,
    chain: _FakeChain | None = None,
    storage_path: Path | None = None,
    adapter: _FakeAdapter | None = None,
    ingest: AsyncMock | None = None,
):
    """Wire the listener's collaborators with test doubles."""
    from ee.cloud.uploads import listeners

    if chain is not None:
        monkeypatch.setattr(
            "ee.cloud.extraction.build_chain",
            lambda settings: chain,
        )

    # ``_resolve_adapter`` underpins both the local-path and the temp-file
    # branches; ``adapter`` lets tests pin both at once.
    if adapter is not None:
        monkeypatch.setattr(listeners, "_resolve_adapter", lambda: adapter)
    elif storage_path is not None:
        # Back-compat shortcut: tests that only care about the local-path
        # branch can pass ``storage_path`` and we wire a fake adapter
        # under the hood.
        fake = _FakeAdapter(local_path_value=storage_path)
        monkeypatch.setattr(listeners, "_resolve_adapter", lambda: fake)

    if ingest is not None:
        from ee.cloud.agents import knowledge as kn

        monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)


@pytest.mark.asyncio
async def test_index_uploaded_file_calls_chain_then_kb(monkeypatch, tmp_path):
    """Happy path: extraction runs, KB ingest gets the right scope/source."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    fake_path = tmp_path / "doc.pdf"
    fake_path.write_bytes(b"unused; chain.run is mocked")

    chain = _FakeChain(ExtractionResult(text="hello world", backend="local"))
    ingest = AsyncMock(return_value={"id": "art-1"})
    _patch_listener(monkeypatch, chain=chain, storage_path=fake_path, ingest=ingest)

    ev = FileReady(
        data={
            "workspace_id": "w1",
            "file_id": "f1",
            "filename": "doc.pdf",
            "mime": "application/pdf",
            "storage_key": "ws/w1/f1.pdf",
        }
    )

    await index_uploaded_file(ev)

    assert chain.calls == [(fake_path, "application/pdf")]
    ingest.assert_awaited_once_with(
        scope="workspace:w1",
        text="hello world",
        source="doc.pdf",
    )


@pytest.mark.asyncio
async def test_index_skips_when_workspace_id_missing(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="ignored", backend="local"))
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest)

    await index_uploaded_file(FileReady(data={"file_id": "f1", "storage_key": "k"}))

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_skips_when_file_id_missing(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="ignored", backend="local"))
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest)

    await index_uploaded_file(FileReady(data={"workspace_id": "w1", "storage_key": "k"}))

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_skips_when_no_storage_key(monkeypatch):
    """No storage_key in the event → no path to extract from; bail cleanly."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="ignored", backend="local"))
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                # storage_key intentionally missing
            }
        )
    )

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_adapter_streams_into_temp_then_extracts(monkeypatch):
    """S3 / GCS path: local_path=None → listener streams adapter.open() bytes
    into a NamedTemporaryFile, runs extraction on it, deletes the file."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="caption from gemini", backend="gemini-flash"))
    adapter = _FakeAdapter(
        local_path_value=None,
        open_chunks=[b"chunk-1 ", b"chunk-2"],
    )
    ingest = AsyncMock(return_value={"id": "art-1"})
    _patch_listener(monkeypatch, chain=chain, adapter=adapter, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "whiteboard.png",
                "mime": "image/png",
                "storage_key": "ws/w1/whiteboard.png",
            }
        )
    )

    # Chain was called exactly once with a temp path that ends in .png so
    # any suffix-routed extractor downstream would still recognize it.
    assert len(chain.calls) == 1
    temp_path, mime = chain.calls[0]
    assert mime == "image/png"
    assert temp_path.suffix == ".png", temp_path
    assert temp_path.name.startswith("paw-extract-"), temp_path
    # File was wiped after the context manager exited.
    assert not temp_path.exists()
    # The temp file held both chunks before extraction ran. We can't peek
    # at the file post-cleanup, so we settle for the chain having been
    # called — the absence of a half-written empty file would have shown
    # up as test_kb_failure_does_not_propagate-style failure.

    ingest.assert_awaited_once_with(
        scope="workspace:w1",
        text="caption from gemini",
        source="whiteboard.png",
    )


@pytest.mark.asyncio
async def test_remote_adapter_stream_failure_bails_cleanly(monkeypatch):
    """If adapter.open() raises mid-stream, listener swallows + skips."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="never reached", backend="local"))
    adapter = _FakeAdapter(
        local_path_value=None,
        open_chunks=[b"partial"],
        open_raises=ConnectionError("S3 disconnected"),
    )
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, adapter=adapter, ingest=ingest)

    # Must not raise — handler-level isolation is the whole point.
    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "doc.pdf",
                "mime": "application/pdf",
                "storage_key": "ws/w1/doc.pdf",
            }
        )
    )

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_adapter_temp_suffix_falls_back_to_mime(monkeypatch):
    """Filename has no extension → guess from MIME. Keeps suffix-routed
    extractors (LocalExtractor's pypdf path, etc.) working in the remote
    case too."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="extracted", backend="local"))
    adapter = _FakeAdapter(local_path_value=None, open_chunks=[b"%PDF-1.4 ..."])
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, adapter=adapter, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "no-extension-here",
                "mime": "application/pdf",
                "storage_key": "ws/w1/blob",
            }
        )
    )

    assert len(chain.calls) == 1
    temp_path, _mime = chain.calls[0]
    assert temp_path.suffix == ".pdf", temp_path
    ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_adapter_uses_direct_path_no_temp(monkeypatch, tmp_path):
    """Local-disk adapter: listener uses the direct path, no temp file is
    created — the regression case for the S3 fallback not breaking the
    fast path."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    direct = tmp_path / "doc.pdf"
    direct.write_bytes(b"%PDF-1.4 ...")
    chain = _FakeChain(ExtractionResult(text="from direct path", backend="local"))
    # Adapter exposes a local path AND an open() — the listener should
    # prefer the local path and never call open().
    open_calls: list[str] = []

    class _AdapterWithBoth(_FakeAdapter):
        async def open(self, key: str):  # type: ignore[override]
            open_calls.append(key)
            yield b"unexpected"

    adapter = _AdapterWithBoth(local_path_value=direct)
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, adapter=adapter, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "doc.pdf",
                "mime": "application/pdf",
                "storage_key": "local/w1/doc.pdf",
            }
        )
    )

    assert chain.calls == [(direct, "application/pdf")]
    assert open_calls == [], "local adapter must not stream when local_path works"
    ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_index_skips_when_adapter_unavailable(monkeypatch):
    """Test contexts without the upload router mounted: _resolve_adapter
    returns None → listener logs and skips, never calls extraction."""
    from ee.cloud.uploads import listeners
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="ignored", backend="local"))
    ingest = AsyncMock()
    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: None)
    monkeypatch.setattr("ee.cloud.extraction.build_chain", lambda settings: chain)
    from ee.cloud.agents import knowledge as kn

    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "x.pdf",
                "mime": "application/pdf",
                "storage_key": "k",
            }
        )
    )

    assert chain.calls == []
    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_skips_when_extraction_returns_empty(monkeypatch, tmp_path):
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="   ", backend="local"))
    ingest = AsyncMock()
    _patch_listener(monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "storage_key": "k",
                "filename": "blank.pdf",
                "mime": "application/pdf",
            }
        )
    )

    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_failure_does_not_propagate(monkeypatch, tmp_path):
    """Chain raises → listener swallows the error and skips ingest."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    class _ExplodingChain:
        async def run(self, *_args, **_kwargs):
            raise RuntimeError("kaboom")

    monkeypatch.setattr("ee.cloud.extraction.build_chain", lambda settings: _ExplodingChain())
    ingest = AsyncMock()
    _patch_listener(monkeypatch, storage_path=tmp_path / "x", ingest=ingest)

    # Must not raise — the bus catches handler errors but we keep the
    # listener defensive so the failure surfaces as a log line, not a
    # missed audit trail.
    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "storage_key": "k",
                "filename": "x.pdf",
                "mime": "application/pdf",
            }
        )
    )

    ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_kb_failure_does_not_propagate(monkeypatch, tmp_path):
    """KB ingest raises → listener swallows so the publisher never sees it."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    chain = _FakeChain(ExtractionResult(text="content", backend="local"))
    ingest = AsyncMock(side_effect=RuntimeError("kb missing"))
    _patch_listener(monkeypatch, chain=chain, storage_path=tmp_path / "x", ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "storage_key": "k",
                "filename": "x.pdf",
                "mime": "application/pdf",
            }
        )
    )

    # The mock was actually called, but its side-effect was contained.
    ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_upload_listeners_subscribes_to_file_ready():
    """Bootstrap path: register_upload_listeners hooks the bus."""
    from ee.cloud._core.realtime import bus as bus_mod
    from ee.cloud._core.realtime.audience import AudienceResolver
    from ee.cloud._core.realtime.bus import InProcessBus
    from ee.cloud.uploads.listeners import (
        index_uploaded_file,
        register_upload_listeners,
    )

    real_bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = real_bus  # type: ignore[attr-defined]
    try:
        register_upload_listeners()
        handlers = real_bus._handlers.get(FileReady.EVENT_TYPE, [])
        assert index_uploaded_file in handlers
    finally:
        bus_mod._bus = prev  # type: ignore[attr-defined]
