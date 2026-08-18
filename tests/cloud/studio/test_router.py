# tests/cloud/studio/test_router.py — /studio HTTP layer.
#
# A FastAPI app mounts the studio router with the license + workspace deps
# waived; the SERVICE is the seam (the router is a thin adapter). Asserts:
#   * GET /studio/models         → StudioModelsResponse envelope.
#   * GET /studio/styles         → StudioStylesResponse envelope.
#   * GET /studio/generations    → GenerationsResponse, scoped to the workspace.
#   * POST /studio/generate      → a Generation; bad input → 400; video → 501;
#                                  proxy failure → 502.
#   * GET /studio/generations/{id} → one generation, 404 on a miss.
#   * POST /studio/edit          → a Generation; unknown op → 501; bad input →
#                                 400; fal upstream failure → 502.
#   * POST /studio/suggest-prompt → PromptSuggestion from the {sentence} body.
#   * license gate               → no override, require_license denies.
#
# Created 2026-08-17 (studio-real-backend): new router tests.

from __future__ import annotations

import pocketpaw_ee.cloud.studio.service as studio_service
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pocketpaw_ee.catalog.litellm_client import CatalogUpstreamError
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.studio import schemas
from pocketpaw_ee.cloud.studio.router import router as studio_router


def _generation(**overrides) -> schemas.Generation:
    return schemas.Generation(
        id="gen_abc",
        prompt="a red bicycle",
        status="succeeded",
        kind="image",
        model="fal_ai/fal-ai/flux/schnell",
        params=schemas.GenerationParams(
            kind="image",
            model="fal_ai/fal-ai/flux/schnell",
            aspectRatio="1:1",
            count=1,
            styleId="cinematic",
        ),
        assets=[schemas.GeneratedAsset(id="asset_1", url="/api/v1/media/x.png", mime="image/png")],
        createdAt=1700000000000,
        **overrides,
    )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(studio_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"
    return TestClient(app, raise_server_exceptions=False)


# ── catalog reads ────────────────────────────────────────────────────────────


def test_list_models_returns_envelope(client, monkeypatch):
    async def _list():
        return [
            schemas.StudioModel(
                id="fal_ai/fal-ai/flux/schnell",
                label="Flux Schnell",
                kind="image",
                provider="fal_ai",
                aspectRatios=["1:1", "16:9"],
                maxCount=1,
                supportsNegativePrompt=False,
                default=True,
            )
        ]

    monkeypatch.setattr(studio_service, "list_models", _list)
    resp = client.get("/api/v1/studio/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"][0]["id"] == "fal_ai/fal-ai/flux/schnell"
    assert body["models"][0]["default"] is True


def test_list_models_upstream_failure_is_502(client, monkeypatch):
    async def _boom():
        raise CatalogUpstreamError("proxy down")

    monkeypatch.setattr(studio_service, "list_models", _boom)
    resp = client.get("/api/v1/studio/models")
    assert resp.status_code == 502


def test_list_styles_returns_envelope(client):
    resp = client.get("/api/v1/studio/styles")
    assert resp.status_code == 200
    body = resp.json()
    ids = [s["id"] for s in body["styles"]]
    assert ids[0] == "none"
    assert "cinematic" in ids


# ── generations ──────────────────────────────────────────────────────────────


def test_list_generations_is_workspace_scoped(client, monkeypatch):
    seen: dict = {}

    def _list(workspace_id):
        seen["workspace"] = workspace_id
        return [_generation()]

    monkeypatch.setattr(studio_service, "list_generations", _list)
    resp = client.get("/api/v1/studio/generations")
    assert resp.status_code == 200
    assert resp.json()["generations"][0]["id"] == "gen_abc"
    # The service got the caller's active workspace, not a global list.
    assert seen["workspace"] == "ws-1"


def test_post_generate_returns_generation(client, monkeypatch):
    async def _gen(req, *, workspace_id):
        assert req.prompt == "a red bicycle"
        assert workspace_id == "ws-1"
        return _generation()

    monkeypatch.setattr(studio_service, "generate", _gen)
    resp = client.post(
        "/api/v1/studio/generate",
        json={
            "prompt": "a red bicycle",
            "kind": "image",
            "model": "fal_ai/fal-ai/flux/schnell",
            "aspectRatio": "1:1",
            "count": 1,
            "styleId": "cinematic",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "gen_abc"
    assert body["status"] == "succeeded"


def test_post_generate_bad_input_is_400(client, monkeypatch):
    async def _gen(req, *, workspace_id):
        raise ValueError("prompt is required")

    monkeypatch.setattr(studio_service, "generate", _gen)
    resp = client.post(
        "/api/v1/studio/generate",
        json={"prompt": "", "kind": "image", "model": "m", "aspectRatio": "1:1"},
    )
    assert resp.status_code == 400
    assert "prompt is required" in resp.json()["detail"]


def test_post_generate_video_is_501(client, monkeypatch):
    async def _gen(req, *, workspace_id):
        raise studio_service.StudioNotSupported("video not configured")

    monkeypatch.setattr(studio_service, "generate", _gen)
    resp = client.post(
        "/api/v1/studio/generate",
        json={"prompt": "a clip", "kind": "video", "model": "m", "aspectRatio": "16:9"},
    )
    assert resp.status_code == 501


def test_post_generate_proxy_failure_is_502(client, monkeypatch):
    async def _gen(req, *, workspace_id):
        raise studio_service.StudioUpstreamError("no quota")

    monkeypatch.setattr(studio_service, "generate", _gen)
    resp = client.post(
        "/api/v1/studio/generate",
        json={"prompt": "x", "kind": "image", "model": "m", "aspectRatio": "1:1"},
    )
    assert resp.status_code == 502


def test_get_generation_found(client, monkeypatch):
    def _get(gen_id, workspace_id):
        assert gen_id == "gen_abc"
        assert workspace_id == "ws-1"
        return _generation()

    monkeypatch.setattr(studio_service, "get_generation", _get)
    resp = client.get("/api/v1/studio/generations/gen_abc")
    assert resp.status_code == 200
    assert resp.json()["id"] == "gen_abc"


def test_get_generation_not_found_is_404(client, monkeypatch):
    monkeypatch.setattr(studio_service, "get_generation", lambda gen_id, workspace_id: None)
    resp = client.get("/api/v1/studio/generations/nope")
    assert resp.status_code == 404


# ── edit + suggest ───────────────────────────────────────────────────────────


def test_post_edit_returns_generation(client, monkeypatch):
    async def _edit(req, *, workspace_id):
        assert req.op == "upscale"
        assert req.sourceUrl == "/api/v1/media/x.png"
        assert workspace_id == "ws-1"
        return _generation()

    monkeypatch.setattr(studio_service, "edit", _edit)
    resp = client.post(
        "/api/v1/studio/edit",
        json={"op": "upscale", "sourceUrl": "/api/v1/media/x.png", "factor": 4},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "gen_abc"
    assert resp.json()["status"] == "succeeded"


def test_post_edit_unknown_op_is_501(client, monkeypatch):
    async def _edit(req, *, workspace_id):
        raise studio_service.StudioNotSupported("Edit op 'warp' is not supported")

    monkeypatch.setattr(studio_service, "edit", _edit)
    resp = client.post(
        "/api/v1/studio/edit",
        json={"op": "warp", "sourceUrl": "/api/v1/media/x.png"},
    )
    assert resp.status_code == 501


def test_post_edit_bad_input_is_400(client, monkeypatch):
    async def _edit(req, *, workspace_id):
        raise ValueError("sourceUrl is required for an edit")

    monkeypatch.setattr(studio_service, "edit", _edit)
    resp = client.post(
        "/api/v1/studio/edit",
        json={"op": "upscale", "sourceUrl": ""},
    )
    assert resp.status_code == 400
    assert "sourceUrl is required" in resp.json()["detail"]


def test_post_edit_upstream_failure_is_502(client, monkeypatch):
    async def _edit(req, *, workspace_id):
        raise studio_service.StudioUpstreamError("fal edit failed: timeout")

    monkeypatch.setattr(studio_service, "edit", _edit)
    resp = client.post(
        "/api/v1/studio/edit",
        json={"op": "remove-bg", "sourceUrl": "/api/v1/media/x.png"},
    )
    assert resp.status_code == 502
    assert "Image edit failed" in resp.json()["detail"]


def test_post_suggest_prompt(client):
    resp = client.post("/api/v1/studio/suggest-prompt", json={"sentence": "a lighthouse"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "image"
    assert "a lighthouse" in body["prompt"]


def test_license_gate_403_when_denied(monkeypatch):
    """The router-level license dep is real: when require_license denies, every
    /studio call answers 403 before the handler runs."""

    async def _deny():
        raise HTTPException(status_code=403, detail="Enterprise license is missing or invalid")

    app = FastAPI()
    app.include_router(studio_router, prefix="/api/v1")
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"
    app.dependency_overrides[require_license] = _deny
    resp = TestClient(app, raise_server_exceptions=False).get("/api/v1/studio/models")
    assert resp.status_code == 403


# ── Flow projects ────────────────────────────────────────────────────────────


@pytest.fixture
def flow_env(tmp_path, monkeypatch):
    """Point the service's flow-project JSONL at a tmp dir so the CRUD round-trip
    (real service, real file) never touches the developer's ~/.pocketpaw."""
    projects = tmp_path / "studio" / "flow-projects.jsonl"
    projects.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(studio_service, "_projects_path", lambda: projects)
    return projects


def _flow_node(node_id="text_1"):
    return {
        "id": node_id,
        "type": "text",
        "position": {"x": 40, "y": 120},
        "data": {"status": "idle", "text": "hello"},
    }


def test_flow_projects_crud_roundtrip(client, flow_env):
    """GET list → PUT (upsert create) → GET shows it → PUT (update) → DELETE.
    Runs the REAL service + JSONL persistence through the router seam."""
    # Fresh workspace: empty list.
    resp = client.get("/api/v1/studio/flow-projects")
    assert resp.status_code == 200
    assert resp.json() == {"projects": []}

    # PUT creates (upsert) a project.
    resp = client.put(
        "/api/v1/studio/flow-projects/proj_1",
        json={"name": "Posters", "nodes": [_flow_node()], "edges": []},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["id"] == "proj_1"
    assert created["name"] == "Posters"
    assert created["nodes"][0]["data"]["text"] == "hello"

    # GET lists it (wire keys camelCase, opaque node payload preserved).
    resp = client.get("/api/v1/studio/flow-projects")
    assert resp.status_code == 200
    listed = resp.json()["projects"]
    assert [p["id"] for p in listed] == ["proj_1"]

    # PUT updates in place; name preserved when omitted.
    resp = client.put(
        "/api/v1/studio/flow-projects/proj_1",
        json={
            "name": None,
            "nodes": [_flow_node("text_2")],
            "edges": [
                {
                    "id": "e-1",
                    "source": "text_2",
                    "target": "img_1",
                    "sourceHandle": None,
                    "targetHandle": None,
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Posters"
    assert resp.json()["nodes"][0]["id"] == "text_2"
    assert resp.json()["edges"][0]["source"] == "text_2"

    # DELETE removes it; a second delete is 404.
    assert client.delete("/api/v1/studio/flow-projects/proj_1").status_code == 204
    assert client.get("/api/v1/studio/flow-projects").json()["projects"] == []
    assert client.delete("/api/v1/studio/flow-projects/proj_1").status_code == 404
