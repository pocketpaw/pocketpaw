# tests/v1/test_api_v1_atlas.py — GET /api/v1/atlas/{surfaces,verbs,search}.
# Created: 2026-10-01 (feat/atlas-canonical). The composer is built against a
# fixed wire contract, so these pin the exact key sets and value domains, the
# signed-in requirement, the limit cap, the q cap, the kinds filter, the score
# range, and that role-gated (admin) capability cards never leave through it.

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.atlas import MAX_LIMIT, router

SURFACE_KEYS = {
    "id",
    "name",
    "summary",
    "route",
    "slash",
    "presentation",
    "agent_openable",
    "keywords",
}
VERB_KEYS = {
    "id",
    "name",
    "summary",
    "slash",
    "applies_to",
    "triggers",
    "risk",
    "undo",
    "keywords",
}
RESULT_KEYS = {"id", "kind", "name", "route", "slash", "score"}


def _client(**state) -> TestClient:
    """App whose middleware stamps request.state the way the auth layers do."""
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        for k, v in state.items():
            setattr(request.state, k, v)
        return await call_next(request)

    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return _client(user_id="u1", workspace_id="w1")


def _is_str_or_none(v) -> bool:
    return v is None or isinstance(v, str)


class TestShapes:
    def test_surfaces(self, client):
        body = client.get("/api/v1/atlas/surfaces").json()
        assert set(body) == {"surfaces"}
        surfaces = body["surfaces"]
        # 24 authored; surface:security is owner-gated (role:owner) and the read
        # API has no role context, so the overlay hides it (fail-closed).
        assert len(surfaces) == 23
        assert "surface:security" not in {s["id"] for s in surfaces}
        for s in surfaces:
            assert set(s) == SURFACE_KEYS
            assert s["id"].startswith("surface:") and s["route"].startswith("/")
            assert _is_str_or_none(s["slash"])
            assert s["presentation"] in ("inline", "window")
            assert isinstance(s["agent_openable"], bool)
            assert isinstance(s["keywords"], list)
        files = next(s for s in surfaces if s["id"] == "surface:files")
        assert files["slash"] == "files" and files["presentation"] == "inline"
        assert files["agent_openable"] is True

    def test_verbs(self, client):
        body = client.get("/api/v1/atlas/verbs").json()
        assert set(body) == {"verbs"}
        verbs = body["verbs"]
        assert len(verbs) >= 30
        for v in verbs:
            assert set(v) == VERB_KEYS
            assert v["id"].startswith("verb:")
            assert _is_str_or_none(v["slash"])
            assert v["applies_to"] and isinstance(v["applies_to"], list)
            assert v["triggers"] and set(v["triggers"]) <= {"slash", "verb", "agent"}
            assert v["risk"] in ("read", "safe", "risky")
            assert isinstance(v["undo"], bool)
        send = next(v for v in verbs if v["id"] == "verb:send")
        assert send["slash"] == "send" and send["risk"] == "risky"
        assert send["applies_to"] == ["channel"]

    def test_search(self, client):
        body = client.get("/api/v1/atlas/search", params={"q": "rename this file"}).json()
        assert set(body) == {"query", "results"}
        assert body["query"] == "rename this file"
        assert body["results"][0]["id"] == "verb:file-rename"
        for r in body["results"]:
            assert set(r) == RESULT_KEYS
            assert r["kind"] in ("surface", "verb", "capability", "primitive")
            assert _is_str_or_none(r["route"]) and _is_str_or_none(r["slash"])
            assert 0.0 <= r["score"] <= 1.0
        scores = [r["score"] for r in body["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_surface_result_carries_route_and_slash(self, client):
        body = client.get(
            "/api/v1/atlas/search", params={"q": "deep work", "kinds": "surface"}
        ).json()
        top = body["results"][0]
        assert top["id"] == "surface:mission-control"
        assert top["route"] == "/deep-work" and top["slash"] == "deep-work"

    def test_full_name_match_scores_one(self, client):
        """A primitive whose name matches every query word, and no other name
        shares the word, is the 1.0 ceiling."""
        body = client.get("/api/v1/atlas/search", params={"q": "Verify loop"}).json()
        assert body["results"][0] == {
            "id": "primitive:verify-loop",
            "kind": "primitive",
            "name": "Verify loop",
            "route": "/deep-work",
            "slash": None,
            "score": 1.0,
        }

    def test_no_match_is_an_empty_list(self, client):
        body = client.get("/api/v1/atlas/search", params={"q": "zzqx"}).json()
        assert body == {"query": "zzqx", "results": []}


class TestSearchParams:
    def test_kinds_filter(self, client):
        body = client.get(
            "/api/v1/atlas/search", params={"q": "file", "kinds": "verb", "limit": 20}
        ).json()
        assert body["results"] and {r["kind"] for r in body["results"]} == {"verb"}

    def test_default_kinds_exclude_widgets_connectors_skills_senses(self, client):
        # "kanban" is a widget and "stripe" a connector — neither may leak out.
        for q in ("kanban board", "stripe invoices", "studio skill", "payments"):
            body = client.get("/api/v1/atlas/search", params={"q": q, "limit": 20}).json()
            kinds = {r["kind"] for r in body["results"]}
            assert kinds <= {"surface", "verb", "capability", "primitive"}, (q, kinds)

    def test_unknown_kind_is_422(self, client):
        resp = client.get("/api/v1/atlas/search", params={"q": "file", "kinds": "widget"})
        assert resp.status_code == 422

    def test_limit_is_capped(self, client):
        body = client.get(
            "/api/v1/atlas/search", params={"q": "file task site", "limit": 500}
        ).json()
        assert len(body["results"]) == MAX_LIMIT

    def test_limit_default_is_five(self, client):
        body = client.get("/api/v1/atlas/search", params={"q": "file task site"}).json()
        assert len(body["results"]) == 5

    @pytest.mark.parametrize("limit", [0, -1])
    def test_limit_below_one_is_422(self, client, limit):
        resp = client.get("/api/v1/atlas/search", params={"q": "file", "limit": limit})
        assert resp.status_code == 422

    def test_query_over_200_chars_is_422(self, client):
        assert client.get("/api/v1/atlas/search", params={"q": "a" * 201}).status_code == 422
        assert client.get("/api/v1/atlas/search", params={"q": "a" * 200}).status_code == 200

    def test_query_required(self, client):
        assert client.get("/api/v1/atlas/search").status_code == 422
        assert client.get("/api/v1/atlas/search", params={"q": ""}).status_code == 422


class TestOverlay:
    def test_role_gated_capabilities_never_leave(self, client):
        """Admin capability cards carry role:* markers; the read API has no role
        context, so they stay hidden — even for an exact-name query."""
        for q in ("remove a user", "delete the workspace", "change a member's role"):
            body = client.get(
                "/api/v1/atlas/search", params={"q": q, "kinds": "capability", "limit": 20}
            ).json()
            assert body["results"] == [], q


@pytest.mark.enforce_scope
class TestAuth:
    @pytest.mark.parametrize("path", ["surfaces", "verbs", "search?q=file"])
    def test_anonymous_caller_is_refused(self, path):
        assert _client().get(f"/api/v1/atlas/{path}").status_code == 403

    def test_signed_in_cloud_member_is_allowed(self):
        assert _client(user_id="u1").get("/api/v1/atlas/surfaces").status_code == 200

    def test_api_key_without_chat_scope_is_refused(self):
        class _Key:
            scopes = ["memory"]

        assert _client(api_key=_Key()).get("/api/v1/atlas/verbs").status_code == 403

    def test_api_key_with_chat_scope_is_allowed(self):
        class _Key:
            scopes = ["chat"]

        assert _client(api_key=_Key()).get("/api/v1/atlas/verbs").status_code == 200


def test_router_is_mounted_under_v1():
    from pocketpaw.api.v1 import _V1_ROUTERS

    assert ("pocketpaw.api.v1.atlas", "router", "Atlas") in _V1_ROUTERS
