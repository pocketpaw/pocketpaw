# tests/cloud/catalog/test_router.py — Model Catalog (MCG-1) HTTP-layer tests.
# A FastAPI app mounts the catalog router with the license dep waived; the catalog
# SERVICE is patched (the router is a thin adapter, so the service is the seam).
# Asserts:
#   * GET /catalog/models returns {models, total} and forwards filter params.
#   * an unknown modality value is a 400 (clear error, not a silent empty list).
#   * GET /catalog/models/{id} resolves the URL-ENCODED "<provider>/<model>" id
#     (the %2F-encoded slash) to one entry; a miss is 404.
#   * a LiteLLM upstream failure (CatalogUpstreamError) surfaces as 502.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1).

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.catalog import service as catalog_service
from pocketpaw_ee.catalog.litellm_client import CatalogUpstreamError
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry, Pricing
from pocketpaw_ee.catalog.router import router as catalog_router
from pocketpaw_ee.cloud.license import require_license

_SONNET = ModelCatalogEntry(
    id="anthropic/claude-3-5-sonnet",
    display_name="claude-3-5-sonnet",
    provider="anthropic",
    modality=Modality.CHAT,
    context=200000,
    max_output_tokens=8192,
    pricing=Pricing(input_per_mtok=3.0, output_per_mtok=15.0),
    capabilities=["tool_call", "vision", "streaming"],
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(catalog_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_list_models_returns_envelope(client, monkeypatch):
    seen: dict = {}

    async def _list(**kwargs):
        seen.update(kwargs)
        return [_SONNET]

    monkeypatch.setattr(catalog_service, "list_models", _list)

    resp = client.get(
        "/api/v1/catalog/models",
        params={"modality": "chat", "provider": "anthropic", "q": "son", "capability": "vision"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["models"][0]["id"] == "anthropic/claude-3-5-sonnet"
    assert body["models"][0]["pricing"]["input_per_mtok"] == 3.0
    # filters forwarded to the service.
    assert seen == {
        "modality": "chat",
        "provider": "anthropic",
        "q": "son",
        "capability": "vision",
    }


def test_list_models_unknown_modality_is_400(client):
    resp = client.get("/api/v1/catalog/models", params={"modality": "music"})
    assert resp.status_code == 400
    assert "Unknown modality" in resp.json()["detail"]


def test_list_models_upstream_failure_is_502(client, monkeypatch):
    async def _boom(**kwargs):
        raise CatalogUpstreamError("proxy down")

    monkeypatch.setattr(catalog_service, "list_models", _boom)
    resp = client.get("/api/v1/catalog/models")
    assert resp.status_code == 502


def test_get_model_url_encoded_id(client, monkeypatch):
    seen: dict = {}

    async def _get(model_id, **kwargs):
        seen["id"] = model_id
        return _SONNET

    monkeypatch.setattr(catalog_service, "get_model", _get)

    # The "/" in the canonical id is URL-encoded as %2F by the client.
    resp = client.get("/api/v1/catalog/models/anthropic%2Fclaude-3-5-sonnet")
    assert resp.status_code == 200
    assert resp.json()["id"] == "anthropic/claude-3-5-sonnet"
    # The router decoded it back to the canonical "<provider>/<model>" key.
    assert seen["id"] == "anthropic/claude-3-5-sonnet"


def test_get_model_not_found_is_404(client, monkeypatch):
    async def _none(model_id, **kwargs):
        return None

    monkeypatch.setattr(catalog_service, "get_model", _none)
    resp = client.get("/api/v1/catalog/models/nope%2Fnone")
    assert resp.status_code == 404
