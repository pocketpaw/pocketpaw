# tests/cloud/growth/social/test_blueprint_live.py — the live values of the
# approved Blueprint (``GET /api/v1/growth/social/blueprint/live``, ``live.get``)
# and the owner-only ``metric_key`` on objectives and play KPIs. Same harness as
# test_blueprint_units.py: the social router over mongomock-backed Beanie with
# seeded ideas, snapshots, tracked links and repo rows. No network, no model.
#
# Covers: each key's value from a fixture, null with no data and null with no
# key (missing is never 0; a measured 0 is a number); objectives scoped by
# channel; a play counting only its platform's ideas; two active acting plays on
# one platform make the play's post values null (stars stay brand-wide); other
# workspaces' and brands' rows never count; no approved Blueprint is empty maps;
# ``metric_key`` round-trips through draft, approve and the units read, and an
# unknown key is 422; a regenerate whose analyst output carries a metric_key
# keeps the owner's value and never writes a play; a LinkedIn play counting
# LinkedIn Page rows from the Postiz stats fixture, and an X idea cross-posted
# to a Page counting once on each platform; a count Postiz did not report is
# skipped, so an impressions-only or likes-only post leaves the other value null.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social import live
from pocketpaw_ee.cloud.growth.social import metrics as social_metrics
from pocketpaw_ee.cloud.growth.social.analyst import set_production_analyze_fn
from pocketpaw_ee.cloud.growth.social.domain import AnalysisOutcome, SocialAnalysis
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit as UnitDoc
from pocketpaw_ee.cloud.models.repo_metric import RepoMetric as RepoDoc
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc
from pocketpaw_ee.cloud.models.social_link import SocialLink as LinkDoc
from pocketpaw_ee.cloud.models.social_post_metric import SocialPostMetric as MetricDoc

from tests.cloud.growth.social.test_blueprint import BP, _profile
from tests.cloud.growth.social.test_blueprint_units import UNITS, _ok, _put
from tests.cloud.growth.social.test_postiz_metrics import LINKEDIN_PAGE_STATS
from tests.cloud.growth.social.test_router import _client, _FakeAnalyzer

LIVE = f"{BP}/live"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
REPO = "acme/app"
NULL = {"value": None, "as_of": None, "posts_counted": 0}

_OBJECTIVES = [
    {"id": "imp", "metric": "X reach", "target": 5000, "channel": "x",
     "metric_key": "impressions_per_week"},
    {"id": "imp_all", "metric": "Reach", "target": 5000, "metric_key": "impressions_per_week"},
    {"id": "eng", "metric": "Engagement", "target": 10, "metric_key": "engagement_per_post"},
    {"id": "clk", "metric": "Clicks", "target": 50, "metric_key": "link_clicks_per_week"},
    {"id": "star", "metric": "Stars", "target": 500, "metric_key": "github_stars"},
    {"id": "free", "metric": "Signups", "target": 40},
]  # fmt: skip


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_seams():
    set_production_analyze_fn(None)
    try:
        yield
    finally:
        set_production_analyze_fn(None)


def _ctx(workspace: str = "w1") -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=NOW,
    )


async def _blueprint(client, pid: str, plays: dict[str, dict] | None = None) -> None:
    """An approved brand + strategy (``_OBJECTIVES``) and the given approved plays."""
    await _put(client, pid, "brand", {"who_we_are": {"summary": "Acme"}}, 0)
    await _put(client, pid, "strategy", {"goals": {"objectives": _OBJECTIVES}}, 0)
    await _ok(client, pid, "brand")
    await _ok(client, pid, "strategy")
    for unit, body in (plays or {}).items():
        await _put(client, pid, unit, body, 0)
        await _ok(client, pid, unit)


def _play(status: str = "core", key: str | None = "impressions_per_week") -> dict:
    return {"status": status, "posts_per_week": 3, "kpi": {"metric": "Reach", "metric_key": key}}


async def _idea(pid: str, platform: str, days_ago: float, workspace: str = "w1") -> str:
    doc = IdeaDoc(
        workspace=workspace,
        profile=pid,
        format="x_post",
        hook="Ship it",
        platform=platform,
        published_at=NOW - timedelta(days=days_ago),
    )
    await doc.insert()
    return str(doc.id)


async def _snap(idea: str, bucket: int, workspace: str = "w1", **counts: int) -> None:
    await MetricDoc(
        workspace=workspace,
        idea=idea,
        x_post_id="1",
        age_bucket=bucket,
        captured_at=NOW - timedelta(hours=10 - bucket),
        **counts,
    ).insert()


async def _link(idea: str, by_day: dict[int, int], code: str, workspace: str = "w1") -> None:
    days = {(TODAY - timedelta(days=d)).isoformat(): n for d, n in by_day.items()}
    await LinkDoc(
        code=code,
        workspace=workspace,
        idea=idea,
        target_url="https://a.test",
        clicks=sum(days.values()),
        clicks_by_day=days,
    ).insert()


async def _repo(day: int, stars: int, workspace: str = "w1") -> None:
    await RepoDoc(
        workspace=workspace,
        repo=REPO,
        day=(TODAY - timedelta(days=day)).isoformat(),
        stars=stars,
        captured_at=NOW - timedelta(days=day - 1),
    ).insert()


async def _live(pid: str, workspace: str = "w1") -> dict:
    return (await live.get(_ctx(workspace), pid, now=NOW)).model_dump(mode="json")


async def _seed_numbers(pid: str) -> None:
    """X: a 2-day-old post (latest bucket 3 wins), a 10- and a 9-day-old post with
    7-day snapshots (outside the week), one with no snapshot yet. Reddit: one post.
    Rows of another brand and another workspace (even naming this brand's idea) that
    must never count."""
    a = await _idea(pid, "x", 2)
    await _snap(a, 1, impressions=100, likes=50)
    await _snap(a, 3, impressions=300, likes=9, replies=9, reposts=9)
    b = await _idea(pid, "x", 10)
    await _snap(b, 7, impressions=999, likes=4, replies=2, reposts=1, quotes=50, bookmarks=50)
    e = await _idea(pid, "x", 9)
    await _snap(e, 7, impressions=10, likes=1)
    await _idea(pid, "x", 1)  # published, not measured yet
    c = await _idea(pid, "reddit", 1)
    await _snap(c, 1, impressions=50)
    await _link(a, {0: 2, 6: 1, 7: 5}, "codeaaa1")  # day 7 is outside the week
    await _link(c, {0: 4}, "codeccc1")
    await _repo(2, 10)
    await _repo(1, 12)

    other = await _profile()
    o = await _idea(other, "x", 1)
    await _snap(o, 7, impressions=10_000, likes=100)
    await _link(o, {0: 100}, "codeooo1")
    f = await _idea(pid, "x", 1, workspace="w2")  # names this brand, other workspace
    await _snap(f, 7, workspace="w2", impressions=10_000, likes=100)
    await _link(f, {0: 100}, "codefff1", workspace="w2")
    await _repo(0, 9_999, workspace="w2")
    # Another workspace's rows naming this brand's idea.
    await _snap(a, 28, workspace="w2", impressions=10_000, likes=100)
    await _link(a, {0: 100}, "codeaaa2", workspace="w2")


# --- values per key ------------------------------------------------------------------------


async def test_each_key_measures_its_fixture(w1):
    pid = await _profile(github_repos=[REPO])
    await _blueprint(w1, pid, {"play:x-organic": _play(), "play:reddit-community": _play()})
    await _seed_numbers(pid)
    out = await _live(pid)
    obj = out["objectives"]

    # Last 7 days, each post at its largest bucket: 300 on X, plus 50 on Reddit.
    assert obj["imp"] == {"value": 300.0, "as_of": "2026-11-21T05:00:00Z", "posts_counted": 1}
    assert obj["imp_all"]["value"] == 350.0 and obj["imp_all"]["posts_counted"] == 2
    # Only posts with a 7-day snapshot: (4+2+1 + 1) / 2; quotes and bookmarks left out.
    assert obj["eng"] == {"value": 4.0, "as_of": "2026-11-21T09:00:00Z", "posts_counted": 2}
    # Clicks in the last 7 UTC days on this brand's linked posts: 2 + 1 + 4.
    assert obj["clk"] == {"value": 7.0, "as_of": "2026-11-21T12:00:00Z", "posts_counted": 2}
    # The newest stored day's stars.
    assert obj["star"] == {"value": 12.0, "as_of": "2026-11-21T12:00:00Z", "posts_counted": 0}
    assert obj["free"] == NULL  # no metric_key, even with data

    # A play counts only its platform's ideas.
    assert out["plays"]["play:x-organic"]["value"] == 300.0
    assert out["plays"]["play:reddit-community"]["value"] == 50.0


async def test_no_data_is_null_never_zero(w1):
    pid = await _profile(github_repos=[REPO])
    await _blueprint(w1, pid, {"play:x-organic": _play()})
    await _idea(pid, "x", 30)  # outside the week, no snapshot, no link
    out = await _live(pid)
    assert out["objectives"] == {o["id"]: NULL for o in _OBJECTIVES}
    assert out["plays"] == {"play:x-organic": NULL}


async def test_a_measured_zero_is_a_number(w1):
    pid = await _profile()
    await _blueprint(w1, pid)
    idea = await _idea(pid, "x", 8)
    await _snap(idea, 7, impressions=0)
    await _link(idea, {}, "codezzz1")
    recent = await _idea(pid, "x", 1)
    await _snap(recent, 1, impressions=0)
    obj = (await _live(pid))["objectives"]
    assert obj["imp"]["value"] == 0.0 and obj["imp"]["posts_counted"] == 1
    assert obj["eng"]["value"] == 0.0
    assert obj["clk"]["value"] == 0.0 and obj["clk"]["posts_counted"] == 1


async def test_no_kpi_key_is_null(w1):
    pid = await _profile(github_repos=[REPO])
    await _blueprint(w1, pid, {"play:x-organic": _play(key=None)})
    await _seed_numbers(pid)
    assert (await _live(pid))["plays"] == {"play:x-organic": NULL}


# --- scoping -------------------------------------------------------------------------------


async def test_an_objective_channel_without_ideas_is_null(w1):
    pid = await _profile()
    objectives = [{"id": "mail", "metric": "Reach", "target": 1, "channel": "email",
                   "metric_key": "impressions_per_week"}]  # fmt: skip
    await _put(w1, pid, "brand", {"who_we_are": {"summary": "Acme"}}, 0)
    await _put(w1, pid, "strategy", {"goals": {"objectives": objectives}}, 0)
    await _ok(w1, pid, "brand")
    await _ok(w1, pid, "strategy")
    await _seed_numbers(pid)
    assert (await _live(pid))["objectives"] == {"mail": NULL}


async def test_two_acting_plays_on_one_platform_leave_post_values_null(w1):
    pid = await _profile(github_repos=[REPO])
    await _blueprint(
        w1,
        pid,
        {
            "play:x-organic": _play(),
            "play:x-authority": _play(status="testing", key="link_clicks_per_week"),
            "play:reddit-community": _play(),
        },
    )
    await _seed_numbers(pid)
    out = await _live(pid)
    assert out["plays"]["play:x-organic"] == NULL
    assert out["plays"]["play:x-authority"] == NULL
    assert out["plays"]["play:reddit-community"]["value"] == 50.0  # its own platform
    assert out["objectives"]["imp"]["value"] == 300.0  # platform level still counts

    # Stars are not post-based: a two-play platform still gets them.
    await _put(w1, pid, "play:x-authority", _play(key="github_stars"), 1)
    await _ok(w1, pid, "play:x-authority")
    assert (await _live(pid))["plays"]["play:x-authority"]["value"] == 12.0

    # Parking one leaves the other the platform's single acting play.
    await _put(w1, pid, "play:x-authority", _play(status="parked"), 2)
    await _ok(w1, pid, "play:x-authority")
    out = await _live(pid)
    assert out["plays"]["play:x-organic"]["value"] == 300.0
    assert out["plays"]["play:x-authority"] == NULL


async def test_no_approved_blueprint_is_empty_maps(w1):
    pid = await _profile()
    await _seed_numbers(pid)
    await _put(w1, pid, "brand", {"who_we_are": {"summary": "Acme"}}, 0)
    await _ok(w1, pid, "brand")  # no strategy approved
    resp = await w1.get(LIVE, params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"objectives": {}, "plays": {}}


async def test_the_route_reads_the_approved_blueprint(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _play()})
    idea = IdeaDoc(
        workspace="w1",
        profile=pid,
        format="x_post",
        hook="h",
        platform="x",
        published_at=datetime.now(UTC) - timedelta(days=1),
    )
    await idea.insert()
    await MetricDoc(
        workspace="w1",
        idea=str(idea.id),
        x_post_id="1",
        age_bucket=1,
        captured_at=datetime.now(UTC),
        impressions=3100,
    ).insert()
    resp = await w1.get(LIVE, params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    assert resp.json()["plays"]["play:x-organic"]["value"] == 3100.0
    async with await _client("w2") as w2:
        assert (await w2.get(LIVE, params={"profile_id": pid})).status_code == 404


# --- metric_key on the bodies --------------------------------------------------------------


async def test_metric_key_round_trips_and_unknown_keys_are_422(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _play(key="engagement_per_post")})
    units = {
        u["unit"]: u for u in (await w1.get(UNITS, params={"profile_id": pid})).json()["units"]
    }
    objectives = units["strategy"]["approved"]["body"]["goals"]["objectives"]
    assert {o["id"]: o["metric_key"] for o in objectives} == {
        o["id"]: o.get("metric_key") for o in _OBJECTIVES
    }
    assert units["play:x-organic"]["approved"]["body"]["kpi"] == {
        "metric": "Reach",
        "baseline": None,
        "target": None,
        "metric_key": "engagement_per_post",
    }

    bad = {"goals": {"objectives": [{"metric": "m", "target": 1, "metric_key": "followers"}]}}
    body = await _put(w1, pid, "strategy", bad, 1, status=422)
    assert body["error"]["code"] == "social.blueprint_invalid_body"


async def test_regenerate_keeps_the_owner_metric_key(w1, monkeypatch):
    """An analyst output that changes or introduces a metric_key is refused: the
    owner's value is carried verbatim, and regenerate never writes a play."""
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _play()})
    play_before = [d.model_dump() for d in await UnitDoc.find({"unit": "play:x-organic"}).to_list()]

    real = social_blueprint._regenerated

    def ai_writes_keys(*args: Any, **kwargs: Any) -> dict:
        out = real(*args, **kwargs)
        for goal in out["goals"]:
            goal["metric_key"] = "github_stars"
        return out

    monkeypatch.setattr(social_blueprint, "_regenerated", ai_writes_keys)
    set_production_analyze_fn(
        _FakeAnalyzer(AnalysisOutcome(analysis=SocialAnalysis(summary="New summary")))
    )
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 200, resp.text

    draft = await UnitDoc.find_one({"unit": "strategy", "status": "draft"})
    assert draft is not None and draft.version == 2
    keys = {o["id"]: o["metric_key"] for o in draft.body["goals"]["objectives"]}
    assert keys["imp"] == "impressions_per_week"  # changed by the AI: kept
    assert keys["free"] is None  # introduced by the AI: refused
    assert keys == {o["id"]: o.get("metric_key") for o in _OBJECTIVES}
    play_after = [d.model_dump() for d in await UnitDoc.find({"unit": "play:x-organic"}).to_list()]
    assert play_after == play_before


# --- per-platform rows (Postiz) ------------------------------------------------------------


async def _page_snap(idea: str, pid: str, post: str, bucket: int, days_ago: float) -> None:
    """A LinkedIn Page snapshot as the Postiz step writes it, counts mapped from the
    recorded Postiz stats fixture."""
    counts = social_metrics.postiz_counts(
        {s["label"]: int(s["data"][-1]["total"]) for s in LINKEDIN_PAGE_STATS}
    )
    await MetricDoc(
        workspace="w1",
        profile=pid,
        idea=idea,
        platform="linkedin-page",
        postiz_post_id=post,
        published_at=NOW - timedelta(days=days_ago),
        age_bucket=bucket,
        captured_at=NOW - timedelta(hours=10 - bucket),
        **counts,
    ).insert()


async def test_a_linkedin_play_counts_linkedin_page_rows(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:linkedin-content": _play()})
    idea = await _idea(pid, "reddit", 2)
    await _page_snap(idea, pid, "p-lp", 1, 2)
    await _page_snap(idea, pid, "p-old", 7, 12)  # outside the week
    out = await _live(pid)
    assert out["plays"]["play:linkedin-content"] == {
        "value": 1520.0,
        "as_of": "2026-11-21T03:00:00Z",
        "posts_counted": 1,
    }


async def test_a_cross_posted_x_idea_counts_once_per_platform(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _play(), "play:linkedin-content": _play()})
    idea = await _idea(pid, "x", 2)
    await _snap(idea, 1, impressions=300)
    # Posted to the Page earlier, so its row is the idea's largest bucket.
    await _page_snap(idea, pid, "p-lp", 3, 4)
    out = await _live(pid)
    assert out["plays"]["play:x-organic"]["value"] == 300.0  # X rows only, no double count
    assert out["plays"]["play:linkedin-content"]["value"] == 1520.0
    assert out["objectives"]["imp"]["value"] == 300.0  # the X objective
    assert out["objectives"]["imp_all"] == {
        "value": 1820.0,
        "as_of": "2026-11-21T05:00:00Z",
        "posts_counted": 2,
    }
    # Another brand's LinkedIn Page row never counts.
    await _page_snap(idea, await _profile(), "p-other", 3, 2)
    assert (await _live(pid))["plays"]["play:linkedin-content"]["value"] == 1520.0


async def _page_row(idea: str, pid: str, post: str, bucket: int, days_ago: float, **counts):
    """A Postiz LinkedIn Page row with only ``counts`` reported; the rest None."""
    none = dict.fromkeys(("impressions", "likes", "replies", "reposts", "quotes", "bookmarks"))
    await MetricDoc(
        workspace="w1",
        profile=pid,
        idea=idea,
        platform="linkedin-page",
        postiz_post_id=post,
        published_at=NOW - timedelta(days=days_ago),
        age_bucket=bucket,
        captured_at=NOW - timedelta(hours=3),
        **(none | counts),
    ).insert()


async def test_impressions_without_engagement_leave_engagement_null(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:linkedin-content": _play(key="engagement_per_post")})
    idea = await _idea(pid, "reddit", 8)
    await _page_row(idea, pid, "p-imp", 7, 8, impressions=900)
    assert (await _live(pid))["plays"]["play:linkedin-content"] == NULL  # unknown, not 0.0
    # A post that reports likes only counts with what it reported.
    await _page_row(idea, pid, "p-likes", 7, 8, likes=8)
    play = (await _live(pid))["plays"]["play:linkedin-content"]
    assert (play["value"], play["posts_counted"]) == (8.0, 1)


async def test_likes_without_impressions_leave_reach_null(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:linkedin-content": _play()})
    idea = await _idea(pid, "reddit", 2)
    await _page_row(idea, pid, "p-likes", 1, 2, likes=8)
    assert (await _live(pid))["plays"]["play:linkedin-content"] == NULL  # unknown, not 0.0
    await _page_row(idea, pid, "p-imp", 1, 2, impressions=40)
    play = (await _live(pid))["plays"]["play:linkedin-content"]
    assert (play["value"], play["posts_counted"]) == (40.0, 1)
