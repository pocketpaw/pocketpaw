# tests/cloud/growth/social/test_metrics_summary.py: the Today strip's read,
# ``service.metrics_summary`` and ``GET /growth/social/metrics/summary``, over
# mongomock-backed Beanie with seeded ideas, metric snapshots, tracked links and
# repo metric rows. No network, nothing written by the code under test.
#
# Covers: the wire shape (exact key sets, the frontend codes against them); the
# yesterday-UTC window edges (00:00:00 in, today's 00:00:00 out); the latest
# snapshot winning; clicks summed per idea; the repo 14-day series, the 7-day
# star delta and uniques; zeros and empty lists when data is missing; another
# workspace's rows (even ones naming this profile or idea) never showing; one
# profile never seeing another's posts or repos.

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.router import router as social_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.repo_metric import RepoMetric as RepoDoc
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc
from pocketpaw_ee.cloud.models.social_link import SocialLink as LinkDoc
from pocketpaw_ee.cloud.models.social_post_metric import SocialPostMetric as MetricDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as ProfileDoc

NOW = datetime(2026, 11, 14, 6, 30, tzinfo=UTC)
TODAY = datetime(2026, 11, 14, tzinfo=UTC)
YESTERDAY = TODAY - timedelta(days=1)
REPO = "acme/app"


def _ctx(workspace: str = "w1") -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=NOW,
    )


async def _profile(workspace: str = "w1", repos: list[str] | None = None) -> ProfileDoc:
    doc = ProfileDoc(workspace=workspace, github_repos=repos or [])
    await doc.insert()
    return doc


async def _idea(
    workspace: str, profile: str, at: datetime | None, hook: str = "Ship it", **kw: Any
) -> IdeaDoc:
    doc = IdeaDoc(
        workspace=workspace,
        profile=profile,
        format="x_post",
        hook=hook,
        platform=kw.pop("platform", "x"),
        published_at=at,
        published_url=f"https://x.com/a/status/{hook.replace(' ', '')}" if at else "",
        x_post_id="1" if at else "",
        **kw,
    )
    await doc.insert()
    return doc


async def _snapshot(workspace: str, idea: str, bucket: int, at: datetime, **counts: int) -> None:
    await MetricDoc(
        workspace=workspace, idea=idea, x_post_id="1", age_bucket=bucket, captured_at=at, **counts
    ).insert()


async def _link(workspace: str, idea: str, clicks: int, code: str) -> None:
    await LinkDoc(
        code=code, workspace=workspace, idea=idea, target_url="https://a.test", clicks=clicks
    ).insert()


async def _repo_day(
    workspace: str, day: date, stars: int, uniques: int = 0, traffic: bool = True
) -> None:
    await RepoDoc(
        workspace=workspace,
        repo=REPO,
        day=day.isoformat(),
        stars=stars,
        uniques=uniques,
        traffic_available=traffic,
        captured_at=NOW,
    ).insert()


async def _summary(workspace: str = "w1", profile_id: str | None = None, now: datetime = NOW):
    return await social_service.metrics_summary(_ctx(workspace), profile_id, now=now)


# --- posts --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posts_carry_the_latest_snapshot_and_summed_clicks(mongo_db: Any):
    p = await _profile()
    idea = await _idea("w1", str(p.id), YESTERDAY + timedelta(hours=9))
    iid = str(idea.id)
    await _snapshot("w1", iid, 1, NOW - timedelta(hours=2), impressions=10, likes=1)
    await _snapshot("w1", iid, 3, NOW, impressions=50, likes=4, replies=2, reposts=3, quotes=9)
    await _link("w1", iid, 3, "c1")
    await _link("w1", iid, 2, "c2")

    out = await _summary()
    assert [post.model_dump() for post in out.posts] == [
        {
            "idea_id": iid,
            "hook": "Ship it",
            "published_url": "https://x.com/a/status/Shipit",
            "published_at": YESTERDAY + timedelta(hours=9),
            "impressions": 50,
            "likes": 4,
            "replies": 2,
            "reposts": 3,
            "clicks": 5,
        }
    ]


@pytest.mark.asyncio
async def test_only_posts_published_yesterday_utc_count_sorted_by_time(mongo_db: Any):
    pid = str((await _profile()).id)
    first = await _idea("w1", pid, YESTERDAY, "first")  # 00:00:00 exactly: in
    last = await _idea("w1", pid, TODAY - timedelta(seconds=1), "last")
    mid = await _idea("w1", pid, YESTERDAY + timedelta(hours=12), "mid")
    await _idea("w1", pid, TODAY, "today")  # today's 00:00:00: out
    await _idea("w1", pid, YESTERDAY - timedelta(seconds=1), "day before")
    await _idea("w1", pid, None, "unpublished")
    await _idea("w1", pid, YESTERDAY + timedelta(hours=1), "reddit", platform="reddit")

    out = await _summary()
    assert [p.idea_id for p in out.posts] == [str(first.id), str(mid.id), str(last.id)]
    # The window is the UTC day, whatever offset ``now`` carries.
    # 01:30 on the 14th in UTC-5 is still the 14th in UTC.
    later = NOW.astimezone(timezone(timedelta(hours=-5)))
    assert [p.idea_id for p in (await _summary(now=later)).posts] == [
        str(first.id),
        str(mid.id),
        str(last.id),
    ]


@pytest.mark.asyncio
async def test_a_post_with_no_snapshot_or_link_reads_as_zeros(mongo_db: Any):
    pid = str((await _profile()).id)
    idea = await _idea("w1", pid, YESTERDAY + timedelta(hours=3))
    (post,) = (await _summary()).posts
    assert post.idea_id == str(idea.id)
    assert (post.impressions, post.likes, post.replies, post.reposts, post.clicks) == (0,) * 5


# --- repos --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_series_week_delta_and_uniques(mongo_db: Any):
    await _profile(repos=[REPO])
    d = TODAY.date()
    await _repo_day("w1", d - timedelta(days=15), 1, uniques=500)  # outside 14 days
    for back in range(14, 0, -1):  # 10-31 .. 11-13; only the latest lacks traffic
        stars = 200 - back * back
        await _repo_day("w1", d - timedelta(days=back), stars, uniques=back, traffic=back != 1)

    (repo,) = (await _summary()).repos
    assert repo.repo == REPO
    assert [pt.day for pt in repo.stars][0] == (d - timedelta(days=14)).isoformat()
    assert repo.stars[-1].day == (d - timedelta(days=1)).isoformat()
    # latest (11-13) is 199; 7 days earlier (11-06) is 200 - 64 = 136.
    assert repo.stars_week_delta == 199 - 136
    assert len(repo.stars) == 14
    # uniques of 11-07..11-13 (back = 7..1); 11-06 and older stay out.
    assert repo.uniques_week == sum(range(1, 8))
    assert repo.traffic_available is False


@pytest.mark.asyncio
async def test_repo_with_short_or_no_history(mongo_db: Any):
    await _profile(repos=[REPO, "acme/empty", "not a slug"])
    d = TODAY.date()
    await _repo_day("w1", d - timedelta(days=3), 10, uniques=2)
    await _repo_day("w1", d - timedelta(days=1), 12, uniques=3)

    short, empty = (await _summary()).repos
    assert short.model_dump() == {
        "repo": REPO,
        "traffic_available": True,
        "uniques_week": 5,
        "stars_week_delta": 0,  # no row 7 days before the latest
        "stars": [
            {"day": (d - timedelta(days=3)).isoformat(), "stars": 10},
            {"day": (d - timedelta(days=1)).isoformat(), "stars": 12},
        ],
    }
    assert empty.model_dump() == {
        "repo": "acme/empty",
        "traffic_available": False,
        "uniques_week": 0,
        "stars_week_delta": 0,
        "stars": [],
    }


@pytest.mark.asyncio
async def test_no_profile_or_no_data_is_empty(mongo_db: Any):
    assert (await _summary()).model_dump() == {"posts": [], "repos": []}
    await _profile()
    assert (await _summary()).model_dump() == {"posts": [], "repos": []}


# --- tenancy and profile scoping ------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_workspace_never_shows(mongo_db: Any):
    p = await _profile("w1", repos=[REPO])
    pid = str(p.id)
    mine = await _idea("w1", pid, YESTERDAY + timedelta(hours=8), "mine")
    iid = str(mine.id)
    # w2 rows forged to name w1's profile and idea: none may leak.
    await _idea("w2", pid, YESTERDAY + timedelta(hours=9), "theirs")
    await _snapshot("w2", iid, 1, NOW, impressions=999, likes=999)
    await _link("w2", iid, 999, "w2code")
    await _repo_day("w2", TODAY.date() - timedelta(days=1), 9999, uniques=999)

    out = await _summary("w1")
    assert [p.idea_id for p in out.posts] == [iid]
    assert (out.posts[0].impressions, out.posts[0].clicks) == (0, 0)
    assert out.repos[0].stars == []
    assert out.repos[0].uniques_week == 0
    # w2 asking for w1's profile by id gets nothing.
    assert (await _summary("w2", pid)).model_dump() == {"posts": [], "repos": []}


@pytest.mark.asyncio
async def test_one_profile_never_sees_anothers_posts_or_repos(mongo_db: Any):
    p1 = await _profile("w1", repos=[REPO])
    p2 = await _profile("w1", repos=["acme/other"])
    a = await _idea("w1", str(p1.id), YESTERDAY + timedelta(hours=1), "one")
    b = await _idea("w1", str(p2.id), YESTERDAY + timedelta(hours=2), "two")

    one = await _summary(profile_id=str(p1.id))
    two = await _summary(profile_id=str(p2.id))
    assert ([p.idea_id for p in one.posts], [r.repo for r in one.repos]) == ([str(a.id)], [REPO])
    assert ([p.idea_id for p in two.posts], [r.repo for r in two.repos]) == (
        [str(b.id)],
        ["acme/other"],
    )


# --- route ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_returns_the_strip_shape(mongo_db: Any):
    app = FastAPI()
    add_error_handler(app)
    app.include_router(social_router, prefix="/api/v1")
    app.dependency_overrides[request_context] = lambda: _ctx("w1")
    app.dependency_overrides[require_license] = lambda: None
    for dep in social_router.routes:
        if getattr(dep, "path", "") == "/growth/social/metrics/summary":
            for d in dep.dependencies:
                app.dependency_overrides[d.dependency] = lambda: None

    p = await _profile("w1", repos=[REPO])
    real_today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    await _idea("w1", str(p.id), real_today - timedelta(hours=6))
    await _repo_day("w1", real_today.date() - timedelta(days=1), 5)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get(
            "/api/v1/growth/social/metrics/summary", params={"profile_id": str(p.id)}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"posts", "repos"}
    assert set(body["posts"][0]) == {
        "idea_id",
        "hook",
        "published_url",
        "published_at",
        "impressions",
        "likes",
        "replies",
        "reposts",
        "clicks",
    }
    assert set(body["repos"][0]) == {
        "repo",
        "traffic_available",
        "uniques_week",
        "stars_week_delta",
        "stars",
    }
    assert set(body["repos"][0]["stars"][0]) == {"day", "stars"}
