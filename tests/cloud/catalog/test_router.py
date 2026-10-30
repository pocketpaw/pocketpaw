# tests/cloud/catalog/test_router.py — Model Catalog (MCG-1) HTTP-layer tests.
# A FastAPI app mounts the catalog router with the license dep waived; the catalog
# SERVICE is patched (the router is a thin adapter, so the service is the seam).
# Asserts:
#   * GET /catalog/models returns {models, total} and forwards filter params.
#   * an unknown modality value is a 400 (clear error, not a silent empty list).
#   * GET /catalog/models/{id} resolves the URL-ENCODED "<provider>/<model>" id
#     (the %2F-encoded slash) to one entry; a miss is 404.
#   * a LiteLLM upstream failure (CatalogUpstreamError) surfaces as 502.
#   * the list carries each model's availability for the caller's workspace
#     (out of credit vs ok), and leaves it null when there is no session.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.catalog import availability
from pocketpaw_ee.catalog import service as catalog_service
from pocketpaw_ee.catalog.litellm_client import CatalogUpstreamError
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry, Pricing
from pocketpaw_ee.catalog.router import caller_workspace_id
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
    app.dependency_overrides[caller_workspace_id] = lambda: None
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


_FLASH = ModelCatalogEntry(
    id="deepseek/deepseek-v4-flash",
    display_name="deepseek-v4-flash",
    provider="deepseek",
    modality=Modality.CHAT,
)


def test_list_models_carries_the_workspace_availability(client, monkeypatch):
    async def _list(**kwargs):
        return [_FLASH, _SONNET]

    seen: dict = {}

    async def _marks(workspace_id):
        seen["ws"] = workspace_id
        since = datetime(2026, 10, 23, 10, tzinfo=UTC).timestamp()
        return {"provider:deepseek": {"since_ts": since, "until": 9e18}}

    monkeypatch.setattr(catalog_service, "list_models", _list)
    monkeypatch.setattr(availability, "read_marks", _marks)
    client.app.dependency_overrides[caller_workspace_id] = lambda: "w1"

    resp = client.get("/api/v1/catalog/models", params={"modality": "chat"})

    assert resp.status_code == 200
    rows = {m["id"]: m["availability"] for m in resp.json()["models"]}
    assert seen["ws"] == "w1"
    assert rows["deepseek/deepseek-v4-flash"]["status"] == "out_of_credit"
    assert rows["deepseek/deepseek-v4-flash"]["since"].startswith("2026-10-23T10:00:00")
    assert "credit" in rows["deepseek/deepseek-v4-flash"]["message"]
    assert rows["anthropic/claude-3-5-sonnet"] == {"status": "ok", "since": None, "message": None}
    # The cached catalog entries are never written to.
    assert _FLASH.availability is None


def test_list_models_without_a_session_leaves_availability_null(client, monkeypatch):
    async def _list(**kwargs):
        return [_FLASH]

    async def _marks(workspace_id):  # pragma: no cover - must not be called
        raise AssertionError("no workspace, no lookup")

    monkeypatch.setattr(catalog_service, "list_models", _list)
    monkeypatch.setattr(availability, "read_marks", _marks)
    client.app.dependency_overrides[caller_workspace_id] = lambda: None

    resp = client.get("/api/v1/catalog/models")

    assert resp.status_code == 200
    assert resp.json()["models"][0]["availability"] is None
