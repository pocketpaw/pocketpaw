# tests/cloud/test_pocket_layout_routes.py — Cluster B Sub-PR #3.
# Created: 2026-04-19 — Integration coverage for the three new routes on
# the pockets router that ship the layout save/share surface:
#
#   POST /pockets/{id}/export-layout
#   POST /pockets/templates
#   GET  /pockets/templates
#
# PocketService.get is monkey-patched to return a canned pocket dict so
# the tests stay independent of Beanie + MongoDB. Auth dependencies and
# the user-template store are overridden via app.dependency_overrides —
# same pattern the widget-router tests use.
#
# What this pins:
#   1. /export-layout returns a YAML document carrying the pocket's
#      ripple_spec under `spec:`.
#   2. /templates POST parses the YAML and stores a row scoped to the
#      caller's workspace.
#   3. /templates GET lists only the workspace's rows — no leak
#      across workspaces.
#   4. Malformed YAML yields 400 with a human-readable detail, not 500.
#   5. Round-trip: /export-layout → /templates POST → GET lists the row
#      and the stored spec equals the source pocket's rippleSpec.

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud.license import require_license
from ee.cloud.pockets.layouts import (
    UserTemplateStore,
    get_user_template_store,
    parse_layout_yaml,
    reset_user_template_store,
)
from ee.cloud.pockets.router import router
from ee.cloud.pockets.service import PocketService
from ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_pocket_edit,
    require_pocket_owner,
)


FAKE_WORKSPACE = "ws-alpha"
FAKE_USER = "user-alice"

POCKET_FIXTURE: dict[str, Any] = {
    "_id": "pocket-1",
    "workspace": FAKE_WORKSPACE,
    "name": "Sales Dashboard",
    "description": "Q1 pipeline view",
    "type": "business",
    "icon": "bar-chart",
    "color": "#0A84FF",
    "owner": FAKE_USER,
    "visibility": "workspace",
    "team": [],
    "agents": [],
    "widgets": [],
    "rippleSpec": {
        "widgets": [
            {"id": "w1", "type": "pipeline", "title": "Pipeline"},
            {"id": "w2", "type": "leads", "title": "Leads"},
        ],
        "layout": "2-col",
    },
}


@pytest.fixture(autouse=True)
def _reset_store():
    reset_user_template_store()
    yield
    reset_user_template_store()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    a = FastAPI()
    a.include_router(router)

    async def _fake_get(pocket_id: str, user_id: str) -> dict:
        return dict(POCKET_FIXTURE, _id=pocket_id)

    monkeypatch.setattr(PocketService, "get", staticmethod(_fake_get))

    # Skip the license/auth guards entirely for the integration tests —
    # the point here is the new surface, not the guard wiring.
    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[require_pocket_edit] = lambda: None
    a.dependency_overrides[require_pocket_owner] = lambda: None
    a.dependency_overrides[current_user_id] = lambda: FAKE_USER
    a.dependency_overrides[current_workspace_id] = lambda: FAKE_WORKSPACE

    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. /export-layout happy path.
# ---------------------------------------------------------------------------


class TestExportLayout:
    def test_returns_yaml_carrying_the_rippleSpec(self, client: TestClient) -> None:
        res = client.post("/pockets/pocket-1/export-layout", json={})
        assert res.status_code == 200, res.text

        body = res.json()
        assert body["pocket_id"] == "pocket-1"
        yaml_text = body["yaml"]
        assert "kind: PocketLayout" in yaml_text

        recovered = parse_layout_yaml(yaml_text)
        assert recovered == POCKET_FIXTURE["rippleSpec"]

    def test_metadata_overrides_take_effect(self, client: TestClient) -> None:
        res = client.post(
            "/pockets/pocket-1/export-layout",
            json={"name": "Shared Dashboard", "description": "Shipped", "category": "custom"},
        )
        assert res.status_code == 200
        yaml_text = res.json()["yaml"]
        assert "name: Shared Dashboard" in yaml_text
        assert "description: Shipped" in yaml_text


# ---------------------------------------------------------------------------
# 2. /templates POST + GET happy path.
# ---------------------------------------------------------------------------


class TestCreateAndListTemplate:
    def test_create_then_list_shows_the_template(self, client: TestClient) -> None:
        # Start from an export; the YAML the UI POSTs is almost always
        # a round-trip of a /export-layout response.
        export_res = client.post("/pockets/pocket-1/export-layout", json={})
        assert export_res.status_code == 200
        yaml_text = export_res.json()["yaml"]

        create_res = client.post(
            "/pockets/templates",
            json={
                "name": "Sales Dashboard (shared)",
                "description": "",
                "category": "business",
                "yaml_source": yaml_text,
            },
        )
        assert create_res.status_code == 200, create_res.text
        created = create_res.json()
        assert created["name"] == "Sales Dashboard (shared)"
        assert created["workspace_id"] == FAKE_WORKSPACE
        assert created["owner_id"] == FAKE_USER
        # Spec round-trips cleanly.
        assert created["spec"] == POCKET_FIXTURE["rippleSpec"]

        list_res = client.get("/pockets/templates")
        assert list_res.status_code == 200
        templates = list_res.json()
        assert len(templates) == 1
        assert templates[0]["id"] == created["id"]


# ---------------------------------------------------------------------------
# 3. Workspace scoping.
# ---------------------------------------------------------------------------


class TestWorkspaceScoping:
    def test_other_workspace_templates_do_not_surface(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """Seed one template under workspace Beta via the store, then
        confirm the GET under Alpha returns nothing.
        """

        store: UserTemplateStore = app.dependency_overrides[get_user_template_store]() \
            if get_user_template_store in app.dependency_overrides \
            else get_user_template_store()

        from ee.cloud.pockets.layouts import UserPocketTemplate

        store.save(
            UserPocketTemplate(
                id="beta-row",
                workspace_id="ws-beta",
                owner_id="carol",
                name="Beta template",
                description="",
                category="custom",
                spec={"widgets": []},
            ),
        )

        res = client.get("/pockets/templates")
        assert res.status_code == 200
        assert all(t["workspace_id"] == FAKE_WORKSPACE for t in res.json())


# ---------------------------------------------------------------------------
# 4. Malformed YAML.
# ---------------------------------------------------------------------------


class TestMalformedYaml:
    def test_missing_spec_returns_400(self, client: TestClient) -> None:
        res = client.post(
            "/pockets/templates",
            json={
                "name": "Broken",
                "description": "",
                "category": "custom",
                "yaml_source": "kind: PocketLayout\nname: no-spec",
            },
        )
        assert res.status_code == 400
        assert "spec" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 5. End-to-end round-trip.
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_export_then_create_then_list_reproduces_the_layout(
        self,
        client: TestClient,
    ) -> None:
        """Captain's §11 demo flow: save layout, create a fresh pocket
        from the saved template, and confirm the layout is recoverable.
        The frontend runs the "create new pocket" step; here we only
        check the server-visible artefacts line up.
        """

        export_res = client.post("/pockets/pocket-1/export-layout", json={})
        assert export_res.status_code == 200
        yaml_text = export_res.json()["yaml"]

        create_res = client.post(
            "/pockets/templates",
            json={
                "name": "Round-trip",
                "description": "",
                "category": "business",
                "yaml_source": yaml_text,
            },
        )
        assert create_res.status_code == 200
        new_template = create_res.json()

        list_res = client.get("/pockets/templates")
        assert list_res.status_code == 200
        templates = list_res.json()
        found = [t for t in templates if t["id"] == new_template["id"]]
        assert len(found) == 1
        # The stored spec mirrors the source pocket's rippleSpec.
        assert found[0]["spec"] == POCKET_FIXTURE["rippleSpec"]
