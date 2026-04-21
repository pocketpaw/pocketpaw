# Scope enforcement + tool profile fail-closed tests.
# Added: 2026-04-16 for security sprint cluster B (#888, #889).

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.deps import require_scope

# Every test in this module needs the real fail-closed behaviour — opt out
# of the _TESTING_FULL_ACCESS bypass that the root conftest sets up.
pytestmark = pytest.mark.enforce_scope


class _APIKey:
    def __init__(self, scopes: list[str]):
        self.scopes = scopes


class _OAuthToken:
    def __init__(self, scope: str):
        self.scope = scope


def _build_app_with_state(**state_kwargs):
    """FastAPI app that sets request.state from kwargs and has one protected route."""
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        for k, v in state_kwargs.items():
            setattr(request.state, k, v)
        return await call_next(request)

    @app.get("/protected", dependencies=[Depends(require_scope("memory"))])
    async def protected():
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# #888 — scope bypass via master/session/cookie auth
# ---------------------------------------------------------------------------


class TestRequireScopeNoFullAccessMarker:
    """Without an explicit full_access marker, scopeless requests must be rejected.

    Today the silent fallback at the end of require_scope() lets master,
    session, cookie, and localhost auth through without any check.
    After the fix, they must set request.state.full_access = True explicitly.
    """

    def test_request_with_no_auth_markers_is_rejected(self):
        app = _build_app_with_state(api_key=None, oauth_token=None)
        resp = TestClient(app).get("/protected")
        assert resp.status_code == 403, (
            "require_scope must fail closed when no auth marker is set"
        )

    def test_request_with_full_access_marker_is_allowed(self):
        app = _build_app_with_state(api_key=None, oauth_token=None, full_access=True)
        resp = TestClient(app).get("/protected")
        assert resp.status_code == 200

    def test_apikey_without_required_scope_is_rejected(self):
        app = _build_app_with_state(
            api_key=_APIKey(scopes=["chat"]), oauth_token=None
        )
        resp = TestClient(app).get("/protected")
        assert resp.status_code == 403

    def test_apikey_with_required_scope_is_allowed(self):
        app = _build_app_with_state(
            api_key=_APIKey(scopes=["memory"]), oauth_token=None
        )
        resp = TestClient(app).get("/protected")
        assert resp.status_code == 200

    def test_apikey_with_admin_scope_is_allowed(self):
        app = _build_app_with_state(
            api_key=_APIKey(scopes=["admin"]), oauth_token=None
        )
        resp = TestClient(app).get("/protected")
        assert resp.status_code == 200

    def test_oauth_without_required_scope_is_rejected(self):
        app = _build_app_with_state(
            api_key=None, oauth_token=_OAuthToken(scope="chat")
        )
        resp = TestClient(app).get("/protected")
        assert resp.status_code == 403

    def test_oauth_with_required_scope_is_allowed(self):
        app = _build_app_with_state(
            api_key=None, oauth_token=_OAuthToken(scope="memory chat")
        )
        resp = TestClient(app).get("/protected")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# #889 — tool_profile fail-open on unknown name
# ---------------------------------------------------------------------------


class TestToolPolicyUnknownProfile:
    def test_unknown_profile_raises_at_construction(self):
        from pocketpaw.tools.policy import ToolPolicy

        with pytest.raises(ValueError, match="Unknown tool profile"):
            ToolPolicy(profile="this-profile-does-not-exist")

    def test_valid_profile_constructs(self):
        from pocketpaw.tools.policy import ToolPolicy

        pol = ToolPolicy(profile="minimal")
        # minimal allows memory + sessions + explorer
        assert pol.is_tool_allowed("remember") is True
        # but not shell
        assert pol.is_tool_allowed("shell") is False

    def test_full_profile_is_unrestricted(self):
        from pocketpaw.tools.policy import ToolPolicy

        pol = ToolPolicy(profile="full")
        assert pol.is_tool_allowed("shell") is True
        assert pol.is_tool_allowed("any_unknown_tool") is True
