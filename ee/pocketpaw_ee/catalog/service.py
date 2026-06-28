# ee/pocketpaw_ee/catalog/service.py — assembly + cache + filtering for the Model
# Catalog (MCG-1). The thin router delegates everything here.
#
# Assembly: read the LiteLLM proxy (litellm_client.fetch_entries), then, when
# enabled and available, merge best-effort models.dev enrichment over the gaps
# (description / logo / extra capabilities) — never overwriting a value LiteLLM
# already provided. The assembled list is TTL-cached IN-PROCESS
# (config.cache_ttl_seconds, default 300s) so a burst of requests hits the proxy
# once; ``bust_cache`` clears it on demand.
#
# Filtering: ``list_models`` applies modality / provider / capability (exact,
# case-insensitive) and ``q`` (substring over id + display_name + description),
# all combinable. ``get_model`` returns one entry by canonical id or None.
#
# Concurrency: a single asyncio.Lock serializes assembly so N concurrent
# cache-miss requests trigger exactly ONE upstream fetch (the rest await the lock
# and then read the now-warm cache) — this is what the TTL-cache test asserts.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1): the catalog service.

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from pocketpaw_ee.catalog import config
from pocketpaw_ee.catalog.litellm_client import LiteLLMClient
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry
from pocketpaw_ee.catalog.models_dev_client import ModelsDevClient


@dataclass
class _CacheState:
    """In-process cache for the assembled catalog. ``expires_at`` is a monotonic
    deadline; ``entries`` is None until the first successful assembly."""

    entries: list[ModelCatalogEntry] | None = None
    expires_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Module-level singleton cache — the catalog is deployment-global (not
# tenant-scoped), so one process-wide cache is correct.
_cache = _CacheState()


def bust_cache() -> None:
    """Invalidate the in-process catalog cache. The next list/get re-assembles
    from the proxy. Exposed for an admin/refresh path and for tests."""
    _cache.entries = None
    _cache.expires_at = 0.0


def _merge_enrichment(
    entries: list[ModelCatalogEntry],
    enrichment: dict[str, dict],
) -> list[ModelCatalogEntry]:
    """Fill gaps on each entry from the models.dev index WITHOUT overwriting
    anything LiteLLM already set. Only ``logo``/``description`` when currently
    None, and capabilities are unioned (deduped, sorted, deterministic)."""
    if not enrichment:
        return entries
    merged: list[ModelCatalogEntry] = []
    for entry in entries:
        extra = enrichment.get(entry.id)
        if not extra:
            merged.append(entry)
            continue
        patch: dict = {}
        if entry.logo is None and extra.get("logo"):
            patch["logo"] = extra["logo"]
        if entry.description is None and extra.get("description"):
            patch["description"] = extra["description"]
        extra_caps = extra.get("capabilities") or []
        if extra_caps:
            patch["capabilities"] = sorted(set(entry.capabilities) | set(extra_caps))
        merged.append(entry.model_copy(update=patch) if patch else entry)
    return merged


async def _assemble() -> list[ModelCatalogEntry]:
    """Read the proxy + (optional) models.dev and produce the merged catalog.

    LiteLLM is the source of truth (its failure propagates — the router maps it to
    a 502). models.dev is best-effort: disabled by flag or failing yields an empty
    index and the LiteLLM-only catalog passes through unchanged.
    """
    entries = await LiteLLMClient().fetch_entries()
    if config.models_dev_enabled():
        enrichment = await ModelsDevClient().fetch_index()
        entries = _merge_enrichment(entries, enrichment)
    # Stable order for deterministic output: by modality then id.
    entries.sort(key=lambda e: (e.modality.value, e.id))
    return entries


async def _get_catalog(force_refresh: bool = False) -> list[ModelCatalogEntry]:
    """Return the cached catalog, assembling (once) on miss/expiry.

    The lock guarantees a single in-flight assembly: the first miss assembles and
    warms the cache; concurrent callers await the lock, re-check the cache, and
    return the warm copy without a second upstream call.
    """
    now = time.monotonic()
    if not force_refresh and _cache.entries is not None and now < _cache.expires_at:
        return _cache.entries

    async with _cache.lock:
        # Re-check under the lock — another coroutine may have just filled it.
        now = time.monotonic()
        if not force_refresh and _cache.entries is not None and now < _cache.expires_at:
            return _cache.entries
        entries = await _assemble()
        _cache.entries = entries
        _cache.expires_at = time.monotonic() + config.cache_ttl_seconds()
        return entries


def _matches(
    entry: ModelCatalogEntry,
    *,
    modality: str | None,
    provider: str | None,
    q: str | None,
    capability: str | None,
) -> bool:
    """Combined predicate for the list filters. All supplied filters must pass
    (AND). modality/provider/capability are exact case-insensitive; q is a
    case-insensitive substring over id + display_name + description."""
    if modality and entry.modality.value != modality.strip().lower():
        return False
    if provider and entry.provider.lower() != provider.strip().lower():
        return False
    if capability and capability.strip().lower() not in {c.lower() for c in entry.capabilities}:
        return False
    if q:
        needle = q.strip().lower()
        haystack = " ".join(filter(None, [entry.id, entry.display_name, entry.description])).lower()
        if needle not in haystack:
            return False
    return True


async def list_models(
    *,
    modality: str | None = None,
    provider: str | None = None,
    q: str | None = None,
    capability: str | None = None,
    force_refresh: bool = False,
) -> list[ModelCatalogEntry]:
    """Return catalog entries matching every supplied filter (filters AND
    together; omitted filters match all)."""
    catalog = await _get_catalog(force_refresh=force_refresh)
    return [
        e
        for e in catalog
        if _matches(e, modality=modality, provider=provider, q=q, capability=capability)
    ]


async def get_model(model_id: str, *, force_refresh: bool = False) -> ModelCatalogEntry | None:
    """Return the single entry whose canonical id matches, or None."""
    catalog = await _get_catalog(force_refresh=force_refresh)
    for entry in catalog:
        if entry.id == model_id:
            return entry
    return None


# Re-export so callers can ``from ...service import Modality`` for query validation.
__all__ = [
    "list_models",
    "get_model",
    "bust_cache",
    "Modality",
]
