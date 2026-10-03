# tests/cloud/lens/test_router.py — the /api/v1/lens/* paw-lens proxy.
#
# A FastAPI app mounts the lens router with the CloudError handler, the license
# dep waived and the workspace pinned to "ws_test". The service's LensClient is
# swapped for one on an httpx.MockTransport that records every upstream request,
# so these run the real router -> service -> client path with no network.
# Covers: disabled (no URL, no request), pass-through, workspace_id injection
# (client-sent one ignored), token header, mute body, timeout -> 503
# lens.unavailable, 404 -> 404, 401 -> 503 lens.misconfigured, bad path -> 422,
# the runs list and span detail routes, the agent_id / automation / status /
# limit filters (forwarded when valid, 422 with no upstream call when not), the
# real lens.manage RBAC guard on mute/resolve (member 403, admin passes), and
# Privacy A: a member gets run/span/issue content stripped plus
# ``content_hidden``, an admin gets the upstream body untouched.

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
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


def _app(role: str) -> TestClient:
    """Lens router with the real RBAC guard; the user holds ``role`` in ws_test."""
    app = FastAPI()
    add_error_handler(app)
    app.include_router(lens_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: "ws_test"
    user = SimpleNamespace(
        id="u1",
        is_active=True,
        active_workspace="ws_test",
        workspaces=[SimpleNamespace(workspace="ws_test", role=role)],
    )

    async def _user():
        return user

    app.dependency_overrides[current_active_user] = _user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client() -> TestClient:
    return _app("admin")


def test_disabled_returns_enabled_false_without_network(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "")
    for path in ("/overview", "/issues", "/agents", "/monitors", "/runs", "/runs/t1/spans/s1"):
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


@pytest.mark.parametrize("action", ["mute", "resolve"])
def test_member_cannot_mute_or_resolve(monkeypatch, upstream, seen, action):
    _settings(monkeypatch, "http://lens:8790")
    resp = _app("member").post(f"/api/v1/lens/issues/fp1/{action}", json={"minutes": 60})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "workspace.insufficient_role"
    assert seen == []


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admin_and_owner_can_resolve(monkeypatch, upstream, seen, role):
    _settings(monkeypatch, "http://lens:8790")
    resp = _app(role).post("/api/v1/lens/issues/fp1/resolve")
    assert resp.status_code == 200
    assert seen[0].url.path == "/v1/issues/fp1/resolve"


def test_member_can_read(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    assert _app("member").get("/api/v1/lens/overview").status_code == 200


def test_runs_list_forwards_filters(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json=[{"trace_id": "a" * 32}]))
    resp = client.get(
        "/api/v1/lens/runs",
        params={
            "agent_id": "69e51d5c57ff64b3903868fe",
            "automation": "reminder:r1",
            "status": "error",
            "since": "24h",
            "limit": 200,
            "workspace_id": "ws_evil",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == [{"trace_id": "a" * 32}]
    req = seen[0]
    assert req.url.path == "/v1/runs"
    assert dict(req.url.params) == {
        "agent_id": "69e51d5c57ff64b3903868fe",
        "automation": "reminder:r1",
        "status": "error",
        "since": "24h",
        "limit": "200",
        "workspace_id": "ws_test",
    }


def test_runs_list_sends_only_set_filters(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    assert client.get("/api/v1/lens/runs").status_code == 200
    assert dict(seen[0].url.params) == {"workspace_id": "ws_test"}


def test_span_detail_proxied(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json={"span_id": "b" * 16}))
    resp = client.get(f"/api/v1/lens/runs/{'a' * 32}/spans/{'b' * 16}")
    assert resp.status_code == 200
    assert resp.json() == {"span_id": "b" * 16}
    assert seen[0].url.path == f"/v1/runs/{'a' * 32}/spans/{'b' * 16}"
    assert seen[0].url.params["workspace_id"] == "ws_test"


@pytest.mark.parametrize("path", ["/overview", "/issues", "/agents", "/runs"])
def test_agent_id_forwarded(client, monkeypatch, upstream, seen, path):
    _settings(monkeypatch, "http://lens:8790")
    assert client.get(f"/api/v1/lens{path}", params={"agent_id": "ag_1-x"}).status_code == 200
    assert seen[0].url.params["agent_id"] == "ag_1-x"


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/overview", {"agent_id": "a.b"}),
        ("/issues", {"agent_id": "a" * 65}),
        ("/agents", {"agent_id": ""}),
        ("/runs", {"agent_id": "a/b"}),
        ("/runs", {"automation": "../x"}),
        ("/runs", {"automation": "a" * 200}),
        ("/runs", {"status": "running"}),
        ("/runs", {"limit": 0}),
        ("/runs", {"limit": 201}),
        ("/runs", {"limit": "ten"}),
    ],
)
def test_bad_filters_rejected(client, monkeypatch, upstream, seen, path, params):
    _settings(monkeypatch, "http://lens:8790")
    assert client.get(f"/api/v1/lens{path}", params=params).status_code == 422
    assert seen == []


def test_bad_span_id_rejected(client, monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    assert client.get("/api/v1/lens/runs/t1/spans/-x").status_code == 422
    assert seen == []


# --- Privacy A -------------------------------------------------------------

_RUN = {
    "run": {"trace_id": "a" * 32, "summary": "user asked for payroll", "status": "error"},
    "spans": [{"span_id": "s1", "tool": "search", "args_preview": "q=salary"}],
    "findings": [{"detector": "tool_error", "message": "search failed"}],
}
_SPAN = {
    "span_id": "s1",
    "attributes": {
        "gen_ai.input.messages": "[secret prompt]",
        "gen_ai.output.messages": "[secret answer]",
        "gen_ai.system_instructions": "be nice",
        "gen_ai.tool.call.arguments": '{"q":"salary"}',
        "gen_ai.tool.call.result": "rows",
        "pydantic_ai.all_messages": "[...]",
        "gen_ai.request.model": "claude",
    },
    "events": [{"name": "log", "attributes": {"gen_ai.input.messages": "x", "level": "info"}}],
    "messages": {"input": [{"role": "user"}], "output": [], "system": []},
    "tool": {"name": "search", "call_id": "c1", "arguments": {"q": "salary"}, "result": "rows"},
}


def _content_routes():
    return [
        ("/runs", [{"trace_id": "a" * 32, "summary": "secret"}]),
        (f"/runs/{'a' * 32}", _RUN),
        (f"/runs/{'a' * 32}/spans/s1", _SPAN),
        ("/issues/fp1", {"fingerprint": "fp1", "runs": [{"trace_id": "t", "summary": "secret"}]}),
    ]


@pytest.mark.parametrize(("path", "body"), _content_routes())
def test_admin_gets_full_content(monkeypatch, upstream, path, body):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json=body))
    resp = _app("admin").get(f"/api/v1/lens{path}")
    assert resp.status_code == 200
    assert resp.json() == body


def test_member_run_detail_stripped(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json=_RUN))
    got = _app("member").get(f"/api/v1/lens/runs/{'a' * 32}").json()
    assert got["content_hidden"] is True
    assert got["run"]["summary"] == ""
    assert got["run"]["status"] == "error"
    assert got["spans"][0] == {"span_id": "s1", "tool": "search", "args_preview": ""}
    assert got["findings"] == _RUN["findings"]


def test_member_span_detail_stripped(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json=_SPAN))
    got = _app("member").get(f"/api/v1/lens/runs/{'a' * 32}/spans/s1").json()
    assert got["content_hidden"] is True
    assert got["messages"] is None
    assert got["tool"] == {"name": "search", "call_id": "c1", "arguments": None, "result": None}
    attrs = got["attributes"]
    assert attrs["gen_ai.request.model"] == "claude"
    for key in _SPAN["attributes"]:
        if key != "gen_ai.request.model":
            assert attrs[key] == "[hidden]", key
    assert got["events"][0]["attributes"] == {"gen_ai.input.messages": "[hidden]", "level": "info"}


def test_member_runs_list_and_issue_stripped(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    upstream(lambda req: httpx.Response(200, json=[{"trace_id": "t", "summary": "secret"}]))
    member = _app("member")
    assert member.get("/api/v1/lens/runs").json() == [{"trace_id": "t", "summary": ""}]
    upstream(lambda req: httpx.Response(200, json={"runs": [{"summary": "secret"}]}))
    got = member.get("/api/v1/lens/issues/fp1").json()
    assert got == {"runs": [{"summary": ""}], "content_hidden": True}


def test_member_disabled_body_untouched(monkeypatch, upstream):
    _settings(monkeypatch, "")
    assert _app("member").get(f"/api/v1/lens/runs/{'a' * 32}").json() == {"enabled": False}
