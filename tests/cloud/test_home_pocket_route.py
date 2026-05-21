# tests/cloud/test_home_pocket_route.py — Home-as-Pocket endpoint coverage.
# Created: 2026-05-21 — Integration coverage for the home-pocket route on the
# pockets router:
#
#   GET /pockets/home  → {pocket_id, pocket}
#
# The route resolves-or-provisions the caller's home pocket via
# ``ensure_home_pocket``. ``ensure_home_pocket`` is monkey-patched to a canned
# pocket dict so the test stays independent of Beanie + MongoDB — same pattern
# as ``test_pocket_layout_routes.py``. Auth + license guards are overridden via
# ``app.dependency_overrides``.
#
# What this pins:
#   1. GET /pockets/home returns 200 with {pocket_id, pocket}.
#   2. The response carries the full pocket (rippleSpec / widgets).
#   3. The static /home route is matched ahead of the /{pocket_id} route.

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.router import router
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

FAKE_WORKSPACE = "ws-home"
FAKE_USER = "user-home-owner"

HOME_POCKET: dict[str, Any] = {
    "_id": "home-pocket-1",
    "workspace": FAKE_WORKSPACE,
    "name": "Home",
    "description": "",
    "type": "home",
    "icon": "",
    "color": "",
    "owner": FAKE_USER,
    "visibility": "private",
    "team": [],
    "agents": [],
    "widgets": [],
    "rippleSpec": None,
    "shareLinkToken": None,
    "shareLinkAccess": "view",
    "sharedWith": [],
    "projectId": None,
    "createdAt": "2026-05-21T00:00:00Z",
    "updatedAt": "2026-05-21T00:00:00Z",
}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler

    a = FastAPI()
    add_error_handler(a)
    a.include_router(router)

    async def _fake_ensure(workspace_id: str, user_id: str) -> dict:
        assert workspace_id == FAKE_WORKSPACE
        assert user_id == FAKE_USER
        return dict(HOME_POCKET)

    monkeypatch.setattr(pockets_service, "ensure_home_pocket", _fake_ensure)

    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[current_user_id] = lambda: FAKE_USER
    a.dependency_overrides[current_workspace_id] = lambda: FAKE_WORKSPACE
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_get_home_pocket_returns_pocket_id_and_pocket(client: TestClient) -> None:
    res = client.get("/pockets/home")
    assert res.status_code == 200, res.text

    body = res.json()
    assert body["pocket_id"] == "home-pocket-1"
    assert body["pocket"]["type"] == "home"
    assert body["pocket"]["name"] == "Home"
    # The full pocket — rippleSpec / widgets — rides the response.
    assert "widgets" in body["pocket"]
    assert "rippleSpec" in body["pocket"]


def test_get_home_route_matches_ahead_of_pocket_id_route(client: TestClient) -> None:
    # If /{pocket_id} shadowed /home, "home" would be treated as a pocket id
    # and the response would not carry the {pocket_id, pocket} envelope.
    res = client.get("/pockets/home")
    assert res.status_code == 200, res.text
    assert set(res.json().keys()) == {"pocket_id", "pocket"}
