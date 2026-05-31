# tests/cloud/pockets/test_tools_run.py — #1206 part a (invoke_tool wire).
# Created: 2026-05-24 — Integration coverage for the new tool-run route:
#
#   POST /pockets/{id}/tools/run — invoke a named server-side tool with the
#                                  resolved args from the new invoke_tool
#                                  Ripple action verb.
#
# Part (a) intentionally ships an empty allowlist so every tool call is
# rejected with `code:"not_allowed"`. These tests pin the wire-level
# contract (auth gate, tenancy scoping, structured error shape) so part
# (b) — the home-grid `onEvent` plumbing — can rely on a stable surface.
#
# The pocket service + tool executor are monkeypatched, so the tests pin
# the route wiring (request body parsing, status codes, response shape)
# without a Mongo connection or real outbound HTTP. Auth + license guards
# are overridden — same pattern as test_pocket_backend_routes.py.

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


def test_run_tool_empty_allowlist_returns_not_allowed(monkeypatch, client):
    """Part (a) ships with no tools enabled. Every POST returns ok:false
    with code:not_allowed so the wire is locked down until the captain
    explicitly enables a tool per pocket."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    monkeypatch.setattr(pockets_service, "get", _get_pocket)

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


def test_run_tool_threads_workspace_user_pocket_to_executor(monkeypatch, client):
    """The route forwards tenancy + caller identity into the executor so
    a future audit log + per-(pocket, user) rate limit can plumb in
    without touching the route signature."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    monkeypatch.setattr(pockets_service, "get", _get_pocket)

    captured: dict[str, object] = {}

    async def _run_tool(**kwargs):
        captured.update(kwargs)
        return {"ok": False, "tool": kwargs["tool"], "code": "not_allowed"}

    async def _allowlist(workspace_id, pocket_id):
        captured.setdefault("allowlist_args", []).append((workspace_id, pocket_id))
        return []

    monkeypatch.setattr(tool_executor, "run_tool", _run_tool)
    monkeypatch.setattr(tool_executor, "get_pocket_allowed_tools", _allowlist)

    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "GMAIL_FETCH_EMAILS", "args": {"label": "INBOX"}},
    )
    assert res.status_code == 200, res.text
    assert captured["workspace_id"] == FAKE_WORKSPACE
    assert captured["pocket_id"] == "pocket-1"
    assert captured["user_id"] == FAKE_USER
    assert captured["tool"] == "GMAIL_FETCH_EMAILS"
    assert captured["args"] == {"label": "INBOX"}
    assert captured["allowed_tools"] == []
    # The allowlist lookup is workspace-scoped.
    assert captured["allowlist_args"] == [(FAKE_WORKSPACE, "pocket-1")]


def test_run_tool_returns_unknown_tool_when_allowlisted_but_unregistered(monkeypatch, client):
    """An allowlist with the tool name but no registry implementation
    yet returns `code:unknown_tool` — the wire surface is in place so
    the follow-up that adds Composio / WebFetch routing replaces this
    branch without changing the response shape."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    async def _allowlist(workspace_id, pocket_id):
        return ["GMAIL_FETCH_EMAILS"]

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(tool_executor, "get_pocket_allowed_tools", _allowlist)

    res = client.post(
        "/pockets/pocket-1/tools/run",
        json={"tool": "GMAIL_FETCH_EMAILS", "args": {}},
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

    captured: dict[str, object] = {}

    async def _run_tool(**kwargs):
        captured.update(kwargs)
        return {"ok": False, "tool": kwargs["tool"], "code": "not_allowed"}

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(tool_executor, "run_tool", _run_tool)

    res = client.post("/pockets/pocket-1/tools/run", json={"tool": "WebFetch"})
    assert res.status_code == 200, res.text
    assert captured["args"] == {}
