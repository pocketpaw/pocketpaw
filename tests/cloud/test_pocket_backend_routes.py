# tests/cloud/test_pocket_backend_routes.py — RFC 04 alpha.
# Created: 2026-05-21 — Integration coverage for the pocket-backend routes
# added to the pockets router:
#
#   PUT    /pockets/{id}/backend
#   GET    /pockets/{id}/backend
#   DELETE /pockets/{id}/backend
#   POST   /pockets/{id}/sources/run
#
# The service functions and the source executor are monkeypatched so the
# tests pin the route wiring (request body parsing, status codes, response
# shape) without a Mongo connection or real outbound HTTP. Auth + license
# guards are overridden — same pattern as test_pocket_layout_routes.py.
#
# Updated: 2026-05-21 (PR #1177 security pass) — added coverage for the
# DELETE route, the edit-access guard on GET, and the user_id thread-through
# on the source-run route.
#
# Updated: 2026-05-22 (RFC 05 M2a) — the executor-creds tuple gained a
# trailing `allowed_writes` element and the backend response carries the
# write allowlist; existing assertions updated. Added coverage for the two
# new write-action routes:
#   POST /pockets/{id}/actions/run        — run a declared write action
#   PUT  /pockets/{id}/backend/write-policy — set the write allowlist

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
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
# PUT /pockets/{id}/backend
# ---------------------------------------------------------------------------


def test_put_backend_configures(monkeypatch, client):
    captured = {}

    async def _set(workspace_id, user_id, pocket_id, base_url, auth_type, auth_token, auth_header):
        captured.update(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            base_url=base_url,
            auth_type=auth_type,
            auth_token=auth_token,
        )
        return {"base_url": base_url, "auth_type": auth_type, "configured": True}

    monkeypatch.setattr(pockets_service, "set_pocket_backend", _set)

    res = client.put(
        "/pockets/pocket-1/backend",
        json={
            "base_url": "https://api.example.com",
            "auth_type": "bearer",
            "auth_token": "secret",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {
        "base_url": "https://api.example.com",
        "auth_type": "bearer",
        "configured": True,
        # RFC 05 M2a: the response now carries the write allowlist —
        # empty by default (fail-closed).
        "allowed_writes": [],
    }
    # The route forwarded the right identity + body to the service.
    assert captured["workspace_id"] == FAKE_WORKSPACE
    assert captured["pocket_id"] == "pocket-1"
    assert captured["auth_token"] == "secret"


def test_put_backend_rejects_bad_auth_type(client):
    res = client.put(
        "/pockets/pocket-1/backend",
        json={"base_url": "https://api.example.com", "auth_type": "oauth2"},
    )
    assert res.status_code == 422  # Literal validation


# ---------------------------------------------------------------------------
# GET /pockets/{id}/backend
# ---------------------------------------------------------------------------


def test_get_backend_returns_summary(monkeypatch, client):
    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id, "name": "P"}

    async def _get_backend(workspace_id, pocket_id):
        return {"base_url": "https://api.example.com", "auth_type": "none", "configured": True}

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend", _get_backend)

    res = client.get("/pockets/pocket-1/backend")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["configured"] is True
    assert "token" not in body


def test_get_backend_404_when_unconfigured(monkeypatch, client):
    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id}

    async def _get_backend(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend", _get_backend)

    res = client.get("/pockets/pocket-1/backend")
    assert res.status_code == 404


def test_get_backend_forbidden_for_non_editor(monkeypatch):
    """SHOULD-FIX-1 — a viewer (no edit access) gets 403, not the binding."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud._core.http import add_error_handler

    a = FastAPI()
    add_error_handler(a)
    a.include_router(router)
    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[require_pocket_owner] = lambda: None
    a.dependency_overrides[current_user_id] = lambda: FAKE_USER
    a.dependency_overrides[current_workspace_id] = lambda: FAKE_WORKSPACE

    def _deny_edit():
        raise Forbidden("pocket.forbidden", "edit access required")

    a.dependency_overrides[require_pocket_edit] = _deny_edit

    res = TestClient(a).get("/pockets/pocket-1/backend")
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /pockets/{id}/backend
# ---------------------------------------------------------------------------


def test_delete_backend_revokes(monkeypatch, client):
    captured = {}

    async def _remove(workspace_id, user_id, pocket_id):
        captured.update(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)

    monkeypatch.setattr(pockets_service, "remove_pocket_backend", _remove)

    res = client.delete("/pockets/pocket-1/backend")
    assert res.status_code == 204, res.text
    assert res.content == b""
    assert captured == {
        "workspace_id": FAKE_WORKSPACE,
        "user_id": FAKE_USER,
        "pocket_id": "pocket-1",
    }


def test_delete_backend_idempotent_when_unconfigured(monkeypatch, client):
    """remove_pocket_backend is a no-op on a pocket with no credential —
    the route still returns 204."""

    async def _remove(workspace_id, user_id, pocket_id):
        return None  # service no-ops when there is no row

    monkeypatch.setattr(pockets_service, "remove_pocket_backend", _remove)

    res = client.delete("/pockets/pocket-with-no-backend/backend")
    assert res.status_code == 204


# ---------------------------------------------------------------------------
# POST /pockets/{id}/sources/run
# ---------------------------------------------------------------------------


def test_run_sources_happy_path(monkeypatch, client):
    spec = {"sources": {"prs": {"method": "GET", "path": "/pulls", "bind": "state.prs"}}}

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id, "rippleSpec": spec}

    async def _get_creds(workspace_id, pocket_id):
        # RFC 05 M2a: 5-tuple — the trailing element is the write allowlist.
        return ("https://api.example.com", "bearer", None, "tok", [])

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _get_creds)

    from pocketpaw_ee.cloud.pockets import source_executor

    captured = {}

    async def _run_sources(**kwargs):
        captured.update(kwargs)
        return {"ran": [{"source": "prs", "bind": "prs", "value": [1, 2]}], "errors": []}

    monkeypatch.setattr(source_executor, "run_sources", _run_sources)

    res = client.post("/pockets/pocket-1/sources/run", json={"trigger": "manual"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ran"][0]["bind"] == "prs"
    assert body["errors"] == []
    # The route passed the spec + creds + trigger + identity through.
    assert captured["ripple_spec"] == spec
    assert captured["base_url"] == "https://api.example.com"
    assert captured["token"] == "tok"
    assert captured["trigger"] == "manual"
    assert captured["user_id"] == FAKE_USER


def test_run_sources_400_when_no_backend(monkeypatch, client):
    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id, "rippleSpec": {}}

    async def _no_creds(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _no_creds)

    res = client.post("/pockets/pocket-1/sources/run", json={})
    assert res.status_code == 400, res.text


# ---------------------------------------------------------------------------
# PUT /pockets/{id}/backend/write-policy — RFC 05 M2a
# ---------------------------------------------------------------------------


def test_put_write_policy_sets_allowlist(monkeypatch, client):
    captured = {}

    async def _set_policy(workspace_id, user_id, pocket_id, allowed_writes):
        captured.update(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            allowed_writes=allowed_writes,
        )
        return {
            "base_url": "https://api.example.com",
            "auth_type": "bearer",
            "configured": True,
            "allowed_writes": allowed_writes,
        }

    monkeypatch.setattr(pockets_service, "set_pocket_write_policy", _set_policy)

    res = client.put(
        "/pockets/pocket-1/backend/write-policy",
        json={"allowed_writes": [{"method": "POST", "path_pattern": "/leases/*/renew"}]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["allowed_writes"] == [{"method": "POST", "path_pattern": "/leases/*/renew"}]
    # The route forwarded the right identity + rules to the service.
    assert captured["pocket_id"] == "pocket-1"
    assert captured["allowed_writes"] == [{"method": "POST", "path_pattern": "/leases/*/renew"}]


def test_put_write_policy_rejects_bad_method(client):
    """`method` is a write-verb Literal — GET is rejected at parse time."""
    res = client.put(
        "/pockets/pocket-1/backend/write-policy",
        json={"allowed_writes": [{"method": "GET", "path_pattern": "/x"}]},
    )
    assert res.status_code == 422


def test_put_write_policy_empty_list_is_valid(monkeypatch, client):
    """An empty allowlist revokes every write — a valid request."""

    async def _set_policy(workspace_id, user_id, pocket_id, allowed_writes):
        return {
            "base_url": "https://api.example.com",
            "auth_type": "none",
            "configured": True,
            "allowed_writes": [],
        }

    monkeypatch.setattr(pockets_service, "set_pocket_write_policy", _set_policy)
    res = client.put("/pockets/pocket-1/backend/write-policy", json={"allowed_writes": []})
    assert res.status_code == 200, res.text
    assert res.json()["allowed_writes"] == []


# ---------------------------------------------------------------------------
# POST /pockets/{id}/actions/run — RFC 05 M2a
# ---------------------------------------------------------------------------


def test_run_action_happy_path(monkeypatch, client):
    """The route reads `method` server-side from the persisted action,
    threads the resolved path/params + creds + allowlist to the executor,
    and returns its result."""
    spec = {
        "actions": {
            "mark_renewed": {
                "kind": "write_binding",
                "method": "POST",
                "path": "/leases/{item.id}/renew",
            }
        }
    }

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id, "rippleSpec": spec}

    async def _get_creds(workspace_id, pocket_id):
        return (
            "https://api.example.com",
            "bearer",
            None,
            "tok",
            [{"method": "POST", "path_pattern": "/leases/*/renew"}],
        )

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _get_creds)

    from pocketpaw_ee.cloud.pockets import action_executor

    captured = {}

    async def _run_action(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "action": kwargs["action"],
            "status": 200,
            "response": {"renewed": True},
            "on_success": [],
            "on_error": [],
        }

    monkeypatch.setattr(action_executor, "run_action", _run_action)

    res = client.post(
        "/pockets/pocket-1/actions/run",
        json={"action": "mark_renewed", "path": "/leases/42/renew", "params": {"rent": 2000}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["response"] == {"renewed": True}
    # The executor saw the raw action (the server picks the verb), the
    # resolved path/params, the creds, and the allowlist.
    assert captured["raw_action"]["method"] == "POST"
    assert captured["path"] == "/leases/42/renew"
    assert captured["params"] == {"rent": 2000}
    assert captured["allowed_writes"] == [{"method": "POST", "path_pattern": "/leases/*/renew"}]
    assert captured["user_id"] == FAKE_USER
    # The route threads `workspace_id` so the executor can tenant-tag its
    # audit-log entries.
    assert captured["workspace_id"]


def test_run_action_404_when_action_not_declared(monkeypatch, client):
    """An action name not in the persisted spec returns an `ok:false`
    body with code `action_not_found` — no executor call."""

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id, "rippleSpec": {"actions": {}}}

    monkeypatch.setattr(pockets_service, "get", _get_pocket)

    res = client.post(
        "/pockets/pocket-1/actions/run",
        json={"action": "ghost", "path": "/x"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["code"] == "action_not_found"


def test_run_action_400_when_no_backend(monkeypatch, client):
    spec = {"actions": {"a": {"kind": "write_binding", "method": "POST", "path": "/x"}}}

    async def _get_pocket(pocket_id, user_id):
        return {"_id": pocket_id, "rippleSpec": spec}

    async def _no_creds(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(pockets_service, "get", _get_pocket)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _no_creds)

    res = client.post("/pockets/pocket-1/actions/run", json={"action": "a", "path": "/x"})
    assert res.status_code == 400, res.text


def test_run_action_forbidden_for_non_invited(monkeypatch):
    """The action-run gate is owner OR explicit shared_with ONLY — a
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
        raise Forbidden("pocket.access_denied", "write-action access required")

    a.dependency_overrides[require_pocket_action_run] = _deny

    res = TestClient(a).post("/pockets/pocket-1/actions/run", json={"action": "a", "path": "/x"})
    assert res.status_code == 403
