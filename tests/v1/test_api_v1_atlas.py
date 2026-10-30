# tests/v1/test_api_v1_atlas.py — GET /api/v1/atlas/{surfaces,verbs,search}.
# Created: 2026-10-01 (feat/atlas-canonical). The composer is built against a
# fixed wire contract, so these pin the exact key sets and value domains, the
# signed-in requirement, the limit cap, the q cap, the kinds filter, the score
# range, and that role-gated (admin) capability cards never leave through it.
# Review pass (same branch): an active user is required (ee_user_authenticated);
# the overlay is role-aware (owner sees surface:security, member doesn't, an
# unresolved role fails closed) and gets the request's workspace as ws:<id>;
# kinds is capped at 64 chars.
# Live tie fix: navigational queries land on their surface with a clear margin
# over every verb ("show me my files" used to tie files / file-delete /
# file-download at 0.656), and the action words still pick their verb.
# The overlay surface counts are pinned (34 for an owner, 33 for a member), so an
# added surface is a deliberate pin bump.

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1 import atlas as atlas_api
from pocketpaw.api.v1.atlas import MAX_LIMIT, router
from pocketpaw.atlas.overlay import ROLE_LEVELS, entry_role_requirement

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
    return _client(user_id="u1", workspace_id="w1", ee_user_authenticated=True)


def _is_str_or_none(v) -> bool:
    return v is None or isinstance(v, str)


class TestShapes:
    def test_surfaces(self, client):
        body = client.get("/api/v1/atlas/surfaces").json()
        assert set(body) == {"surfaces"}
        surfaces = body["surfaces"]
        assert len(surfaces) >= 28
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


class TestNavigationBeatsVerbs:
    @pytest.mark.parametrize(
        ("q", "surface_id"),
        [
            ("show me my files", "surface:files"),
            ("open my files", "surface:files"),
            ("my files", "surface:files"),
            ("go to chat", "surface:chat"),
            ("show tasks", "surface:mission-control"),
        ],
    )
    def test_surface_wins_with_a_clear_margin(self, client, q, surface_id):
        results = client.get(
            "/api/v1/atlas/search", params={"q": q, "kinds": "surface,verb", "limit": 20}
        ).json()["results"]
        top = results[0]
        assert top["id"] == surface_id
        verb_scores = [r["score"] for r in results if r["kind"] == "verb"]
        assert all(v <= top["score"] - 0.15 for v in verb_scores), (q, results[:4])

    @pytest.mark.parametrize("kinds", [None, "surface,verb"])
    def test_show_me_my_files_leads_every_result_by_the_open_margin(self, client, kinds):
        """The composer opens a surface at score >= 0.45 with a >= 0.15 lead."""
        params = {"q": "show me my files", "limit": 20}
        if kinds:
            params["kinds"] = kinds
        results = client.get("/api/v1/atlas/search", params=params).json()["results"]
        top, rest = results[0], results[1:]
        assert top["id"] == "surface:files" and top["score"] >= 0.45
        assert all(r["score"] <= top["score"] - 0.15 for r in rest), results[:3]

    def test_show_me_my_files_no_longer_ties_a_delete(self, client):
        results = client.get(
            "/api/v1/atlas/search", params={"q": "show me my files", "kinds": "surface,verb"}
        ).json()["results"]
        scores = {r["id"]: r["score"] for r in results}
        assert results[0]["id"] == "surface:files"
        assert scores.get("verb:file-delete", 0) <= scores["surface:files"] / 2

    @pytest.mark.parametrize(
        ("q", "verb_id"),
        [("delete this file", "verb:file-delete"), ("download this file", "verb:file-download")],
    )
    def test_action_word_still_picks_the_verb(self, client, q, verb_id):
        results = client.get(
            "/api/v1/atlas/search", params={"q": q, "kinds": "surface,verb"}
        ).json()["results"]
        assert results[0]["id"] == verb_id
        assert results[0]["score"] >= results[1]["score"] + 0.15


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

    def test_kinds_over_64_chars_is_422(self, client):
        kinds = ",".join(["surface"] * 9)  # 71 chars, every value valid
        resp = client.get("/api/v1/atlas/search", params={"q": "file", "kinds": kinds})
        assert resp.status_code == 422

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


class _FakeRoleProvider:
    """Stands in for the EE role-aware provider: a fixed role, primed or not."""

    def __init__(self, role: str | None, *, prime_raises: bool = False):
        self.role = role
        self.prime_raises = prime_raises
        self._level: int | None = None

    async def prime(self) -> None:
        self._level = None
        if self.prime_raises:
            raise RuntimeError("db down")
        self._level = ROLE_LEVELS.get(self.role) if self.role else None

    def connected_connector_names(self) -> set[str]:
        return set()

    def is_granted(self, entry) -> bool:
        tier = entry_role_requirement(entry)
        if tier is None:
            return True
        return self._level is not None and self._level >= ROLE_LEVELS.get(tier, 99)


def _with_role(monkeypatch, role, **kw):
    calls: list[tuple[str, str | None]] = []

    def factory(scope_key, user_id=None):
        calls.append((scope_key, user_id))
        return _FakeRoleProvider(role, **kw)

    monkeypatch.setattr(atlas_api, "build_role_aware_provider", factory)
    return calls


def _surface_ids(client) -> set[str]:
    return {s["id"] for s in client.get("/api/v1/atlas/surfaces").json()["surfaces"]}


class TestOverlay:
    def test_owner_sees_owner_gated_surface(self, client, monkeypatch):
        _with_role(monkeypatch, "owner")
        ids = _surface_ids(client)
        assert "surface:security" in ids and len(ids) == 35

    def test_member_does_not(self, client, monkeypatch):
        _with_role(monkeypatch, "member")
        ids = _surface_ids(client)
        assert "surface:security" not in ids and len(ids) == 34

    @pytest.mark.parametrize("kw", [{"role": None}, {"role": "owner", "prime_raises": True}])
    def test_unresolved_role_fails_closed(self, client, monkeypatch, kw):
        role = kw.pop("role")
        _with_role(monkeypatch, role, **kw)
        assert "surface:security" not in _surface_ids(client)

    def test_no_role_aware_provider_fails_closed(self, client, monkeypatch):
        monkeypatch.setattr(atlas_api, "build_role_aware_provider", lambda *a, **k: None)
        assert "surface:security" not in _surface_ids(client)

    def test_request_workspace_reaches_the_provider_as_ws_scope(self, monkeypatch):
        calls = _with_role(monkeypatch, "member")
        c = _client(user_id="u7", workspace_id="w42", ee_user_authenticated=True)
        assert c.get("/api/v1/atlas/verbs").status_code == 200
        assert calls == [("ws:w42", "u7")]

    def test_admin_capabilities_follow_the_role(self, client, monkeypatch):
        _with_role(monkeypatch, "member")
        member = client.get(
            "/api/v1/atlas/search", params={"q": "remove a user", "kinds": "capability"}
        ).json()["results"]
        _with_role(monkeypatch, "owner")
        owner = client.get(
            "/api/v1/atlas/search", params={"q": "remove a user", "kinds": "capability"}
        ).json()["results"]
        assert "capability:admin.member_remove" not in {r["id"] for r in member}
        assert owner[0]["id"] == "capability:admin.member_remove"

    def test_role_gated_capabilities_hidden_without_a_role(self, client, monkeypatch):
        """Admin capability cards carry role:* markers; with no resolvable role
        they stay hidden, even for an exact-name query."""
        monkeypatch.setattr(atlas_api, "build_role_aware_provider", lambda *a, **k: None)
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

    def test_active_signed_in_user_is_allowed(self):
        c = _client(user_id="u1", ee_user_authenticated=True)
        assert c.get("/api/v1/atlas/surfaces").status_code == 200

    @pytest.mark.parametrize("path", ["surfaces", "verbs", "search?q=file"])
    def test_inactive_or_guest_user_is_refused(self, path):
        """user_id alone (a verified JWT for an inactive user) is not enough."""
        assert _client(user_id="u1").get(f"/api/v1/atlas/{path}").status_code == 403

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
