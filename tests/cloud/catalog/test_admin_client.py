# tests/cloud/catalog/test_admin_client.py — proves the LiteLLM proxy ADMIN client
# (MCG-8) wraps the management API correctly. httpx.MockTransport stands in for a
# live proxy (no network). Asserts:
#   * generate_key POSTs /key/generate with only the non-None budget / limit /
#     models / metadata fields and returns the proxy's body (incl. the new key).
#   * key_info GETs /key/info?key=.
#   * spend_logs GETs /spend/logs?api_key= (the required filter) and unwraps both
#     a bare-list and a {"data": [...]} response; an empty api_key raises.
#   * delete_keys POSTs /key/delete with the keys list.
#   * the master key rides as a Bearer header.
#   * a non-2xx surfaces LiteLLMAdminError with the proxy's error message.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8).

from __future__ import annotations

import json

import httpx
import pytest
from pocketpaw_ee.catalog.admin_client import LiteLLMAdminClient, LiteLLMAdminError

_BASE = "http://proxy.test:4000"


def _client(handler, *, api_key: str | None = None) -> LiteLLMAdminClient:
    return LiteLLMAdminClient(
        base_url=_BASE, api_key=api_key, _transport=httpx.MockTransport(handler)
    )


async def test_generate_key_posts_only_supplied_fields():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"key": "sk-tenant-abc", "max_budget": 25.0})

    client = _client(handler, api_key="sk-master")
    body = await client.generate_key(
        key_alias="ws-w1",
        max_budget=25.0,
        budget_duration="30d",
        rpm_limit=60,
        models=["anthropic/claude-3-5-sonnet"],
        metadata={"workspace_id": "w1"},
    )

    assert body["key"] == "sk-tenant-abc"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/key/generate")
    # The master key Bearers the privileged call.
    assert captured["auth"] == "Bearer sk-master"
    # Only the supplied fields are present (tpm_limit was omitted -> absent).
    assert captured["body"] == {
        "key_alias": "ws-w1",
        "max_budget": 25.0,
        "budget_duration": "30d",
        "rpm_limit": 60,
        "models": ["anthropic/claude-3-5-sonnet"],
        "metadata": {"workspace_id": "w1"},
    }
    assert "tpm_limit" not in captured["body"]


async def test_generate_key_omits_none_budget():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"key": "sk-tenant-xyz"})

    client = _client(handler)
    await client.generate_key(key_alias="ws-w2", metadata={"workspace_id": "w2"})
    # No budget / limits passed -> the proxy applies its own defaults; we send none.
    assert captured["body"] == {"key_alias": "ws-w2", "metadata": {"workspace_id": "w2"}}


async def test_key_info_gets_with_key_param():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"info": {"spend": 1.23, "max_budget": 25.0}})

    client = _client(handler)
    body = await client.key_info("sk-tenant-abc")
    assert "/key/info" in captured["url"]
    assert "key=sk-tenant-abc" in captured["url"]
    assert body["info"]["spend"] == 1.23


async def test_spend_logs_requires_api_key():
    client = _client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(LiteLLMAdminError):
        await client.spend_logs(api_key="")


async def test_spend_logs_unwraps_bare_list():
    rows = [
        {"request_id": "r1", "spend": 0.04, "startTime": "2026-06-26T10:00:00"},
        {"request_id": "r2", "spend": 0.01, "startTime": "2026-06-26T10:01:00"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "api_key=sk-tenant-abc" in str(request.url)
        return httpx.Response(200, json=rows)

    client = _client(handler)
    out = await client.spend_logs(api_key="sk-tenant-abc")
    assert out == rows


async def test_spend_logs_unwraps_data_envelope():
    rows = [{"request_id": "r1", "spend": 0.04}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": rows})

    client = _client(handler)
    out = await client.spend_logs(api_key="sk-tenant-abc")
    assert out == rows


async def test_delete_keys_posts_keys_list():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"deleted_keys": ["sk-tenant-abc"]})

    client = _client(handler)
    await client.delete_keys(["sk-tenant-abc"])
    assert captured["url"].endswith("/key/delete")
    assert captured["body"] == {"keys": ["sk-tenant-abc"]}


async def test_non_2xx_raises_admin_error_with_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid master key"}})

    client = _client(handler)
    with pytest.raises(LiteLLMAdminError) as exc:
        await client.generate_key(key_alias="ws-w1")
    assert "401" in str(exc.value)
    assert "Invalid master key" in str(exc.value)
