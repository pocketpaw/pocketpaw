# chain.py — ExtractionChain runner + build_chain factory.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Routes a (path, mime) pair to the configured adapter chain with offline
# detection and per-MIME overrides. Always falls back to a network-free
# adapter on failure or offline conditions.
"""Extraction chain runner.

Selection order on `run(path, mime)`:
  1. If `per_mime_override` has an entry for `mime`, that adapter wins.
  2. Otherwise the first adapter in the chain whose `supports_mimes`
     contains `mime` (or the wildcard "*") is picked.
  3. Network-required adapters get skipped if `_is_online()` returns False.
  4. On adapter exception the chain falls through to the offline fallback.
  5. If nothing matches the mime at all, the offline fallback handles it.

The fallback is always available — it's wired to LocalExtractor in
`build_chain` regardless of what the user puts in `extraction_chain`.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from pocketpaw_ee.cloud.extraction.adapter import ExtractionAdapter, ExtractionResult

logger = logging.getLogger(__name__)


def _is_online() -> bool:
    """Cheap reachability check. Tests monkeypatch this."""
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=2):
            return True
    except OSError:
        return False


class ExtractionChain:
    def __init__(
        self,
        adapters: list[ExtractionAdapter],
        offline_fallback: ExtractionAdapter,
        per_mime_override: dict[str, str] | None = None,
    ) -> None:
        self._adapters = adapters
        self._offline_fallback = offline_fallback
        self._by_name: dict[str, ExtractionAdapter] = {
            a.name: a for a in [*adapters, offline_fallback]
        }
        self._per_mime = per_mime_override or {}

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        # 1. Per-MIME override wins.
        override = self._per_mime.get(mime)
        if override:
            adapter = self._by_name.get(override)
            if adapter is not None:
                return await self._call(adapter, path, mime)
            logger.warning("per-mime override %r unknown; falling through to chain", override)

        # 2. First chain adapter that supports the mime (wildcard or explicit).
        for adapter in self._adapters:
            if "*" in adapter.supports_mimes or mime in adapter.supports_mimes:
                return await self._call(adapter, path, mime)

        # 3. No adapter claimed this mime — let the fallback take it.
        return await self._offline_fallback.extract(path, mime)

    async def _call(self, adapter: ExtractionAdapter, path: Path, mime: str) -> ExtractionResult:
        if adapter.requires_network and not _is_online():
            logger.info(
                "adapter %s requires network but host is offline; using fallback",
                adapter.name,
            )
            return await self._offline_fallback.extract(path, mime)
        try:
            return await adapter.extract(path, mime)
        except Exception as exc:  # noqa: BLE001 — fall-through is the contract
            logger.warning("adapter %s failed for %s: %s", adapter.name, path, exc)
            if adapter is self._offline_fallback:
                raise
            return await self._offline_fallback.extract(path, mime)


def build_chain(settings) -> ExtractionChain:
    """Build the chain from `Settings`.

    Reads `settings.extraction_chain` (ordered list of adapter names),
    `settings.extraction_per_mime` (override map), and the offline fallback
    name. The fallback is always a `LocalExtractor`, regardless of what the
    chain config says — local is the only adapter guaranteed to work without
    network access.
    """
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor
    from pocketpaw_ee.cloud.extraction.local import LocalExtractor

    adapters: list[ExtractionAdapter] = []
    for name in settings.extraction_chain:
        if name == "local":
            adapters.append(LocalExtractor())
        elif name == "gemini-flash":
            api_key = getattr(settings, "gemini_api_key", None)
            if api_key:
                adapters.append(GeminiFlashExtractor(api_key))
            else:
                logger.info("gemini-flash adapter skipped: POCKETPAW_GEMINI_API_KEY not set")
        else:
            raise ValueError(f"unknown extraction adapter: {name!r}")

    fallback = LocalExtractor()
    return ExtractionChain(adapters, fallback, dict(settings.extraction_per_mime))
