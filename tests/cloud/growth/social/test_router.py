# tests/cloud/growth/social/test_router.py — HTTP-layer tests for Growth ›
# Social (``/api/v1/growth/social/...``). Same harness as the growth router
# tests: the router mounted with a fixed RequestContext per workspace (w1/w2
# clients) over mongomock-backed Beanie (``mongo_db``). The analyser and the
# ideas writer are fakes installed through ``set_production_analyze_fn`` /
# ``set_production_ideas_fn`` and reset after each test: nothing here touches
# the network or a model.
#
# Covers: profile 404 then PUT upsert and partial update (omit leaves, null
# clears, description merges, hand-edited analysis); enum and website 422s;
# analyze success / failure / raise / description-only / 422 with neither /
# 404 / 503; complete 422 then success; ideas generate (409 before complete,
# 503 with no writer, 502 on failure), list by status, patch status and copy;
# tenant isolation for profile and ideas, and a cross-tenant idea patch 404.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
from pocketpaw_ee.cloud.growth.social.analyst import set_production_analyze_fn
from pocketpaw_ee.cloud.growth.social.domain import (
    AnalysisOutcome,
    AnalysisRequest,
    GeneratedIdea,
    SocialAnalysis,
    SocialProfile,
)
from pocketpaw_ee.cloud.growth.social.ideas import set_production_ideas_fn
from pocketpaw_ee.cloud.growth.social.router import router as social_router
from pocketpaw_ee.cloud.license import require_license

BASE = "/api/v1/growth/social"
PROFILE = f"{BASE}/profile"
IDEAS = f"{BASE}/ideas"


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str, workspace_id: str, role: str = "admin") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role=role)]


def _build_app(workspace_id: str = "w1", role: str = "admin") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(social_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    user = _FakeUser("u1", workspace_id, role=role)
    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


async def _client(workspace_id: str, role: str = "admin"):
    transport = ASGITransport(app=_build_app(workspace_id, role))
    return AsyncClient(transport=transport, base_url="http://t")


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with await _client("w2") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_seams():
    try:
        yield
    finally:
        set_production_analyze_fn(None)
        set_production_ideas_fn(None)


class _FakeAnalyzer:
    def __init__(self, outcome: AnalysisOutcome | Exception) -> None:
        self.outcome = outcome
        self.calls: list[AnalysisRequest] = []

    async def __call__(self, request: AnalysisRequest) -> AnalysisOutcome:
        self.calls.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _FakeIdeas:
    def __init__(self, result: tuple[GeneratedIdea, ...] | Exception = ()) -> None:
        self.result = result
        self.calls: list[tuple[SocialProfile, int, list[str]]] = []

    async def __call__(
        self, profile: SocialProfile, count: int, recent_hooks: list[str]
    ) -> tuple[GeneratedIdea, ...]:
        self.calls.append((profile, count, recent_hooks))
        if isinstance(self.result, Exception):
            raise self.result
        if self.result:
            return self.result
        n = len(self.calls)
        return tuple(
            GeneratedIdea(
                format="hook_demo",
                hook=f"Hook {n}-{i}",
                on_screen_text="Text",
                caption="Caption",
                why="Because.",
                script=("Beat one", "Beat two"),
                hashtags=("#dentist",),
            )
            for i in range(count)
        )


_ANALYSIS = SocialAnalysis(
    summary="A gentle family dentist.",
    product="Checkups",
    audience="Parents",
    content_pillars=("Kids", "Comfort"),
    hooks=("No more fear",),
    pages_read=("https://acme.com/",),
    logo_url="https://acme.com/logo.png",
)

_COMPLETE = {
    "owner_name": "Sam",
    "company_name": "Acme Dental",
    "team_size": "2_10",
    "monthly_revenue": "10k_50k",
    "role": "founder",
    "business_model": "local_business",
    "category": "Health",
}


async def _complete(client: AsyncClient) -> dict[str, Any]:
    assert (await client.put(PROFILE, json=_COMPLETE)).status_code == 200
    resp = await client.post(f"{PROFILE}/complete")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_is_404_until_the_first_put(w1):
    resp = await w1.get(PROFILE)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_profile.not_found"


@pytest.mark.asyncio
async def test_put_creates_then_partially_updates(w1):
    resp = await w1.put(
        PROFILE,
        json={
            "owner_name": "Sam",
            "company_name": "Acme",
            "website": "acme.com",
            "description": {"product": "Checkups", "audience": "Parents"},
            "team_size": "solo",
        },
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["workspace_id"] == "w1"
    assert created["website"] == "https://acme.com"
    assert created["description"] == {
        "product": "Checkups",
        "audience": "Parents",
        "problem": "",
        "benefits": "",
        "tone": "",
        "avoid": "",
    }
    assert created["analysis_status"] == "none"
    assert created["analysis"] is None
    assert created["onboarding_completed_at"] is None
    assert created["created_at"] and created["updated_at"]

    resp = await w1.put(
        PROFILE,
        json={"description": {"audience": "Busy parents", "tone": "Warm"}, "team_size": None},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["id"] == created["id"]
    assert updated["owner_name"] == "Sam"
    assert updated["website"] == "https://acme.com"
    assert updated["team_size"] is None
    assert updated["description"]["product"] == "Checkups"
    assert updated["description"]["audience"] == "Busy parents"
    assert updated["description"]["tone"] == "Warm"

    assert (await w1.get(PROFILE)).json() == updated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"team_size": "huge"},
        {"monthly_revenue": "a lot"},
        {"role": "ceo"},
        {"business_model": "b2b"},
        {"category": "x" * 61},
        {"website": "ftp://acme.com"},
        {"website": "not a url"},
        {"analysis": {"hooks": ["h"] * 21}},
        {"analysis": {"benefits": ["x" * 301]}},
        {"analysis": {"summary": "x" * 2001}},
    ],
)
async def test_bad_values_are_422(w1, payload):
    resp = await w1.put(PROFILE, json=payload)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_a_hand_edited_analysis_keeps_server_fields_and_marks_ready(w1):
    await w1.put(PROFILE, json={"company_name": "Acme", "website": "https://acme.com"})
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(analysis=_ANALYSIS)))
    analysed = (await w1.post(f"{PROFILE}/analyze")).json()

    resp = await w1.put(
        PROFILE,
        json={
            "analysis": {
                "summary": "  Edited summary  ",
                "hooks": ["New hook", "  "],
                "pages_read": ["https://forged.example/"],
                "logo_url": "https://forged.example/logo.png",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    edited = resp.json()
    assert edited["analysis"]["summary"] == "Edited summary"
    assert edited["analysis"]["hooks"] == ["New hook"]
    assert edited["analysis"]["product"] == "Checkups"
    assert edited["analysis"]["pages_read"] == ["https://acme.com/"]
    assert edited["analysis"]["logo_url"] == "https://acme.com/logo.png"
    assert edited["analysis_status"] == "ready"
    assert edited["analyzed_at"] == analysed["analyzed_at"]


@pytest.mark.asyncio
async def test_saving_an_analysis_by_hand_turns_none_or_failed_into_ready(w1):
    await w1.put(PROFILE, json={"description": {"product": "Checkups"}})
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(error="Nope.")))
    assert (await w1.post(f"{PROFILE}/analyze")).json()["analysis_status"] == "failed"

    resp = await w1.put(PROFILE, json={"analysis": {"summary": "Typed by hand"}})
    body = resp.json()
    assert body["analysis_status"] == "ready"
    assert body["analysis_error"] is None
    assert body["analyzed_at"] is None
    assert body["analysis"]["summary"] == "Typed by hand"
    assert body["analysis"]["pages_read"] == []


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_is_503_without_an_analyser(w1):
    await w1.put(PROFILE, json={"website": "acme.com"})
    resp = await w1.post(f"{PROFILE}/analyze")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "social.analyzer_unavailable"


@pytest.mark.asyncio
async def test_analyze_is_404_without_a_profile(w1):
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(analysis=_ANALYSIS)))
    assert (await w1.post(f"{PROFILE}/analyze")).status_code == 404


@pytest.mark.asyncio
async def test_analyze_is_422_with_neither_website_nor_description(w1):
    fake = _FakeAnalyzer(AnalysisOutcome(analysis=_ANALYSIS))
    set_production_analyze_fn(fake)
    await w1.put(PROFILE, json={"company_name": "Acme", "description": {"product": "  "}})
    resp = await w1.post(f"{PROFILE}/analyze")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.nothing_to_analyze"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_analyze_success_stores_the_analysis(w1):
    fake = _FakeAnalyzer(AnalysisOutcome(analysis=_ANALYSIS))
    set_production_analyze_fn(fake)
    await w1.put(
        PROFILE,
        json={"company_name": "Acme", "website": "acme.com", "description": {"audience": "Kids"}},
    )

    resp = await w1.post(f"{PROFILE}/analyze")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analysis_status"] == "ready"
    assert body["analysis_error"] is None
    assert body["analyzed_at"] is not None
    assert body["analysis"]["summary"] == "A gentle family dentist."
    assert body["analysis"]["content_pillars"] == ["Kids", "Comfort"]
    assert body["analysis"]["pages_read"] == ["https://acme.com/"]
    assert body["analysis"]["logo_url"] == "https://acme.com/logo.png"
    assert body["analysis"]["competitors"] == []
    assert fake.calls[0].website == "https://acme.com"
    assert fake.calls[0].description["audience"] == "Kids"
    assert fake.calls[0].workspace_id == "w1"
    assert (await w1.get(PROFILE)).json()["analysis"] == body["analysis"]


@pytest.mark.asyncio
async def test_analyze_description_only(w1):
    fake = _FakeAnalyzer(AnalysisOutcome(analysis=SocialAnalysis(summary="From the description")))
    set_production_analyze_fn(fake)
    await w1.put(PROFILE, json={"description": {"problem": "Scared kids"}})

    body = (await w1.post(f"{PROFILE}/analyze")).json()
    assert body["analysis_status"] == "ready"
    assert body["analysis"]["summary"] == "From the description"
    assert fake.calls[0].website is None


@pytest.mark.asyncio
async def test_analyze_failure_is_saved_as_failed_with_200(w1):
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(error="We couldn't reach acme.com.")))
    await w1.put(PROFILE, json={"website": "acme.com", "owner_name": "Sam"})

    resp = await w1.post(f"{PROFILE}/analyze")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analysis_status"] == "failed"
    assert body["analysis_error"] == "We couldn't reach acme.com."
    assert body["analysis"] is None
    assert body["owner_name"] == "Sam"


@pytest.mark.asyncio
async def test_a_raising_analyser_is_a_failed_analysis_and_keeps_the_last_good_one(w1):
    await w1.put(PROFILE, json={"website": "acme.com"})
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(analysis=_ANALYSIS)))
    first = (await w1.post(f"{PROFILE}/analyze")).json()

    set_production_analyze_fn(_FakeAnalyzer(RuntimeError("boom")))
    resp = await w1.post(f"{PROFILE}/analyze")
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_status"] == "failed"
    assert body["analysis_error"]
    assert body["analysis"] == first["analysis"]
    assert body["analyzed_at"] == first["analyzed_at"]


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_is_422_until_every_required_field_is_set(w1):
    await w1.put(PROFILE, json={"owner_name": "Sam", "company_name": "Acme"})
    resp = await w1.post(f"{PROFILE}/complete")
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "social.profile_incomplete"
    for name in ("team_size", "monthly_revenue", "role", "business_model", "category"):
        assert name in error["message"]
    assert "owner_name" not in error["message"]

    body = await _complete(w1)
    assert body["onboarding_completed_at"] is not None
    again = (await w1.post(f"{PROFILE}/complete")).json()
    assert again["onboarding_completed_at"] == body["onboarding_completed_at"]


@pytest.mark.asyncio
async def test_complete_is_404_without_a_profile(w1):
    assert (await w1.post(f"{PROFILE}/complete")).status_code == 404


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_is_503_without_a_writer(w1):
    await _complete(w1)
    resp = await w1.post(f"{IDEAS}/generate", json={})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "social.ideas_unavailable"


@pytest.mark.asyncio
async def test_generate_is_409_before_onboarding_is_complete(w1):
    fake = _FakeIdeas()
    set_production_ideas_fn(fake)
    assert (await w1.post(f"{IDEAS}/generate", json={})).status_code == 409
    await w1.put(PROFILE, json={"owner_name": "Sam"})
    resp = await w1.post(f"{IDEAS}/generate", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "social.onboarding_incomplete"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_generate_stores_new_ideas_and_passes_recent_hooks(w1):
    fake = _FakeIdeas()
    set_production_ideas_fn(fake)
    await _complete(w1)

    resp = await w1.post(f"{IDEAS}/generate", json={})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 6
    first = items[0]
    assert first["status"] == "new"
    assert first["format"] == "hook_demo"
    assert first["script"] == ["Beat one", "Beat two"]
    assert first["hashtags"] == ["#dentist"]
    assert first["why"] == "Because."
    assert first["workspace_id"] == "w1"
    assert fake.calls[0][1] == 6
    assert fake.calls[0][2] == []
    assert fake.calls[0][0].company_name == "Acme Dental"

    resp = await w1.post(f"{IDEAS}/generate", json={"count": 2})
    assert len(resp.json()["items"]) == 2
    assert len(fake.calls[1][2]) == 6
    assert "Hook 1-0" in fake.calls[1][2]

    assert (await w1.post(f"{IDEAS}/generate")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 13, "many"])
async def test_generate_count_is_bounded(w1, count):
    set_production_ideas_fn(_FakeIdeas())
    await _complete(w1)
    assert (await w1.post(f"{IDEAS}/generate", json={"count": count})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [ResearchUnavailable("down"), RuntimeError("boom")])
async def test_a_failed_ideas_run_is_502(w1, result):
    set_production_ideas_fn(_FakeIdeas(result))
    await _complete(w1)
    resp = await w1.post(f"{IDEAS}/generate", json={})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "social.ideas_failed"
    assert (await w1.get(IDEAS)).json()["items"] == []


@pytest.mark.asyncio
async def test_list_and_patch_ideas(w1):
    set_production_ideas_fn(_FakeIdeas())
    await _complete(w1)
    items = (await w1.post(f"{IDEAS}/generate", json={"count": 3})).json()["items"]

    listed = (await w1.get(IDEAS)).json()["items"]
    assert [i["id"] for i in listed] == [i["id"] for i in reversed(items)]

    resp = await w1.patch(f"{IDEAS}/{items[0]['id']}", json={"status": "approved"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    resp = await w1.patch(
        f"{IDEAS}/{items[1]['id']}",
        json={"status": "skipped", "hook": "  Better hook ", "script": ["One", " ", "Two"]},
    )
    edited = resp.json()
    assert edited["status"] == "skipped"
    assert edited["hook"] == "Better hook"
    assert edited["script"] == ["One", "Two"]

    approved = (await w1.get(IDEAS, params={"status": "approved"})).json()["items"]
    skipped = (await w1.get(IDEAS, params={"status": "skipped"})).json()["items"]
    fresh = (await w1.get(IDEAS, params={"status": "new"})).json()["items"]
    assert [i["id"] for i in approved] == [items[0]["id"]]
    assert [i["id"] for i in skipped] == [items[1]["id"]]
    assert [i["id"] for i in fresh] == [items[2]["id"]]
    assert len((await w1.get(IDEAS)).json()["items"]) == 3


@pytest.mark.asyncio
async def test_ideas_list_newest_first(w1):
    set_production_ideas_fn(_FakeIdeas())
    await _complete(w1)
    first = (await w1.post(f"{IDEAS}/generate", json={"count": 1})).json()["items"][0]
    second = (await w1.post(f"{IDEAS}/generate", json={"count": 1})).json()["items"][0]
    listed = [i["id"] for i in (await w1.get(IDEAS)).json()["items"]]
    assert listed == [second["id"], first["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{}, {"status": "deleted"}, {"hook": "   "}, {"format": "meme"}, {"status": None}],
)
async def test_bad_idea_patches_are_422(w1, payload):
    set_production_ideas_fn(_FakeIdeas())
    await _complete(w1)
    idea = (await w1.post(f"{IDEAS}/generate", json={"count": 1})).json()["items"][0]
    assert (await w1.patch(f"{IDEAS}/{idea['id']}", json=payload)).status_code == 422


@pytest.mark.asyncio
async def test_bad_status_filter_is_422(w1):
    assert (await w1.get(IDEAS, params={"status": "deleted"})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("idea_id", ["not-an-id", "507f1f77bcf86cd799439011"])
async def test_malformed_or_missing_idea_is_404(w1, idea_id):
    resp = await w1.patch(f"{IDEAS}/{idea_id}", json={"status": "approved"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_idea.not_found"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profiles_are_per_workspace(w1, w2):
    await w1.put(PROFILE, json={"company_name": "Acme"})
    assert (await w2.get(PROFILE)).status_code == 404

    await w2.put(PROFILE, json={"company_name": "Beta"})
    assert (await w1.get(PROFILE)).json()["company_name"] == "Acme"
    assert (await w2.get(PROFILE)).json()["company_name"] == "Beta"


@pytest.mark.asyncio
async def test_analyze_and_complete_never_cross_workspaces(w1, w2):
    fake = _FakeAnalyzer(AnalysisOutcome(analysis=_ANALYSIS))
    set_production_analyze_fn(fake)
    await _complete(w1)
    await w1.put(PROFILE, json={"website": "acme.com"})
    assert (await w2.post(f"{PROFILE}/analyze")).status_code == 404
    assert (await w2.post(f"{PROFILE}/complete")).status_code == 404
    assert fake.calls == []


@pytest.mark.asyncio
async def test_ideas_are_per_workspace(w1, w2):
    set_production_ideas_fn(_FakeIdeas())
    await _complete(w1)
    mine = (await w1.post(f"{IDEAS}/generate", json={"count": 2})).json()["items"]

    assert (await w2.get(IDEAS)).json()["items"] == []
    resp = await w2.patch(f"{IDEAS}/{mine[0]['id']}", json={"status": "approved"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "social_idea.not_found"
    assert (await w2.post(f"{IDEAS}/generate", json={})).status_code == 409
    statuses = {i["status"] for i in (await w1.get(IDEAS)).json()["items"]}
    assert statuses == {"new"}


@pytest.mark.asyncio
async def test_a_member_can_use_every_route(mongo_db):
    set_production_ideas_fn(_FakeIdeas())
    async with await _client("w1", role="member") as member:
        assert (await member.put(PROFILE, json=_COMPLETE)).status_code == 200
        assert (await member.post(f"{PROFILE}/complete")).status_code == 200
        assert (await member.post(f"{IDEAS}/generate", json={"count": 1})).status_code == 200
        assert (await member.get(IDEAS)).status_code == 200


@pytest.mark.asyncio
async def test_a_workspace_keeps_several_profiles_each_with_its_own_ideas(w1, w2):
    set_production_ideas_fn(_FakeIdeas())
    first = await _complete(w1)
    second = (await w1.post(f"{BASE}/profiles")).json()
    sid = {"profile_id": second["id"]}
    assert (await w1.put(PROFILE, params=sid, json=_COMPLETE)).status_code == 200
    assert (await w1.post(f"{PROFILE}/complete", params=sid)).status_code == 200

    listed = (await w1.get(f"{BASE}/profiles")).json()["items"]
    assert [p["id"] for p in listed] == [first["id"], second["id"]]
    assert (await w2.get(f"{BASE}/profiles")).json()["items"] == []
    assert (await w2.get(PROFILE, params=sid)).status_code == 404

    made = (await w1.post(f"{IDEAS}/generate", params=sid, json={"count": 2})).json()["items"]
    assert len(made) == 2
    assert len((await w1.get(IDEAS, params=sid)).json()["items"]) == 2
    assert (await w1.get(IDEAS, params={"profile_id": first["id"]})).json()["items"] == []
