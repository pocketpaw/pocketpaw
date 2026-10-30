# tests/cloud/catalog/test_service.py — Model Catalog (MCG-1) service tests:
# filtering, TTL cache, and best-effort models.dev enrichment merge.
# The upstream clients (LiteLLMClient / ModelsDevClient) are replaced with fakes
# injected via monkeypatch so no network is touched. Asserts:
#   * modality / provider / capability / q filters (each + combined).
#   * the TTL cache: a second request inside the window does NOT re-hit the proxy
#     (the fetch counter stays at 1), and bust_cache forces a re-fetch.
#   * models.dev enrichment fills gaps WITHOUT overwriting LiteLLM values.
#   * enrichment is fail-open: an empty index leaves the catalog unchanged.
#   * the models.dev toggle (CATALOG_MODELS_DEV_ENABLED=false) skips enrichment.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1).

from __future__ import annotations

import pytest
from pocketpaw_ee.catalog import service
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry, Pricing


def _entry(
    id_: str,
    *,
    provider: str,
    modality: Modality,
    caps: list[str] | None = None,
    description: str | None = None,
    logo: str | None = None,
) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id=id_,
        display_name=id_.split("/", 1)[-1],
        provider=provider,
        modality=modality,
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=2.0),
        capabilities=caps or [],
        description=description,
        logo=logo,
    )


_CATALOG = [
    _entry(
        "anthropic/claude-3-5-sonnet",
        provider="anthropic",
        modality=Modality.CHAT,
        caps=["tool_call", "vision", "streaming"],
    ),
    _entry(
        "openai/gpt-4o", provider="openai", modality=Modality.CHAT, caps=["tool_call", "streaming"]
    ),
    _entry("openai/text-embedding-3-small", provider="openai", modality=Modality.EMBEDDING),
    _entry("google/imagen-3", provider="google", modality=Modality.IMAGE),
]


class _FakeLiteLLM:
    """Counts fetches so the TTL-cache test can assert a single upstream call."""

    calls = 0

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401
        pass

    async def fetch_entries(self) -> list[ModelCatalogEntry]:
        type(self).calls += 1
        # Return fresh copies so the service's in-place sort never mutates _CATALOG.
        return [e.model_copy(deep=True) for e in _CATALOG]


class _FakeModelsDev:
    """Returns a fixed enrichment index (overridable per-test)."""

    index: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def fetch_index(self) -> dict:
        return dict(type(self).index)


@pytest.fixture(autouse=True)
def wire_fakes(monkeypatch: pytest.MonkeyPatch):
    """Inject the fakes, reset counters + cache, and default enrichment off so a
    test opts INTO models.dev explicitly."""
    _FakeLiteLLM.calls = 0
    _FakeModelsDev.index = {}
    monkeypatch.setattr(service, "LiteLLMClient", _FakeLiteLLM)
    monkeypatch.setattr(service, "ModelsDevClient", _FakeModelsDev)
    monkeypatch.setenv("CATALOG_MODELS_DEV_ENABLED", "false")
    monkeypatch.setenv("CATALOG_CACHE_TTL_SECONDS", "300")
    service.bust_cache()
    yield
    service.bust_cache()


# --- filtering --------------------------------------------------------------


async def test_no_filter_returns_all():
    entries = await service.list_models()
    assert len(entries) == len(_CATALOG)


async def test_filter_by_modality():
    chat = await service.list_models(modality="chat")
    assert {e.id for e in chat} == {"anthropic/claude-3-5-sonnet", "openai/gpt-4o"}
    emb = await service.list_models(modality="embedding")
    assert {e.id for e in emb} == {"openai/text-embedding-3-small"}


async def test_filter_by_provider_case_insensitive():
    res = await service.list_models(provider="OpenAI")
    assert {e.id for e in res} == {"openai/gpt-4o", "openai/text-embedding-3-small"}


async def test_filter_by_capability():
    res = await service.list_models(capability="vision")
    assert {e.id for e in res} == {"anthropic/claude-3-5-sonnet"}


async def test_filter_by_q_substring():
    res = await service.list_models(q="sonnet")
    assert {e.id for e in res} == {"anthropic/claude-3-5-sonnet"}


async def test_filters_combine_and():
    # chat AND provider=openai -> gpt-4o only (embedding is openai but not chat).
    res = await service.list_models(modality="chat", provider="openai")
    assert {e.id for e in res} == {"openai/gpt-4o"}


async def test_get_model_by_id():
    entry = await service.get_model("google/imagen-3")
    assert entry is not None
    assert entry.modality is Modality.IMAGE
    assert await service.get_model("nope/none") is None


# --- TTL cache --------------------------------------------------------------


async def test_cache_avoids_second_upstream_call():
    await service.list_models()
    await service.list_models(modality="chat")
    await service.get_model("openai/gpt-4o")
    # Three service reads, ONE upstream fetch — the TTL cache served the rest.
    assert _FakeLiteLLM.calls == 1


async def test_bust_cache_forces_refetch():
    await service.list_models()
    assert _FakeLiteLLM.calls == 1
    service.bust_cache()
    await service.list_models()
    assert _FakeLiteLLM.calls == 2


async def test_expired_ttl_refetches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CATALOG_CACHE_TTL_SECONDS", "300")
    service.bust_cache()

    # Drive a controllable monotonic clock.
    clock = {"t": 1000.0}
    monkeypatch.setattr(service.time, "monotonic", lambda: clock["t"])

    await service.list_models()
    assert _FakeLiteLLM.calls == 1
    clock["t"] += 301  # past the 300s TTL
    await service.list_models()
    assert _FakeLiteLLM.calls == 2


# --- models.dev enrichment --------------------------------------------------


async def test_enrichment_fills_gaps_without_overwrite(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CATALOG_MODELS_DEV_ENABLED", "true")
    _FakeModelsDev.index = {
        "openai/gpt-4o": {
            "description": "GPT-4o omni model",
            "logo": "https://logos.test/openai.svg",
            "capabilities": ["json_mode"],  # not in the LiteLLM-derived set
        },
        "anthropic/claude-3-5-sonnet": {
            "description": "should NOT overwrite — but LiteLLM left it None",
            "capabilities": [],
        },
    }
    service.bust_cache()
    gpt = await service.get_model("openai/gpt-4o")
    assert gpt is not None
    assert gpt.description == "GPT-4o omni model"
    assert gpt.logo == "https://logos.test/openai.svg"
    # union of LiteLLM caps + the enrichment cap.
    assert "json_mode" in gpt.capabilities
    assert "tool_call" in gpt.capabilities


async def test_enrichment_disabled_by_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CATALOG_MODELS_DEV_ENABLED", "false")
    _FakeModelsDev.index = {"openai/gpt-4o": {"description": "ignored"}}
    service.bust_cache()
    gpt = await service.get_model("openai/gpt-4o")
    assert gpt is not None
    assert gpt.description is None  # enrichment was skipped


async def test_enrichment_empty_index_is_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CATALOG_MODELS_DEV_ENABLED", "true")
    _FakeModelsDev.index = {}  # models.dev down -> fail-open empty
    service.bust_cache()
    entries = await service.list_models()
    assert len(entries) == len(_CATALOG)  # unchanged, LiteLLM-only
