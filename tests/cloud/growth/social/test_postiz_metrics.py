# tests/cloud/growth/social/test_postiz_metrics.py — Growth › Social post
# stats read through Postiz: the client call (``PostizClient.post_analytics``),
# the label map (``metrics.postiz_counts``) and the non-X step of the
# post-metrics sweep (``service.run_post_metrics``).
#
# mongomock-backed Beanie and a real ``PostizClient`` over an
# ``httpx.MockTransport`` that serves a posts list plus per-post stats. The
# stats payloads follow Postiz v2.25.0's ``AnalyticsData`` shape (read from its
# source; no live call was possible when this was written). No network.
#
# Covers: the request path, date param and auth header; the last point per
# series, string and int totals; ``[]`` and ``{"missing": true}`` read as empty;
# errors never carry the API key; the label map (exact, case-insensitive,
# unknown labels dropped); an empty payload stores no row; X and personal
# LinkedIn posts get no Postiz call; a second run the same day re-reads nothing;
# the per-run call cap; one failing post never stops the rest; old rows read as
# X and stay the only rows the X readers see; the superseded index is dropped
# so an idea's X row and LinkedIn Page row share a bucket.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core.crypto import encrypt
from pocketpaw_ee.cloud.growth.social import metrics as social_metrics
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.postiz import (
    PostizClient,
    PostizError,
    set_production_postiz,
)
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc
from pocketpaw_ee.cloud.models.social_post_metric import SocialPostMetric as MetricDoc
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg as OrgDoc

API_KEY = "org-api-key-abc123"

# Postiz v2.25.0 answers for one LinkedIn Page post and one Instagram post.
LINKEDIN_PAGE_STATS = json.loads(
    """[
  {"label": "Impressions", "data": [{"total": 1520, "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Unique Impressions", "data": [{"total": 980, "date": "2026-11-20"}],
   "percentageChange": 0},
  {"label": "Clicks", "data": [{"total": 44, "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Likes", "data": [{"total": 31, "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Comments", "data": [{"total": 6, "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Shares", "data": [{"total": 3, "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Engagement", "data": [{"total": 0.05, "date": "2026-11-20"}], "percentageChange": 0}
]"""
)
INSTAGRAM_STATS = json.loads(
    """[
  {"label": "Views", "data": [{"total": "200", "date": "2026-11-19"},
                              {"total": "410", "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Reach", "data": [{"total": "300", "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Likes", "data": [{"total": "12", "date": "2026-11-20"}], "percentageChange": 0},
  {"label": "Saves", "data": [{"total": "n/a", "date": "2026-11-20"}], "percentageChange": 0}
]"""
)


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    social_metrics.set_production_metrics_fn(None)
    set_production_postiz(None)
    try:
        yield
    finally:
        social_metrics.set_production_metrics_fn(None)
        set_production_postiz(None)


class _Postiz:
    """A mocked Postiz: ``posts`` for the list route, ``stats[id]`` for the stats route
    (a payload, or an int status to fail with)."""

    def __init__(self, posts: list[dict[str, Any]], stats: dict[str, Any]) -> None:
        self.posts = posts
        self.stats = stats
        self.requests: list[httpx.Request] = []

    def client(self) -> PostizClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        return PostizClient("https://postiz.test", "jwt-secret", client=http)

    def stat_calls(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests if "/analytics/" in r.url.path]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/public/v1/posts":
            return httpx.Response(200, json={"posts": self.posts})
        answer = self.stats.get(request.url.path.rsplit("/", 1)[-1], [])
        if isinstance(answer, int):
            return httpx.Response(answer, text=f"boom for {request.headers['authorization']}")
        return httpx.Response(200, json=answer)


def _post(pid: str, provider: str, age: timedelta, now: datetime, state: str = "PUBLISHED"):
    return {
        "id": pid,
        "state": state,
        "publishDate": (now - age).isoformat().replace("+00:00", "Z"),
        "releaseURL": f"https://example.com/{pid}",
        "integration": {"id": f"acct-{provider}", "providerIdentifier": provider},
    }


async def _org(workspace: str = "w1", profile: str = "p1") -> None:
    await OrgDoc(
        workspace=workspace, profile=profile, postiz_org_id="o1", api_key_enc=encrypt(API_KEY)
    ).insert()


async def _idea(post_ids: list[str], platform: str = "reddit", **kw: Any) -> IdeaDoc:
    doc = IdeaDoc(
        workspace=kw.pop("workspace", "w1"),
        profile=kw.pop("profile", "p1"),
        format="reddit_post",
        hook="Ship it",
        platform=platform,
        post_ids=post_ids,
        **kw,
    )
    await doc.insert()
    return doc


def _now() -> datetime:
    return datetime.now(UTC)


DAY = timedelta(days=1, hours=1)


# --- the client call -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_analytics_reads_the_last_point_of_each_series():
    fake = _Postiz([], {"p1": INSTAGRAM_STATS})
    out = await fake.client().post_analytics(API_KEY, "p1", 7)
    # The last Views point; a non-numeric total is dropped.
    assert out == {"Views": 410, "Reach": 300, "Likes": 12}
    (req,) = fake.requests
    assert req.method == "GET"
    assert req.url.path == "/api/public/v1/analytics/post/p1"
    assert req.url.params["date"] == "7"
    assert req.headers["authorization"] == API_KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [[], {"missing": True}])
async def test_post_analytics_reads_no_stats_as_empty(answer: Any):
    assert await _Postiz([], {"p1": answer}).client().post_analytics(API_KEY, "p1", 1) == {}


@pytest.mark.asyncio
async def test_post_analytics_rejects_an_unexpected_shape():
    with pytest.raises(PostizError) as err:
        await _Postiz([], {"p1": {"label": "x"}}).client().post_analytics(API_KEY, "p1", 1)
    assert err.value.code == "postiz.bad_response"


@pytest.mark.asyncio
async def test_post_analytics_errors_never_carry_the_api_key():
    with pytest.raises(PostizError) as err:
        await _Postiz([], {"p1": 500}).client().post_analytics(API_KEY, "p1", 1)
    assert err.value.code == "postiz.http_error"
    assert API_KEY not in err.value.message and "***" in err.value.message


# --- the label map -------------------------------------------------------------------------


def test_label_map_turns_linkedin_page_labels_into_our_columns():
    totals = {s["label"]: int(s["data"][-1]["total"]) for s in LINKEDIN_PAGE_STATS}
    # Unique Impressions, Clicks and Engagement are not ours: dropped, not misfiled.
    assert social_metrics.postiz_counts(totals) == {
        "impressions": 1520,
        "likes": 31,
        "replies": 6,
        "reposts": 3,
    }


def test_label_map_is_case_insensitive_and_drops_unknown_labels():
    assert social_metrics.postiz_counts({"LIKES": 4, " views ": 9, "Pin Clicks": 2}) == {
        "likes": 4,
        "impressions": 9,
    }
    assert social_metrics.postiz_counts({"Reach": 300, "Outbound Clicks": 1}) == {}


# --- the sweep -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_snapshots_reporting_posts_once_and_skips_x_and_personal_linkedin(
    mongo_db: Any,
):
    now = _now()
    await _org()
    # An X idea cross-posted to a LinkedIn Page, and a reddit idea on Instagram.
    x_idea = await _idea(["p-x", "p-lp"], platform="x")
    ig_idea = await _idea(["p-ig", "p-li", "p-q"])
    fake = _Postiz(
        [
            _post("p-x", "x", DAY, now),
            _post("p-lp", "linkedin-page", DAY, now),
            _post("p-ig", "instagram-standalone", timedelta(days=3, hours=1), now),
            _post("p-li", "linkedin", DAY, now),  # personal LinkedIn: Postiz has no stats
            _post("p-q", "instagram-standalone", DAY, now, state="QUEUE"),
        ],
        {"p-lp": LINKEDIN_PAGE_STATS, "p-ig": INSTAGRAM_STATS},
    )
    set_production_postiz(fake.client())

    counts = await social_service.run_post_metrics(now)
    assert sorted(fake.stat_calls()) == ["p-ig", "p-lp"]
    assert (counts["postiz_due"], counts["postiz_stored"], counts["failed"]) == (2, 2, 0)
    rows = {r.postiz_post_id: r for r in await MetricDoc.find({}).to_list()}
    assert set(rows) == {"p-lp", "p-ig"}
    lp = rows["p-lp"]
    assert (lp.workspace, lp.profile, lp.idea, lp.platform, lp.age_bucket) == (
        "w1",
        "p1",
        str(x_idea.id),
        "linkedin-page",
        1,
    )
    assert (lp.impressions, lp.likes, lp.replies, lp.reposts, lp.x_post_id) == (1520, 31, 6, 3, "")
    ig = rows["p-ig"]
    assert (ig.idea, ig.platform, ig.age_bucket) == (str(ig_idea.id), "instagram-standalone", 3)
    assert (ig.impressions, ig.likes, ig.bookmarks) == (410, 12, 0)

    # Same day again: nothing due, no stats call.
    fake.requests.clear()
    again = await social_service.run_post_metrics(now + timedelta(hours=1))
    assert fake.stat_calls() == [] and again["postiz_stored"] == 0
    assert await MetricDoc.find({}).count() == 2


@pytest.mark.asyncio
async def test_an_empty_stats_payload_stores_no_row(mongo_db: Any):
    now = _now()
    await _org()
    await _idea(["p-1", "p-2"])
    fake = _Postiz(
        [_post("p-1", "linkedin-page", DAY, now), _post("p-2", "threads", DAY, now)],
        {"p-1": [], "p-2": [{"label": "Reach", "data": [{"total": "5", "date": "d"}]}]},
    )
    set_production_postiz(fake.client())
    counts = await social_service.run_post_metrics(now)
    assert sorted(fake.stat_calls()) == ["p-1", "p-2"]
    assert (counts["postiz_due"], counts["postiz_stored"]) == (2, 0)
    assert await MetricDoc.find({}).count() == 0


@pytest.mark.asyncio
async def test_stats_calls_are_capped_per_run(mongo_db: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(social_service, "POSTIZ_STATS_MAX_CALLS", 2)
    now = _now()
    await _org()
    ids = ["p-1", "p-2", "p-3"]
    await _idea(ids)
    fake = _Postiz(
        [_post(i, "linkedin-page", DAY, now) for i in ids],
        {i: LINKEDIN_PAGE_STATS for i in ids},
    )
    set_production_postiz(fake.client())

    counts = await social_service.run_post_metrics(now)
    assert len(fake.stat_calls()) == 2
    assert (counts["postiz_due"], counts["postiz_stored"]) == (3, 2)
    # The next run picks up the one left over.
    fake.requests.clear()
    await social_service.run_post_metrics(now + timedelta(hours=1))
    assert len(fake.stat_calls()) == 1
    assert await MetricDoc.find({}).count() == 3


@pytest.mark.asyncio
async def test_one_failing_post_does_not_stop_the_rest(mongo_db: Any):
    now = _now()
    await _org()
    await _idea(["p-bad", "p-ok"])
    fake = _Postiz(
        [_post("p-bad", "facebook", DAY, now), _post("p-ok", "facebook", DAY, now)],
        {"p-bad": 502, "p-ok": [{"label": "Reactions", "data": [{"total": "8", "date": "d"}]}]},
    )
    set_production_postiz(fake.client())
    counts = await social_service.run_post_metrics(now)
    assert counts["failed"] == 0 and counts["postiz_stored"] == 1
    (row,) = await MetricDoc.find({}).to_list()
    assert (row.postiz_post_id, row.likes) == ("p-ok", 8)


@pytest.mark.asyncio
async def test_without_postiz_there_is_no_stats_step(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(social_service, "resolve_postiz", lambda: None)
    await _idea(["p-1"])
    counts = await social_service.run_post_metrics(_now())
    assert counts["postiz_due"] == 0 and await MetricDoc.find({}).count() == 0


# --- old rows and the X readers ------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_rows_read_as_x_and_x_readers_skip_postiz_rows(mongo_db: Any):
    now = _now()
    # A row written before ``platform`` / ``postiz_post_id`` existed.
    await MetricDoc.get_pymongo_collection().insert_one(
        {
            "workspace": "w1",
            "profile": "p1",
            "idea": "idea-1",
            "x_post_id": "123",
            "age_bucket": 1,
            "likes": 5,
            "captured_at": now - timedelta(days=1),
        }
    )
    (old,) = await MetricDoc.find({}).to_list()
    assert (old.platform, old.postiz_post_id, old.x_post_id) == ("x", None, "123")

    # A newer LinkedIn Page row for the same idea stays out of the X readers.
    await MetricDoc(
        workspace="w1",
        idea="idea-1",
        platform="linkedin-page",
        postiz_post_id="p-lp",
        age_bucket=3,
        likes=999,
        captured_at=now,
    ).insert()
    out = await social_service.latest_metrics("w1", ["idea-1"])
    assert (out["idea-1"].x_post_id, out["idea-1"].likes) == ("123", 5)


@pytest.mark.asyncio
async def test_sweep_drops_the_superseded_index_so_x_and_postiz_rows_share_a_bucket(
    mongo_db: Any,
):
    coll = MetricDoc.get_pymongo_collection()
    legacy = "workspace_1_idea_1_age_bucket_1"
    await coll.create_index([("workspace", 1), ("idea", 1), ("age_bucket", 1)], unique=True)
    assert legacy in await coll.index_information()

    now = _now()
    await _org()
    idea = await _idea(["p-lp"], platform="x")
    await MetricDoc(
        workspace="w1", idea=str(idea.id), x_post_id="123", age_bucket=1, captured_at=now
    ).insert()
    fake = _Postiz([_post("p-lp", "linkedin-page", DAY, now)], {"p-lp": LINKEDIN_PAGE_STATS})
    set_production_postiz(fake.client())

    counts = await social_service.run_post_metrics(now)
    assert legacy not in await coll.index_information()
    assert counts["postiz_stored"] == 1
    rows = await MetricDoc.find({"idea": str(idea.id)}).to_list()
    assert sorted(r.platform for r in rows) == ["linkedin-page", "x"]
    assert {r.age_bucket for r in rows} == {1}


@pytest.mark.asyncio
async def test_a_postiz_snapshot_bucket_is_unique(mongo_db: Any):
    from pymongo.errors import DuplicateKeyError

    row = {
        "workspace": "w1",
        "idea": "i",
        "platform": "threads",
        "postiz_post_id": "p-1",
        "age_bucket": 1,
        "captured_at": _now(),
    }
    await MetricDoc(**row).insert()
    with pytest.raises(DuplicateKeyError):
        await MetricDoc(**row).insert()
    # A second post of the same idea on the same platform gets its own row.
    await MetricDoc(**{**row, "postiz_post_id": "p-2"}).insert()


@pytest.mark.asyncio
async def test_a_cross_posted_x_idea_gets_both_its_x_and_its_postiz_snapshot(mongo_db: Any):
    """The Postiz step runs first; its LinkedIn Page row must not count as the X row's
    bucket, and both steps share one posts-list read."""
    now = _now()
    await _org()
    await _idea(["p-x", "p-lp"], platform="x")
    x_post = {**_post("p-x", "x", DAY, now), "releaseURL": "https://x.com/a/status/123"}
    fake = _Postiz(
        [x_post, _post("p-lp", "linkedin-page", DAY, now)], {"p-lp": LINKEDIN_PAGE_STATS}
    )
    set_production_postiz(fake.client())

    async def _x_metrics(workspace_id: str, ids: list[str]) -> dict[str, dict[str, int]]:
        return {i: {"impressions": 50, "likes": 2} for i in ids}

    social_metrics.set_production_metrics_fn(_x_metrics)

    counts = await social_service.run_post_metrics(now)
    assert (counts["filled"], counts["stored"], counts["postiz_stored"]) == (1, 1, 1)
    rows = await MetricDoc.find({}).to_list()
    assert sorted((r.platform, r.age_bucket) for r in rows) == [("linkedin-page", 1), ("x", 1)]
    assert sum(r.url.path == "/api/public/v1/posts" for r in fake.requests) == 1
