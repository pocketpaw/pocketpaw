# tests/cloud/catalog/test_models_dev_client.py — Model Catalog (MCG-1)
# best-effort models.dev enrichment client. httpx.MockTransport stands in for
# models.dev (no network). Asserts:
#   * a well-formed api.json flattens to {"<provider>/<model>": {description,
#     logo, capabilities}}.
#   * a non-2xx response fails OPEN (empty dict, no raise).
#   * a transport error fails OPEN (empty dict, no raise) — a models.dev outage
#     can never break the catalog.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1).

from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.catalog.models_dev_client import ModelsDevClient

_API_JSON = {
    "anthropic": {
        "logo": "https://models.dev/logos/anthropic.svg",
        "models": {
            "claude-3-5-sonnet": {
                "name": "Claude 3.5 Sonnet",
                "description": "Anthropic's mid-tier chat model",
                "capabilities": ["vision", "tool_call"],
            }
        },
    }
}


@pytest.mark.asyncio
async def test_fetch_index_flattens_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_API_JSON)

    client = ModelsDevClient(_transport=httpx.MockTransport(handler))
    index = await client.fetch_index()
    assert "anthropic/claude-3-5-sonnet" in index
    entry = index["anthropic/claude-3-5-sonnet"]
    assert entry["description"] == "Anthropic's mid-tier chat model"
    assert entry["logo"] == "https://models.dev/logos/anthropic.svg"  # provider-level fallback
    assert set(entry["capabilities"]) == {"vision", "tool_call"}


@pytest.mark.asyncio
async def test_non_2xx_fails_open():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = ModelsDevClient(_transport=httpx.MockTransport(handler))
    assert await client.fetch_index() == {}


@pytest.mark.asyncio
async def test_transport_error_fails_open():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    client = ModelsDevClient(_transport=httpx.MockTransport(handler))
    assert await client.fetch_index() == {}
