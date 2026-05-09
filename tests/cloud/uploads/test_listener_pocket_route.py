# test_listener_pocket_route.py — listener pocket-scope routing tests.
# Created: 2026-05-03 — Stage 3.E "Files as Knowledge". Verifies that
# FileReady events with ``pocket_id`` route into ``pocket:{id}`` and
# events without ``pocket_id`` keep the Stage 1.B ``workspace:{wid}``
# behaviour as a regression test.
"""Listener routes ``FileReady`` to the right kb-go scope."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ee.cloud._core.realtime.events import FileReady
from ee.cloud.extraction.adapter import ExtractionResult


class _FakeChain:
    def __init__(self, result: ExtractionResult):
        self._result = result
        self.calls: list[tuple[Path, str]] = []

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        self.calls.append((path, mime))
        return self._result


def _patch(monkeypatch, *, chain, storage_path: Path, ingest):
    from ee.cloud.uploads import listeners

    monkeypatch.setattr("ee.cloud.extraction.build_chain", lambda settings: chain)

    class _Adapter:
        def local_path(self, _key: str) -> Path:
            return storage_path

        async def open(self, _key: str):
            yield b""

    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: _Adapter())

    from ee.cloud.agents import knowledge as kn

    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)


@pytest.mark.asyncio
async def test_pocket_id_routes_to_pocket_scope(monkeypatch, tmp_path):
    """FileReady carries ``pocket_id`` → KB ingest scope is ``pocket:{id}``."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    fake_path = tmp_path / "deck.pdf"
    fake_path.write_bytes(b"unused; chain.run is mocked")

    chain = _FakeChain(ExtractionResult(text="slide content", backend="local"))
    ingest = AsyncMock(return_value={"id": "art-pocket-1"})
    _patch(monkeypatch, chain=chain, storage_path=fake_path, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "pocket_id": "PA",
                "file_id": "f1",
                "filename": "deck.pdf",
                "mime": "application/pdf",
                "storage_key": "ws/w1/f1.pdf",
            }
        )
    )

    ingest.assert_awaited_once_with(
        scope="pocket:PA",
        text="slide content",
        source="deck.pdf",
    )


@pytest.mark.asyncio
async def test_no_pocket_id_keeps_workspace_scope(monkeypatch, tmp_path):
    """Stage 1.B regression: no ``pocket_id`` → ``workspace:{wid}`` scope."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    fake_path = tmp_path / "doc.pdf"
    fake_path.write_bytes(b"unused")

    chain = _FakeChain(ExtractionResult(text="hello", backend="local"))
    ingest = AsyncMock(return_value={"id": "art-1"})
    _patch(monkeypatch, chain=chain, storage_path=fake_path, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "doc.pdf",
                "mime": "application/pdf",
                "storage_key": "ws/w1/f1.pdf",
            }
        )
    )

    ingest.assert_awaited_once_with(
        scope="workspace:w1",
        text="hello",
        source="doc.pdf",
    )


@pytest.mark.asyncio
async def test_empty_pocket_id_falls_back_to_workspace(monkeypatch, tmp_path):
    """Defensive: ``pocket_id=""`` is treated as no pocket (truthy check)."""
    from ee.cloud.uploads.listeners import index_uploaded_file

    fake_path = tmp_path / "doc.pdf"
    fake_path.write_bytes(b"unused")

    chain = _FakeChain(ExtractionResult(text="hello", backend="local"))
    ingest = AsyncMock(return_value={"id": "art-1"})
    _patch(monkeypatch, chain=chain, storage_path=fake_path, ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "pocket_id": "",
                "file_id": "f1",
                "filename": "doc.pdf",
                "mime": "application/pdf",
                "storage_key": "ws/w1/f1.pdf",
            }
        )
    )

    ingest.assert_awaited_once_with(
        scope="workspace:w1",
        text="hello",
        source="doc.pdf",
    )
