# tests/cloud/growth/social/test_repo_metrics.py — the daily repo metrics: the
# production GitHub reader (``github_repo_metrics``, over ``httpx.MockTransport``),
# the sweep (``run_repo_metrics``) and the read (``repo_metrics``) over
# mongomock-backed Beanie with a fake ``RepoMetricsFn``, and the worker cron.
#
# Covers: stars and forks only without a token and on a traffic 403; traffic stored
# with a token, picking yesterday's entry out of the 14-day arrays; a failed repo
# read raises; invalid slugs never reach a URL; the token is sent and never logged.
# The sweep stores one row per (workspace, repo, yesterday), reads each repo once,
# refreshes the row on a rerun, keeps going past a failing repo, skips inactive
# profiles and invalid slugs. ``repo_metrics`` is workspace-scoped.

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pocketpaw_ee.cloud.growth.social import repo_metrics as social_repo_metrics
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.repo_metrics import (
    RepoSnapshot,
    github_repo_metrics,
    set_production_repo_metrics_fn,
)
from pocketpaw_ee.cloud.models.repo_metric import RepoMetric as MetricDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as ProfileDoc

TOKEN_ENV = "POCKETPAW_GROWTH_GITHUB_TOKEN"
NOW = datetime(2026, 11, 14, 6, 30, tzinfo=UTC)
YESTERDAY = "2026-11-13"

REPO = {"stargazers_count": 120, "forks_count": 7}
VIEWS = {
    "count": 999,
    "uniques": 99,
    "views": [
        {"timestamp": "2026-11-12T00:00:00Z", "count": 10, "uniques": 1},
        {"timestamp": "2026-11-13T00:00:00Z", "count": 42, "uniques": 9},
        {"timestamp": "2026-11-14T00:00:00Z", "count": 3, "uniques": 2},
    ],
}
CLONES = {
    "clones": [
        {"timestamp": "2026-11-12T00:00:00Z", "count": 50, "uniques": 5},
        {"timestamp": "2026-11-13T00:00:00Z", "count": 6, "uniques": 4},
        {"timestamp": "2026-11-14T00:00:00Z", "count": 1, "uniques": 1},
    ]
}
REFERRERS = [{"referrer": "news.ycombinator.com", "count": 30, "uniques": 20}]


@pytest.fixture(autouse=True)
def _reset_seam():
    set_production_repo_metrics_fn(None)
    try:
        yield
    finally:
        set_production_repo_metrics_fn(None)


# --- the production reader ------------------------------------------------------------


def _client(seen: list[httpx.Request], traffic_status: int = 200, repo_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if "/traffic/" in path:
            if traffic_status != 200:
                return httpx.Response(traffic_status, json={"message": "Must have push access"})
            if path.endswith("/views"):
                return httpx.Response(200, json=VIEWS)
            if path.endswith("/clones"):
                return httpx.Response(200, json=CLONES)
            return httpx.Response(200, json=REFERRERS)
        return httpx.Response(repo_status, json=REPO)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_reader_without_token_stores_stars_only(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    seen: list[httpx.Request] = []
    async with _client(seen) as client:
        snap = await github_repo_metrics("acme/app", day=YESTERDAY, client=client)
    assert snap == RepoSnapshot("acme/app", YESTERDAY, stars=120, forks=7)
    assert [r.url.path for r in seen] == ["/repos/acme/app"]
    assert all(r.url.host == "api.github.com" for r in seen)
    assert all("authorization" not in r.headers for r in seen)


@pytest.mark.asyncio
async def test_reader_traffic_403_keeps_stars_and_never_logs_the_token(monkeypatch, caplog):
    monkeypatch.setenv(TOKEN_ENV, "sekrit-token")
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []
    async with _client(seen, traffic_status=403) as client:
        snap = await github_repo_metrics("acme/app", day=YESTERDAY, client=client)
    assert snap == RepoSnapshot("acme/app", YESTERDAY, stars=120, forks=7)
    assert snap.traffic_available is False
    assert seen and all(r.headers["authorization"] == "Bearer sekrit-token" for r in seen)
    assert "acme/app traffic unavailable (403)" in caplog.text
    assert "sekrit-token" not in caplog.text


@pytest.mark.asyncio
async def test_reader_with_token_picks_yesterdays_traffic(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "tok123")
    seen: list[httpx.Request] = []
    async with _client(seen) as client:
        snap = await github_repo_metrics("acme/app", day=YESTERDAY, client=client)
    assert snap == RepoSnapshot(
        "acme/app",
        YESTERDAY,
        stars=120,
        forks=7,
        views=42,
        uniques=9,
        clones=6,
        clone_uniques=4,
        referrers=({"referrer": "news.ycombinator.com", "count": 30, "uniques": 20},),
        traffic_available=True,
    )
    assert {r.url.path for r in seen} == {
        "/repos/acme/app",
        "/repos/acme/app/traffic/views",
        "/repos/acme/app/traffic/clones",
        "/repos/acme/app/traffic/popular/referrers",
    }


@pytest.mark.asyncio
async def test_reader_day_with_no_traffic_entry_is_zero(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "tok123")
    async with _client([]) as client:
        snap = await github_repo_metrics("acme/app", day="2026-11-01", client=client)
    assert (snap.views, snap.clones, snap.traffic_available) == (0, 0, True)


@pytest.mark.asyncio
async def test_reader_failed_repo_read_raises(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    async with _client([], repo_status=500) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await github_repo_metrics("acme/app", day=YESTERDAY, client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["../etc", "no-slash", "a/b/c", "evil.com/x?y", ""])
async def test_reader_rejects_invalid_slug_before_any_request(monkeypatch, slug):
    monkeypatch.setenv(TOKEN_ENV, "tok123")
    seen: list[httpx.Request] = []
    async with _client(seen) as client:
        with pytest.raises(ValueError):
            await github_repo_metrics(slug, day=YESTERDAY, client=client)
    assert seen == []


# --- the sweep and the read ------------------------------------------------------------


class _FakeMetrics:
    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, repo: str, *, day: str) -> RepoSnapshot:
        self.calls.append((repo, day))
        if repo == self.fail:
            raise RuntimeError("github down")
        return RepoSnapshot(repo, day, stars=len(self.calls), views=5, traffic_available=True)


async def _profile(workspace: str, repos: list[str], active: bool = True) -> None:
    await ProfileDoc(workspace=workspace, active=active, github_repos=repos).insert()


async def _rows() -> list[MetricDoc]:
    return await MetricDoc.find({}).sort("workspace", "repo").to_list()


@pytest.mark.asyncio
async def test_sweep_stores_yesterday_once_per_workspace_repo(mongo_db: Any):
    fake = _FakeMetrics()
    set_production_repo_metrics_fn(fake)
    await _profile("w1", ["acme/app"])
    await _profile("w1", ["acme/app", "acme/cli"])  # a second product, same repo
    await _profile("w2", ["acme/app"])

    counts = await social_service.run_repo_metrics(now=NOW)

    assert counts == {"repos": 3, "stored": 3, "failed": 0}
    assert sorted(fake.calls) == [("acme/app", YESTERDAY), ("acme/cli", YESTERDAY)]
    rows = await _rows()
    assert [(r.workspace, r.repo, r.day) for r in rows] == [
        ("w1", "acme/app", YESTERDAY),
        ("w1", "acme/cli", YESTERDAY),
        ("w2", "acme/app", YESTERDAY),
    ]
    assert all(r.traffic_available and r.views == 5 for r in rows)


@pytest.mark.asyncio
async def test_sweep_rerun_refreshes_the_same_days_row(mongo_db: Any):
    fake = _FakeMetrics()
    set_production_repo_metrics_fn(fake)
    await _profile("w1", ["acme/app"])

    await social_service.run_repo_metrics(now=NOW)
    await social_service.run_repo_metrics(now=NOW.replace(hour=9))

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].stars == 2  # the second run's read
    assert rows[0].day == YESTERDAY


@pytest.mark.asyncio
async def test_sweep_rerun_without_traffic_keeps_the_stored_traffic(mongo_db: Any):
    reads = iter([True, False])

    async def _fn(repo: str, *, day: str) -> RepoSnapshot:
        ok = next(reads)
        return RepoSnapshot(
            repo, day, stars=10 if ok else 11, views=5 if ok else 0, traffic_available=ok
        )

    set_production_repo_metrics_fn(_fn)
    await _profile("w1", ["acme/app"])
    await social_service.run_repo_metrics(now=NOW)
    await social_service.run_repo_metrics(now=NOW.replace(hour=9))

    (row,) = await _rows()
    assert (row.stars, row.views, row.traffic_available) == (11, 5, True)


@pytest.mark.asyncio
async def test_sweep_failing_repo_does_not_stop_the_others(mongo_db: Any):
    set_production_repo_metrics_fn(_FakeMetrics(fail="acme/broken"))
    await _profile("w1", ["acme/broken", "acme/app"])

    counts = await social_service.run_repo_metrics(now=NOW)

    assert counts == {"repos": 2, "stored": 1, "failed": 1}
    assert [r.repo for r in await _rows()] == ["acme/app"]


@pytest.mark.asyncio
async def test_sweep_skips_inactive_profiles_and_invalid_slugs(mongo_db: Any):
    fake = _FakeMetrics()
    set_production_repo_metrics_fn(fake)
    await _profile("w1", ["acme/off"], active=False)
    await _profile("w1", ["../etc", "no-slash", "acme/app"])

    counts = await social_service.run_repo_metrics(now=NOW)

    assert counts == {"repos": 1, "stored": 1, "failed": 0}
    assert fake.calls == [("acme/app", YESTERDAY)]
    assert [r.repo for r in await _rows()] == ["acme/app"]


@pytest.mark.asyncio
async def test_sweep_without_a_reader_is_a_no_op(mongo_db: Any):
    await _profile("w1", ["acme/app"])
    assert await social_service.run_repo_metrics(now=NOW) == {
        "repos": 0,
        "stored": 0,
        "failed": 0,
    }
    assert await _rows() == []


@pytest.mark.asyncio
async def test_repo_metrics_read_is_workspace_scoped_and_since_filtered(mongo_db: Any):
    set_production_repo_metrics_fn(_FakeMetrics())
    await _profile("w1", ["acme/app"])
    await _profile("w2", ["acme/app"])
    await social_service.run_repo_metrics(now=datetime(2026, 11, 12, 6, 30, tzinfo=UTC))
    await social_service.run_repo_metrics(now=NOW)

    w1 = await social_service.repo_metrics("w1", "acme/app", date(2026, 11, 1))
    assert [s.day for s in w1] == ["2026-11-11", YESTERDAY]
    assert all(isinstance(s, RepoSnapshot) and s.repo == "acme/app" for s in w1)
    assert len(await social_service.repo_metrics("w2", "acme/app", date(2026, 11, 1))) == 2
    assert await social_service.repo_metrics("w3", "acme/app", date(2026, 11, 1)) == []
    recent = await social_service.repo_metrics("w1", "acme/app", date(2026, 11, 13))
    assert [s.day for s in recent] == [YESTERDAY]


# --- the cron ---------------------------------------------------------------------------


def test_worker_registers_the_repo_metrics_cron():
    from pocketpaw_ee.cloud.growth.domain import GROWTH_SOCIAL_REPO_METRICS_JOB_NAME
    from pocketpaw_ee.cloud.growth.worker import WorkerSettings, social_repo_metrics

    assert GROWTH_SOCIAL_REPO_METRICS_JOB_NAME == "growth.social_repo_metrics"
    jobs = {job.name: job for job in WorkerSettings.cron_jobs}
    job = jobs[GROWTH_SOCIAL_REPO_METRICS_JOB_NAME]
    assert (job.hour, job.minute) == (6, 30)
    assert job.unique is True and job.run_at_startup is False
    assert job.coroutine is social_repo_metrics


@pytest.mark.asyncio
async def test_worker_job_installs_the_production_reader_when_missing(monkeypatch):
    from pocketpaw_ee.cloud.growth import worker

    async def _stub_run(now: datetime | None = None) -> dict[str, int]:
        return {"repos": 0, "stored": 0, "failed": 0}

    monkeypatch.setattr(social_service, "run_repo_metrics", _stub_run)
    assert (await worker.social_repo_metrics({}))["stored"] == 0
    assert social_repo_metrics.resolve_repo_metrics_fn() is github_repo_metrics

    fake = _FakeMetrics()
    set_production_repo_metrics_fn(fake)
    await worker.social_repo_metrics({})
    assert social_repo_metrics.resolve_repo_metrics_fn() is fake
