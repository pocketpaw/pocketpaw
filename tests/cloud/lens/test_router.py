# tests/cloud/lens/test_router.py — the /api/v1/lens/* paw-lens proxy.
#
# A FastAPI app mounts the lens router with the CloudError handler, the license
# dep waived and the workspace pinned to "ws_test". The service's LensClient is
# swapped for one on an httpx.MockTransport that records every upstream request,
# so these run the real router -> service -> client path with no network.
# Covers: disabled (no URL, no request), pass-through, workspace_id injection
# (client-sent one ignored), token header, mute body, timeout -> 503
# lens.unavailable, 404 -> 404, 401 -> 503 lens.misconfigured, bad path -> 422.

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.lens import service as lens_service
from pocketpaw_ee.cloud.lens.client import LensClient
from pocketpaw_ee.cloud.lens.router import router as lens_router
from pocketpaw_ee.cloud.license import require_license

TOKEN = "lens-secret-xyz"


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


@pytest.fixture
def upstream(monkeypatch, seen):
    """Install a MockTransport-backed client; returns a setter for the handler."""
    state = {"handler": lambda req: httpx.Response(200, json={"ok": True})}

    def _transport_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state["handler"](request)

    monkeypatch.setattr(
        lens_service, "_client", LensClient(_transport=httpx.MockTransport(_transport_handler))
    )

    def _set(handler) -> None:
        state["handler"] = handler

    return _set


def _settings(monkeypatch, url: str) -> None:
    monkeypatch.setattr(
        lens_service,
        "get_settings",
        lambda: SimpleNamespace(lens_api_url=url, lens_api_token=TOKEN),
    )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(lens_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: "ws_test"
    return TestClient(app, raise_server_exceptions=False)


def test_disabled_returns_enabled_false_without_network(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "")
    for path in ("/overview", "/issues", "/agents", "/monitors", "/runs/t1"):
        resp = client.get(f"/api/v1/lens{path}")
        assert resp.status_code == 200, path
        assert resp.json() == {"enabled": False}
    resp = client.post("/api/v1/lens/issues/fp1/resolve")
    assert resp.json() == {"enabled": False}
    assert seen == []


def test_enabled_passes_upstream_json_through(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790/")
    body = {"runs": 12, "failing_rate": 0.1, "series": {"runs": [1, 2]}}
    upstream(lambda req: httpx.Response(200, json=body))
    resp = client.get("/api/v1/lens/overview", params={"since": "7d"})
    assert resp.status_code == 200
    assert resp.json() == body
    req = seen[0]
    assert req.url.path == "/v1/overview"
    assert req.url.params["since"] == "7d"


def test_list_response_passes_through(client, monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json=[{"fingerprint": "a"}]))
    resp = client.get("/api/v1/lens/issues", params={"status": "open"})
    assert resp.status_code == 200
    assert resp.json() == [{"fingerprint": "a"}]


def test_workspace_injected_and_client_value_ignored(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    resp = client.get("/api/v1/lens/issues", params={"workspace_id": "ws_evil", "status": "muted"})
    assert resp.status_code == 200
    params = seen[0].url.params
    assert params.get_list("workspace_id") == ["ws_test"]
    assert params["status"] == "muted"


def test_token_header_sent(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    client.get("/api/v1/lens/agents")
    assert seen[0].headers["X-Lens-Token"] == TOKEN


def test_mute_body_forwarded(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json={"fingerprint": "fp1", "status": "muted"}))
    resp = client.post("/api/v1/lens/issues/fp1/mute", json={"minutes": 1440})
    assert resp.status_code == 200
    assert resp.json()["status"] == "muted"
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/issues/fp1/mute"
    assert req.url.params["workspace_id"] == "ws_test"
    assert httpx.Response(200, content=req.content).json() == {"minutes": 1440}


def test_mute_rejects_bad_minutes(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    assert client.post("/api/v1/lens/issues/fp1/mute", json={"minutes": 0}).status_code == 422
    assert seen == []


def test_timeout_is_503_unavailable(client, monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")

    def _boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    upstream(_boom)
    resp = client.get("/api/v1/lens/overview")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "lens.unavailable"


def test_connect_error_and_5xx_are_503(client, monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")

    def _refused(req):
        raise httpx.ConnectError("refused", request=req)

    upstream(_refused)
    assert client.get("/api/v1/lens/agents").status_code == 503
    upstream(lambda req: httpx.Response(500, text="boom"))
    resp = client.get("/api/v1/lens/agents")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "lens.unavailable"


def test_upstream_404_is_404(client, monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(404, json={"error": "nope"}))
    resp = client.get("/api/v1/lens/runs/abc123")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "lens.not_found"


def test_upstream_401_is_503_misconfigured(client, monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(401, json={"error": "bad token"}))
    resp = client.get("/api/v1/lens/overview")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "lens.misconfigured"
    assert TOKEN not in resp.text


@pytest.mark.parametrize(
    ("path", "status"),
    [
        # Dot segments and encoded slashes never reach the route (404).
        ("/api/v1/lens/issues/..", 404),
        ("/api/v1/lens/issues/a%2Fb", 404),
        # Everything that does reach it is checked against _SAFE_ID (422).
        ("/api/v1/lens/runs/a%3Fx%3D1", 422),
        ("/api/v1/lens/monitors/" + "a" * 200, 422),
        ("/api/v1/lens/issues/-x", 422),
        ("/api/v1/lens/issues/.hidden", 422),
    ],
)
def test_bad_path_param_rejected(client, monkeypatch, upstream, seen, path, status):
    _settings(monkeypatch, "http://lens:8790")
    assert client.get(path).status_code == status
    assert seen == []


def test_bad_status_filter_rejected(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    assert client.get("/api/v1/lens/issues", params={"status": "all"}).status_code == 422
    assert seen == []
