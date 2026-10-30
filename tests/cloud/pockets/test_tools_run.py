# tests/cloud/pockets/test_tools_run.py — #1206 invoke_tool route coverage.
# Created: 2026-05-24 — Integration coverage for the tool-run route:
#
#   POST /pockets/{id}/tools/run — invoke a named server-side tool with the
#                                  resolved args from the invoke_tool Ripple
#                                  action verb.
#
# Updated: 2026-06-15 (feat/invoke-tool-v1) — the route now reads the
# per-pocket allowlist off the credential row via
# `get_pocket_backend_for_executor` (the `get_pocket_allowed_tools` stub was
# RETIRED), so the tests that used to monkeypatch that stub now stub the
# backend reader instead. The allowlist lives at the 9th tuple element. The
# executor's own dispatch/gating is covered in test_tool_executor.py; these
# tests pin the ROUTE wiring (body parsing, status codes, response shape,
# allowlist read-and-flatten, auth + tenancy gates).
#
# The pocket service + tool executor are monkeypatched, so the tests pin the
# route wiring without a Mongo connection or real outbound HTTP. Auth +
# license guards are overridden — same pattern as test_pocket_backend_routes.py.

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets import tool_executor
from pocketpaw_ee.cloud.pockets.router import router
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_pocket_action_run,
    require_pocket_edit,
    require_pocket_owner,
)

FAKE_USER = "user-alice"
FAKE_WORKSPACE = "ws-alpha"


def _creds_with_tools(*tool_names: str) -> tuple:
    """Build the 9-tuple `get_pocket_backend_for_executor` returns, with the
    given tool names as the `allowed_tools` (9th) element. Mirrors a connector
    backend with no token; only the trailing allowlist matters to the route."""
    return (
        "",  # base_url
        "none",  # auth_type
        None,  # auth_header
        "",  # token
        [],  # allowed_writes
        None,  # approval_route
        "connector",  # backend_type
        "github",  # connector_name
        [{"tool": name} for name in tool_names],  # allowed_tools (9th)
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler

    a = FastAPI()
    add_error_handler(a)
    a.include_router(router)

    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[require_pocket_edit] = lambda: None
    a.dependency_overrides[require_pocket_owner] = lambda: None
    a.dependency_overrides[require_pocket_action_run] = lambda: None
    a.dependency_overrides[current_user_id] = lambda: FAKE_USER
    a.dependency_overrides[current_workspace_id] = lambda: FAKE_WORKSPACE
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Empty-allowlist default — every tool returns `code:"not_allowed"`
# ---------------------------------------------------------------------------


def test_run_tool_no_backend_returns_not_allowed(monkeypatch, client):
    """A pocket with no backend configured reads as an EMPTY allowlist, so
    every POST returns ok:false with code:not_allowed — fail-closed, the wire
    stays locked until an owner sets a tool policy."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    async def _no_creds(workspace_id, pocket_id):
        return None  # no backend configured

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _no_creds)

    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "WebFetch", "args": {"url": "https://api.example.com/x"}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["tool"] == "WebFetch"
    assert body["code"] == "not_allowed"
    # The wire shape mirrors RunActionResponse so the home-grid reconcile
    # handlers don't need a separate branch — on_success/on_error are
    # always present (empty lists by default).
    assert body["on_success"] == []
    assert body["on_error"] == []


def test_run_tool_empty_grant_list_returns_not_allowed(monkeypatch, client):
    """A pocket WITH a backend but an empty tool policy also fails closed —
    an empty `allowed_tools` revokes every tool."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    async def _empty_grants(workspace_id, pocket_id):
        return _creds_with_tools()  # backend present, zero grants

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _empty_grants)

    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "connector:github:list_issues", "args": {}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["code"] == "not_allowed"


def test_run_tool_reads_allowlist_off_row_and_threads_identity(monkeypatch, client):
    """The route reads `allowed_tools` off the credential row (9th tuple
    element), FLATTENS the wire grants to bare names, and forwards tenancy +
    caller identity into the executor — so the executor stays Beanie-free and
    a future audit log / rate limit can plumb in without touching the route."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    captured: dict[str, object] = {}

    async def _creds(workspace_id, pocket_id):
        captured.setdefault("creds_args", []).append((workspace_id, pocket_id))
        return _creds_with_tools("connector:gmail:gmail_search", "web_fetch")

    async def _run_tool(**kwargs):
        captured.update(kwargs)
        return {"ok": False, "tool": kwargs["tool"], "code": "not_allowed"}

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _creds)
    monkeypatch.setattr(tool_executor, "run_tool", _run_tool)

    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "connector:gmail:gmail_search", "args": {"label": "INBOX"}},
    )
    assert res.status_code == 200, res.text
    assert captured["workspace_id"] == FAKE_WORKSPACE
    assert captured["pocket_id"] == "pocket-1"
    assert captured["user_id"] == FAKE_USER
    assert captured["tool"] == "connector:gmail:gmail_search"
    assert captured["args"] == {"label": "INBOX"}
    # Flattened to bare names from the `{tool}` wire grants — the executor
    # never sees the dict form.
    assert captured["allowed_tools"] == ["connector:gmail:gmail_search", "web_fetch"]
    # The backend read is workspace + pocket scoped.
    assert captured["creds_args"] == [(FAKE_WORKSPACE, "pocket-1")]


def test_run_tool_returns_unknown_tool_when_allowlisted_but_unregistered(monkeypatch, client):
    """A non-connector grant on the allowlist but with no registry
    implementation yet returns `code:unknown_tool` — the wire surface is
    stable so the built-in (WebFetch / Composio) follow-up replaces this
    branch without changing the response shape. Exercises the REAL executor
    (no executor monkeypatch)."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    async def _creds(workspace_id, pocket_id):
        return _creds_with_tools("web_fetch")

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _creds)

    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "web_fetch", "args": {}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["code"] == "unknown_tool"


# ---------------------------------------------------------------------------
# Auth / tenancy gates
# ---------------------------------------------------------------------------


def test_run_tool_forbidden_for_non_invited(monkeypatch):
    """The tool-run gate is owner OR explicit ``shared_with`` ONLY — a
    workspace-visible pocket does not grant access. The guard denies."""
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud._core.http import add_error_handler

    a = FastAPI()
    add_error_handler(a)
    a.include_router(router)
    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[require_pocket_edit] = lambda: None
    a.dependency_overrides[require_pocket_owner] = lambda: None
    a.dependency_overrides[current_user_id] = lambda: FAKE_USER
    a.dependency_overrides[current_workspace_id] = lambda: FAKE_WORKSPACE

    def _deny():
        raise Forbidden("pocket.access_denied", "tool-run access required")

    a.dependency_overrides[require_pocket_action_run] = _deny

    res = TestClient(a).post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "WebFetch", "args": {}},
    )
    assert res.status_code == 403


def test_run_tool_404_when_pocket_missing(monkeypatch, client):
    """The pre-flight ``pockets_service.get`` raises NotFound when the
    pocket isn't in the caller's scope — so the tool wire isn't a
    tenant-existence oracle either."""
    from pocketpaw_ee.cloud._core.errors import NotFound

    async def _missing(pocket_id, user_id):
        raise NotFound("pocket.not_found", "no such pocket")

    monkeypatch.setattr(pockets_service, "get", _missing)

    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "WebFetch", "args": {}},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Request validation — pin the body schema
# ---------------------------------------------------------------------------


def test_run_tool_rejects_empty_tool_name(client):
    """Pydantic ``min_length=1`` on the request body — an empty tool
    name is a 422 before the executor sees anything."""
    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "", "args": {}},
    )
    assert res.status_code == 422


def test_run_tool_args_default_to_empty_dict(monkeypatch, client):
    """``args`` is optional — an omitted args field is the empty dict,
    not a 422. The executor sees a stable shape regardless."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    async def _creds(workspace_id, pocket_id):
        return _creds_with_tools("WebFetch")

    captured: dict[str, object] = {}

    async def _run_tool(**kwargs):
        captured.update(kwargs)
        return {"ok": False, "tool": kwargs["tool"], "code": "not_allowed"}

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _creds)
    monkeypatch.setattr(tool_executor, "run_tool", _run_tool)

    res = client.post("/pockets/pocket-1/tools/run", json={"tool": "WebFetch"})
    assert res.status_code == 200, res.text
    assert captured["args"] == {}
