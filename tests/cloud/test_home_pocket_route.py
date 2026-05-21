# tests/cloud/test_home_pocket_route.py — Home-as-Pocket endpoint coverage.
# Created: 2026-05-21 — Integration coverage for the home-pocket route on the
# pockets router:
#
#   GET /pockets/home  → {pocket_id, pocket, created}
#
# The route resolves-or-provisions the caller's home pocket via
# ``ensure_home_pocket``. ``ensure_home_pocket`` is monkey-patched to return a
# canned ``(pocket_dict, created)`` tuple so the test stays independent of
# Beanie + MongoDB — same pattern as ``test_pocket_layout_routes.py``. Auth +
# license guards are overridden via ``app.dependency_overrides``.
#
# Updated: 2026-05-21 — added the ``created`` flag to the response envelope so
# the client can gate one-time widget seeding / localStorage migration; the
# route now surfaces what path ``ensure_home_pocket`` took.
#
# What this pins:
#   1. GET /pockets/home returns 200 with {pocket_id, pocket, created}.
#   2. The response carries the full pocket (rippleSpec / widgets).
#   3. ``created`` reflects whether ensure_home_pocket provisioned a new
#      pocket (True) or returned an existing one (False).
#   4. The static /home route is matched ahead of the /{pocket_id} route.

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


def _make_app(monkeypatch: pytest.MonkeyPatch, *, created: bool) -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler

    a = FastAPI()
    add_error_handler(a)
    a.include_router(router)

    async def _fake_ensure(workspace_id: str, user_id: str) -> tuple[dict, bool]:
        assert workspace_id == FAKE_WORKSPACE
        assert user_id == FAKE_USER
        return dict(HOME_POCKET), created

    monkeypatch.setattr(pockets_service, "ensure_home_pocket", _fake_ensure)

    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[current_user_id] = lambda: FAKE_USER
    a.dependency_overrides[current_workspace_id] = lambda: FAKE_WORKSPACE
    return a


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    return _make_app(monkeypatch, created=False)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_get_home_pocket_returns_pocket_id_pocket_and_created(client: TestClient) -> None:
    res = client.get("/pockets/home")
    assert res.status_code == 200, res.text

    body = res.json()
    assert body["pocket_id"] == "home-pocket-1"
    assert body["pocket"]["type"] == "home"
    assert body["pocket"]["name"] == "Home"
    # The full pocket — rippleSpec / widgets — rides the response.
    assert "widgets" in body["pocket"]
    assert "rippleSpec" in body["pocket"]
    # The provisioning flag rides the envelope alongside the pocket.
    assert body["created"] is False


@pytest.mark.parametrize("created", [True, False])
def test_get_home_pocket_surfaces_created_flag(
    monkeypatch: pytest.MonkeyPatch, created: bool
) -> None:
    app = _make_app(monkeypatch, created=created)
    res = TestClient(app).get("/pockets/home")
    assert res.status_code == 200, res.text
    assert res.json()["created"] is created


def test_get_home_route_matches_ahead_of_pocket_id_route(client: TestClient) -> None:
    # If /{pocket_id} shadowed /home, "home" would be treated as a pocket id
    # and the response would not carry the {pocket_id, pocket, created} envelope.
    res = client.get("/pockets/home")
    assert res.status_code == 200, res.text
    assert set(res.json().keys()) == {"pocket_id", "pocket", "created"}
