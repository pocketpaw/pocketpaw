# tests/ee/agent/test_atlas_api_roles.py — the atlas read API with the REAL EE
# role-aware provider.
# Created: 2026-10-01 (feat/atlas-canonical, review pass). GET /api/v1/atlas/*
# has the request's verified user + workspace but no chat-run ContextVars, so it
# binds the user into RoleAwareEntitlementProvider. Only the User load is faked
# (``_load_user``); resolve_workspace_role and the grant logic are real. Pins:
# owner sees surface:security, admin and member don't, a non-member and an
# unknown user see no role-gated entry, and the provider resolves the user it
# was handed (not whatever chat identity is bound).
# Updated 2026-10-02 (feat/discover-index): surface counts include surface:discover
# (30 for the owner, 29 for others).

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.agent import atlas_provider as ap

from pocketpaw.api.v1.atlas import router


class _Membership:
    def __init__(self, workspace: str, role: str):
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, memberships: list[_Membership]):
        self.workspaces = memberships


def _client(**state) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        for k, v in state.items():
            setattr(request.state, k, v)
        return await call_next(request)

    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def users(monkeypatch):
    """user_id -> list of (workspace, role); unknown ids load as no user."""
    table: dict[str, list[tuple[str, str]]] = {}

    async def _load(user_id: str):
        rows = table.get(user_id)
        return None if rows is None else _FakeUser([_Membership(w, r) for w, r in rows])

    monkeypatch.setattr(ap.RoleAwareEntitlementProvider, "_load_user", staticmethod(_load))
    return table


def _surfaces(user: str, workspace: str = "w1") -> set[str]:
    c = _client(user_id=user, workspace_id=workspace, ee_user_authenticated=True)
    return {s["id"] for s in c.get("/api/v1/atlas/surfaces").json()["surfaces"]}


def test_owner_sees_security(users):
    users["owner1"] = [("w1", "owner")]
    ids = _surfaces("owner1")
    assert "surface:security" in ids
    assert len(ids) == 30


@pytest.mark.parametrize("role", ["admin", "member"])
def test_admin_and_member_do_not(users, role):
    users["u1"] = [("w1", role)]
    ids = _surfaces("u1")
    assert "surface:security" not in ids
    assert len(ids) == 29


def test_owner_elsewhere_is_not_owner_here(users):
    users["u1"] = [("w2", "owner")]
    assert "surface:security" not in _surfaces("u1", workspace="w1")


def test_unknown_user_fails_closed(users):
    assert "surface:security" not in _surfaces("ghost")


def test_owner_sees_owner_capabilities_in_search(users):
    users["owner1"] = [("w1", "owner")]
    c = _client(user_id="owner1", workspace_id="w1", ee_user_authenticated=True)
    body = c.get(
        "/api/v1/atlas/search", params={"q": "delete the workspace", "kinds": "capability"}
    ).json()
    assert body["results"][0]["id"] == "capability:admin.workspace_delete"


def test_bound_user_wins_over_chat_identity(users):
    """The provider resolves the user it was handed, not a bound chat identity."""
    from pocketpaw_ee.cloud.chat.agent_service import attach_agent_identity, detach_agent_identity

    users["owner1"] = [("w1", "owner")]
    users["member1"] = [("w1", "member")]
    tokens = attach_agent_identity(workspace_id="w1", user_id="owner1")
    try:
        assert "surface:security" not in _surfaces("member1")
    finally:
        detach_agent_identity(tokens)
