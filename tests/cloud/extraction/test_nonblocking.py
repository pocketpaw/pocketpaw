# test_nonblocking.py — extraction must never stall the shared event loop.
# LocalExtractor parses in a worker thread (a slow sync parser leaves a ticker
# coroutine running on time), the raw-text fallback reads at most
# _MAX_TEXT_BYTES, and the chain's blocking reachability probe is cached.
"""Event-loop and resource guards for the extraction layer."""

from __future__ import annotations

import asyncio
import time

import pytest
from pocketpaw_ee.cloud.extraction import chain as chain_mod
from pocketpaw_ee.cloud.extraction import local as local_mod
from pocketpaw_ee.cloud.extraction.local import LocalExtractor


@pytest.mark.asyncio
async def test_slow_parser_does_not_block_the_loop(monkeypatch, tmp_path):
    def _slow_parse(path, mime=""):
        time.sleep(0.6)  # a blocking library call, as pypdf/openpyxl are
        return "parsed"

    monkeypatch.setattr(local_mod, "_extract_text", _slow_parse)
    gaps: list[float] = []
    stop = asyncio.Event()

    async def _ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    result = await LocalExtractor().extract(tmp_path / "x.pdf", "application/pdf")
    stop.set()
    await ticker

    assert result.text == "parsed"
    assert len(gaps) > 10
    assert max(gaps) < 0.2


@pytest.mark.asyncio
async def test_fallback_read_is_capped(tmp_path):
    path = tmp_path / "blob.bin"
    limit = local_mod._MAX_TEXT_BYTES
    path.write_bytes(b"a" * (limit + 4096))

    result = await LocalExtractor().extract(path, "application/octet-stream")

    body, marker = result.text.split("\n[truncated:", 1)
    assert body == "a" * limit
    assert str(limit) in marker


@pytest.mark.asyncio
async def test_fallback_read_under_the_cap_is_whole(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("short note", encoding="utf-8")

    result = await LocalExtractor().extract(path, "text/plain")

    assert result.text == "short note"


def test_is_online_probe_is_cached(monkeypatch):
    calls = 0

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _connect(*_a, **_k):
        nonlocal calls
        calls += 1
        return _Conn()

    monkeypatch.setattr(chain_mod.socket, "create_connection", _connect)
    monkeypatch.setattr(chain_mod, "_online_cache", None)

    assert chain_mod._is_online() is True
    assert chain_mod._is_online() is True
    assert calls == 1

    # Past the TTL the probe runs again.
    stamped, value = chain_mod._online_cache
    monkeypatch.setattr(chain_mod, "_online_cache", (stamped - chain_mod._ONLINE_TTL_S - 1, value))
    chain_mod._is_online()
    assert calls == 2
