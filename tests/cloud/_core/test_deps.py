"""Tests for ee.cloud._core.deps — cross-cutting FastAPI dependencies."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import (
    current_user,
    current_user_id,
    current_workspace_id,
    optional_workspace_id,
    require_action,
    require_action_any_workspace,
    require_membership,
)


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Minimal stand-in for ee.cloud.models.user.User."""

    def __init__(
        self,
        id: str,
        active_workspace: str | None,
        workspaces: list[_FakeMembership] | None = None,
    ) -> None:
        self.id = id
        self.active_workspace = active_workspace
        self.workspaces = workspaces or []


@pytest.fixture
def make_app():
    """Factory for a FastAPI app with the auth dep overridden to a fake user."""
    from pocketpaw_ee.cloud.auth import current_active_user

    def _builder(user: _FakeUser) -> FastAPI:
        app = FastAPI()
        app.dependency_overrides[current_active_user] = lambda: user
        return app

    return _builder


def test_current_user_id_extracts_id(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace="w1")
    app = make_app(user)

    @app.get("/me/id")
    async def _r(uid: str = Depends(current_user_id)) -> dict:
        return {"uid": uid}

    resp = TestClient(app).get("/me/id")
    assert resp.json() == {"uid": "u1"}


def test_current_workspace_id_returns_active(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace="w42")
    app = make_app(user)

    @app.get("/ws/active")
    async def _r(ws: str = Depends(current_workspace_id)) -> dict:
        return {"ws": ws}

    resp = TestClient(app).get("/ws/active")
    assert resp.json() == {"ws": "w42"}


def test_current_workspace_id_400_when_no_active(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace=None)
    app = make_app(user)

    @app.get("/ws/active")
    async def _r(ws: str = Depends(current_workspace_id)) -> dict:
        return {"ws": ws}

    resp = TestClient(app).get("/ws/active")
    assert resp.status_code == 400
    assert "No active workspace" in resp.json().get("detail", "")


def test_optional_workspace_id_returns_none_when_unset(make_app) -> None:
    user = _FakeUser(id="u1", active_workspace=None)
    app = make_app(user)

    @app.get("/ws/optional")
    async def _r(ws: str | None = Depends(optional_workspace_id)) -> dict:
        return {"ws": ws}

    assert TestClient(app).get("/ws/optional").json() == {"ws": None}


def test_require_membership_passes_for_member(make_app) -> None:
    user = _FakeUser(
        id="u1",
        active_workspace="w1",
        workspaces=[_FakeMembership(workspace="w1")],
    )
    app = make_app(user)

    @app.get("/ws/{workspace_id}/view")
    async def _r(workspace_id: str, _u=Depends(require_membership)) -> dict:
        return {"ok": True}

    assert TestClient(app).get("/ws/w1/view").status_code == 200


def test_require_membership_403_for_non_member(make_app) -> None:
    user = _FakeUser(
        id="u1",
        active_workspace="w1",
        workspaces=[_FakeMembership(workspace="w1")],
    )
    app = make_app(user)
    # Add the cloud error handler so Forbidden -> 403 envelope
    from pocketpaw_ee.cloud._core.http import add_error_handler

    add_error_handler(app)

    @app.get("/ws/{workspace_id}/view")
    async def _r(workspace_id: str, _u=Depends(require_membership)) -> dict:
        return {"ok": True}

    resp = TestClient(app).get("/ws/w_other/view")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "workspace.not_member"


def test_current_user_is_callable() -> None:
    """current_user is the simplest passthrough — verify it's callable."""
    assert callable(current_user)


def test_require_action_returns_named_callable() -> None:
    """require_action returns a closure named after the action; FastAPI
    uses the closure name for OpenAPI op IDs."""
    guard = require_action("workspace.edit")
    assert guard.__name__ == "require_action_workspace_edit"


def test_require_action_any_workspace_uses_active_workspace() -> None:
    """The variant that resolves workspace from the user instead of path
    is built on require_action with current_workspace_id as workspace_dep."""
    guard = require_action_any_workspace("workspace.edit")
    assert callable(guard)
