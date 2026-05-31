"""Tests for ee.cloud._core.context — RequestContext and FastAPI dependency."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context


class TestScopeKind:
    def test_known_values(self) -> None:
        assert ScopeKind.WORKSPACE.value == "workspace"
        assert ScopeKind.SESSION.value == "session"
        assert ScopeKind.POCKET.value == "pocket"
        assert ScopeKind.GROUP.value == "group"
        assert ScopeKind.DM.value == "dm"
        assert ScopeKind.NONE.value == "none"

    def test_is_str_enum(self) -> None:
        # Behaves as a str so it can be used in path/query templating
        assert ScopeKind.SESSION == "session"


class TestRequestContext:
    def test_construct(self) -> None:
        started = datetime.now(UTC)
        ctx = RequestContext(
            user_id="u1",
            workspace_id="w1",
            request_id="req-abc",
            scope=ScopeKind.WORKSPACE,
            started_at=started,
        )
        assert ctx.user_id == "u1"
        assert ctx.workspace_id == "w1"
        assert ctx.request_id == "req-abc"
        assert ctx.scope is ScopeKind.WORKSPACE
        assert ctx.started_at == started

    def test_is_frozen(self) -> None:
        ctx = RequestContext(
            user_id="u1",
            workspace_id=None,
            request_id="r",
            scope=ScopeKind.NONE,
            started_at=datetime.now(UTC),
        )
        with pytest.raises(FrozenInstanceError):
            ctx.user_id = "u2"  # type: ignore[misc]

    def test_workspace_id_optional(self) -> None:
        ctx = RequestContext(
            user_id="u1",
            workspace_id=None,
            request_id="r",
            scope=ScopeKind.NONE,
            started_at=datetime.now(UTC),
        )
        assert ctx.workspace_id is None


class _FakeUser:
    """Minimal stand-in for ee.cloud.models.user.User."""

    def __init__(self, id: str, active_workspace: str | None) -> None:
        self.id = id
        self.active_workspace = active_workspace


@pytest.fixture
def app_with_context_route() -> FastAPI:
    """Build a tiny FastAPI app that exposes RequestContext via the dep.

    Uses FastAPI's `dependency_overrides` (not monkeypatch) so the
    swap reaches the dependency reference captured by the inner
    `Depends(current_active_user)` inside `request_context`. Patching
    the module attribute would not.
    """
    from pocketpaw_ee.cloud.auth import current_active_user

    fake_user = _FakeUser(id="user-1", active_workspace="ws-42")

    async def _fake_current_active_user() -> _FakeUser:
        return fake_user

    app = FastAPI()
    app.dependency_overrides[current_active_user] = _fake_current_active_user

    @app.get("/_test/ctx")
    async def show_ctx(ctx: RequestContext = Depends(request_context)) -> dict:
        return {
            "user_id": ctx.user_id,
            "workspace_id": ctx.workspace_id,
            "request_id": ctx.request_id,
            "scope": ctx.scope.value,
        }

    return app


def test_request_context_dep_populates_user_and_workspace(
    app_with_context_route: FastAPI,
) -> None:
    client = TestClient(app_with_context_route)
    resp = client.get("/_test/ctx", headers={"x-request-id": "abc-123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "user-1"
    assert body["workspace_id"] == "ws-42"
    assert body["request_id"] == "abc-123"
    assert body["scope"] == "none"


def test_request_context_dep_generates_request_id_when_header_missing(
    app_with_context_route: FastAPI,
) -> None:
    client = TestClient(app_with_context_route)
    resp = client.get("/_test/ctx")
    assert resp.status_code == 200
    body = resp.json()
    # No header → some 32-hex-char request id was generated
    assert isinstance(body["request_id"], str)
    assert len(body["request_id"]) == 32
