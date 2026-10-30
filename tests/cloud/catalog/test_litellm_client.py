# tests/cloud/catalog/test_litellm_client.py — Model Catalog (MCG-1) proxy
# client + mapper tests. httpx.MockTransport stands in for a live LiteLLM proxy
# (no network). Asserts:
#   * map_model_info_row produces a correct ModelCatalogEntry (id/provider/
#     modality/context/max_output/pricing-per-mtok/capabilities).
#   * LiteLLM ``mode`` -> our Modality across every bucket (chat family, embedding,
#     image, audio_tts, audio_stt, video) + unknown -> chat default.
#   * fetch_entries joins /model/info + /v1/models so catalog ⊇ routable (a served
#     id missing from /model/info is still listed).
#   * a non-2xx proxy read fails closed (CatalogUpstreamError).
#   * the proxy api key rides as a Bearer header when configured.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1).

from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.catalog.litellm_client import (
    CatalogUpstreamError,
    LiteLLMClient,
    map_model_info_row,
)
from pocketpaw_ee.catalog.models import Modality

# A representative /model/info row (the rich anthropic chat model).
_SONNET_ROW = {
    "model_name": "anthropic/claude-3-5-sonnet",
    "litellm_params": {"model": "anthropic/claude-3-5-sonnet-20241022"},
    "model_info": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "max_input_tokens": 200000,
        "max_output_tokens": 8192,
        "input_cost_per_token": 0.000003,  # $3 / Mtok
        "output_cost_per_token": 0.000015,  # $15 / Mtok
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_response_schema": True,
        "supports_prompt_caching": True,
    },
}


def _client(handler) -> LiteLLMClient:
    return LiteLLMClient(
        base_url="http://proxy.test:4000",
        api_key=None,
        _transport=httpx.MockTransport(handler),
    )


def test_map_model_info_row_builds_entry():
    entry = map_model_info_row(_SONNET_ROW)
    assert entry is not None
    assert entry.id == "anthropic/claude-3-5-sonnet"
    assert entry.provider == "anthropic"
    assert entry.display_name == "claude-3-5-sonnet"
    assert entry.modality is Modality.CHAT
    assert entry.context == 200000
    assert entry.max_output_tokens == 8192
    # per-token -> per-MILLION-token.
    assert entry.pricing is not None
    assert entry.pricing.input_per_mtok == 3.0
    assert entry.pricing.output_per_mtok == 15.0
    # supports_* -> capability tokens; chat models also get "streaming".
    assert "tool_call" in entry.capabilities
    assert "vision" in entry.capabilities
    assert "json_mode" in entry.capabilities
    assert "prompt_caching" in entry.capabilities
    assert "streaming" in entry.capabilities
    assert entry.status == "available"


def test_map_model_info_row_no_model_name_dropped():
    assert map_model_info_row({"model_info": {"mode": "chat"}}) is None


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("chat", Modality.CHAT),
        ("completion", Modality.CHAT),
        ("responses", Modality.CHAT),
        ("embedding", Modality.EMBEDDING),
        ("image_generation", Modality.IMAGE),
        ("audio_speech", Modality.AUDIO_TTS),
        ("audio_transcription", Modality.AUDIO_STT),
        ("video", Modality.VIDEO),
        ("something_new", Modality.CHAT),  # unknown -> chat default
        (None, Modality.CHAT),
    ],
)
def test_mode_maps_to_modality(mode, expected):
    row = {"model_name": "p/m", "model_info": {"mode": mode} if mode is not None else {}}
    entry = map_model_info_row(row)
    assert entry is not None
    assert entry.modality is expected


def test_unknown_cost_yields_no_pricing():
    row = {"model_name": "p/m", "model_info": {"mode": "embedding"}}
    entry = map_model_info_row(row)
    assert entry is not None
    assert entry.pricing is None


@pytest.mark.asyncio
async def test_fetch_entries_maps_model_info():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": [_SONNET_ROW]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "anthropic/claude-3-5-sonnet"}]})
        return httpx.Response(404)

    entries = await _client(handler).fetch_entries()
    assert len(entries) == 1
    assert entries[0].id == "anthropic/claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_fetch_entries_catalog_superset_of_routable():
    """A model served by /v1/models but absent from /model/info is STILL listed
    (catalog ⊇ routable) as a minimal available entry."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": [_SONNET_ROW]})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "anthropic/claude-3-5-sonnet"},
                        {"id": "openai/gpt-4o"},  # served but undescribed
                    ]
                },
            )
        return httpx.Response(404)

    entries = await _client(handler).fetch_entries()
    ids = {e.id for e in entries}
    assert ids == {"anthropic/claude-3-5-sonnet", "openai/gpt-4o"}
    gpt = next(e for e in entries if e.id == "openai/gpt-4o")
    assert gpt.provider == "openai"
    assert gpt.status == "available"


@pytest.mark.asyncio
async def test_model_info_non_2xx_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    with pytest.raises(CatalogUpstreamError):
        await _client(handler).fetch_entries()


@pytest.mark.asyncio
async def test_v1_models_failure_does_not_blank_catalog():
    """If /model/info succeeds but /v1/models fails, the catalog still serves the
    described entries (reconciliation is best-effort, not blocking)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": [_SONNET_ROW]})
        return httpx.Response(500)  # /v1/models down

    entries = await _client(handler).fetch_entries()
    assert {e.id for e in entries} == {"anthropic/claude-3-5-sonnet"}


@pytest.mark.asyncio
async def test_api_key_sent_as_bearer():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": []})

    client = LiteLLMClient(
        base_url="http://proxy.test:4000",
        api_key="sk-proxy-123",
        _transport=httpx.MockTransport(handler),
    )
    await client.model_info()
    assert seen["auth"] == "Bearer sk-proxy-123"
