# tests/cloud/growth/social/test_metrics.py — the Growth › Social published
# record and X post metrics: the capped read-only X reader (``x_api.x_read``),
# the batched metrics fn (``metrics.x_post_metrics``), the post-metrics sweep
# (``service.run_post_metrics``), ``latest_metrics`` and the 07:00 cron.
#
# mongomock-backed Beanie (the real ``try_spend`` meter runs on it), a fake
# Postiz through ``set_production_postiz``, fake metrics / reader fns through
# their seams, and an ``httpx.MockTransport`` for the real reader. No network.
#
# Covers: the published-record mapper round trip; no token = no HTTP and no
# claim; the daily read cap (reader returns None, sweep finishes, never stores
# zeros); HTTP errors and timeouts return None without logging the token; the
# reader has no write path; 100-id batches; x.com / twitter.com status URLs; the
# age-bucket rule; a second run neither re-reads nor overwrites a snapshot; one
# failing workspace never stops the others; ``latest_metrics`` stays in its
# workspace; the cron entry and its fn installation.

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core.crypto import encrypt
from pocketpaw_ee.cloud.growth.social import metrics as social_metrics
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social import x_api
from pocketpaw_ee.cloud.growth.social.postiz import set_production_postiz
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc
from pocketpaw_ee.cloud.models.social_post_metric import SocialPostMetric as MetricDoc
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg as OrgDoc

TOKEN = "secret-bearer-token-xyz"


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv(x_api.TOKEN_ENV, raising=False)
    monkeypatch.delenv("POCKETPAW_GROWTH_X_DAILY_READ_CAP", raising=False)
    _reset_all()
    try:
        yield
    finally:
        _reset_all()


def _reset_all() -> None:
    social_metrics.set_production_metrics_fn(None)
    x_api.set_x_read_fn(None)
    set_production_postiz(None)


class _Http:
    """Counts requests to the mocked X API and answers each with its ids' metrics."""

    def __init__(self, status: int = 200, raises: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.raises = raises

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = httpx.AsyncClient

        def _client(**kw: Any) -> httpx.AsyncClient:
            return real(transport=httpx.MockTransport(self._handle), **kw)

        monkeypatch.setattr(x_api.httpx, "AsyncClient", _client)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        ids = (request.url.params.get("ids") or "").split(",")
        data = [{"id": i, "public_metrics": {"impression_count": 10, "like_count": 2}} for i in ids]
        return httpx.Response(self.status, json={"data": data})


class _FakeMetrics:
    def __init__(self, fail_for: str = "") -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.likes = 1
        self.fail_for = fail_for

    async def __call__(self, workspace_id: str, ids: list[str]) -> dict[str, dict[str, int]]:
        self.calls.append((workspace_id, list(ids)))
        if workspace_id == self.fail_for:
            raise RuntimeError("x is down")
        return {
            i: {
                "impressions": 100,
                "likes": self.likes,
                "replies": 3,
                "reposts": 4,
                "quotes": 5,
                "bookmarks": 6,
            }
            for i in ids
        }


class _FakePostiz:
    def __init__(self, posts: list[dict[str, Any]]) -> None:
        self.posts = posts
        self.listed: list[str] = []

    async def list_posts(self, api_key: str, start: str, end: str) -> list[dict[str, Any]]:
        self.listed.append(api_key)
        return list(self.posts)


async def _idea(workspace: str = "w1", **kw: Any) -> IdeaDoc:
    doc = IdeaDoc(
        workspace=workspace,
        profile=kw.pop("profile", "p1"),
        format="x_post",
        hook="Ship it",
        platform=kw.pop("platform", "x"),
        **kw,
    )
    await doc.insert()
    return doc


async def _published(workspace: str, x_id: str, age: timedelta, now: datetime) -> IdeaDoc:
    return await _idea(
        workspace, x_post_id=x_id, published_at=now - age, published_url=f"https://x.com/a/{x_id}"
    )


def _now() -> datetime:
    return datetime.now(UTC)


# --- the published record ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idea_mappers_carry_the_published_record(mongo_db: Any):
    at = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
    doc = await _idea(
        published_url="https://x.com/acme/status/123", published_at=at, x_post_id="123"
    )
    domain = social_service._idea_to_domain(doc)
    assert (domain.published_url, domain.x_post_id) == ("https://x.com/acme/status/123", "123")
    assert domain.published_at is not None
    resp = social_service._idea_to_response(domain)
    assert resp.published_url == "https://x.com/acme/status/123"
    assert resp.published_at is not None and resp.published_at.startswith("2026-11-01T14:00")
    assert resp.x_post_id == "123"


@pytest.mark.asyncio
async def test_old_ideas_read_an_empty_published_record(mongo_db: Any):
    doc = await _idea()
    await IdeaDoc.get_pymongo_collection().update_one(
        {"_id": doc.id}, {"$unset": {"published_url": "", "published_at": "", "x_post_id": ""}}
    )
    resp = social_service._idea_to_response(
        social_service._idea_to_domain(await IdeaDoc.get(doc.id))
    )
    assert (resp.published_url, resp.published_at, resp.x_post_id) == ("", None, "")


# --- the capped reader ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reader_without_a_token_makes_no_http_call_and_claims_nothing(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
):
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.metering.domain import DailyMeter

    http = _Http()
    http.install(monkeypatch)
    assert await x_api.x_read("w1", "/2/tweets", {"ids": "1"}) is None
    assert http.requests == []
    used = await metering.used(
        subject_type="workspace", subject_id="w1", meter=DailyMeter.GROWTH_X_READS
    )
    assert used == 0


@pytest.mark.asyncio
async def test_reader_stops_at_the_daily_cap(mongo_db: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(x_api.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("POCKETPAW_GROWTH_X_DAILY_READ_CAP", "2")
    http = _Http()
    http.install(monkeypatch)
    assert await x_api.x_read("w1", "/2/tweets", {"ids": "1"}) is not None
    assert await x_api.x_read("w1", "/2/tweets", {"ids": "1"}) is not None
    assert await x_api.x_read("w1", "/2/tweets", {"ids": "1"}) is None
    assert len(http.requests) == 2
    # The cap is per workspace.
    assert await x_api.x_read("w2", "/2/tweets", {"ids": "1"}) is not None


@pytest.mark.asyncio
async def test_reader_sends_a_bearer_get_to_x(mongo_db: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(x_api.TOKEN_ENV, TOKEN)
    http = _Http()
    http.install(monkeypatch)
    await x_api.x_read("w1", "/2/tweets", {"ids": "1,2", "tweet.fields": "public_metrics"})
    (req,) = http.requests
    assert req.method == "GET"
    assert str(req.url).startswith("https://api.x.com/2/tweets?")
    assert req.url.params["ids"] == "1,2"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "http",
    [_Http(status=503), _Http(status=429), _Http(raises=httpx.ReadTimeout("slow"))],
    ids=["503", "429", "timeout"],
)
async def test_reader_failures_return_none_and_never_log_the_token(
    http: _Http, mongo_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv(x_api.TOKEN_ENV, TOKEN)
    http.install(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        assert await x_api.x_read("w1", "/2/tweets", {"ids": "1"}) is None
    assert caplog.records
    assert TOKEN not in caplog.text


def test_reader_module_has_no_write_path():
    source = inspect.getsource(x_api)
    for verb in ("post", "put", "patch", "delete", "request", "send"):
        assert f".{verb}(" not in source
    assert "method=" not in source
    own = {
        n
        for n, v in vars(x_api).items()
        if inspect.isfunction(v) and v.__module__ == x_api.__name__ and not n.startswith("_")
    }
    assert own == {"x_read", "set_x_read_fn", "resolve_x_read_fn"}


# --- the metrics fn ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_read_in_batches_of_at_most_100_and_map_x_counts():
    calls: list[tuple[str, str, dict]] = []

    async def _read(workspace_id: str, path: str, params: dict | None = None) -> dict | None:
        calls.append((workspace_id, path, dict(params or {})))
        ids = params["ids"].split(",") if params else []
        metric = {
            "impression_count": 9,
            "like_count": 8,
            "reply_count": 7,
            "retweet_count": 6,
            "quote_count": 5,
            "bookmark_count": 4,
        }
        return {"data": [{"id": i, "public_metrics": metric} for i in ids]}

    x_api.set_x_read_fn(_read)
    ids = [str(n) for n in range(150)]
    out = await social_metrics.x_post_metrics("w1", ids)
    assert [len(c[2]["ids"].split(",")) for c in calls] == [100, 50]
    assert all(c[0] == "w1" and c[1] == "/2/tweets" for c in calls)
    assert all(c[2]["tweet.fields"] == "public_metrics" for c in calls)
    assert len(out) == 150
    assert out["0"] == {
        "impressions": 9,
        "likes": 8,
        "replies": 7,
        "reposts": 6,
        "quotes": 5,
        "bookmarks": 4,
    }


@pytest.mark.asyncio
async def test_a_refused_batch_contributes_nothing():
    async def _read(workspace_id: str, path: str, params: dict | None = None) -> dict | None:
        return None

    x_api.set_x_read_fn(_read)
    assert await social_metrics.x_post_metrics("w1", ["1", "2"]) == {}


# --- URL parse and buckets -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.com/acme/status/1850000000000000001", "1850000000000000001"),
        ("https://twitter.com/acme/status/42", "42"),
        ("https://mobile.twitter.com/acme/status/42?s=20", "42"),
        ("https://x.com/i/web/status/77", "77"),
        ("https://linkedin.com/feed/update/status/42", ""),
        ("https://evilx.com/acme/status/42", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_x_status_id(url: Any, expected: str):
    assert social_service.x_status_id(url) == expected


@pytest.mark.parametrize(
    ("age_hours", "bucket"),
    [(12, None), (24, 1), (47, 1), (72, 3), (6 * 24, 3), (7 * 24, 7), (27 * 24, 7), (28 * 24, 28)],
)
def test_due_bucket_is_the_largest_bucket_reached(age_hours: int, bucket: int | None):
    now = _now()
    assert social_service.due_bucket(now - timedelta(hours=age_hours), now) == bucket


# --- the sweep -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_records_the_published_post_then_snapshots_each_bucket_once(mongo_db: Any):
    now = _now()
    await OrgDoc(
        workspace="w1", profile="p1", postiz_org_id="o1", api_key_enc=encrypt("k1")
    ).insert()
    idea = await _idea(post_ids=["p-li", "p-x"])
    published = (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    postiz = _FakePostiz(
        [
            {"id": "p-li", "state": "PUBLISHED", "releaseURL": "https://linkedin.com/x/1"},
            {
                "id": "p-x",
                "state": "PUBLISHED",
                "releaseURL": "https://x.com/acme/status/123",
                "publishDate": published,
            },
        ]
    )
    set_production_postiz(postiz)  # type: ignore[arg-type]
    fake = _FakeMetrics()
    social_metrics.set_production_metrics_fn(fake)

    counts = await social_service.run_post_metrics(now)
    assert counts["filled"] == 1 and counts["stored"] == 1
    assert postiz.listed == ["k1"]
    doc = await IdeaDoc.get(idea.id)
    assert (doc.x_post_id, doc.published_url) == ("123", "https://x.com/acme/status/123")
    assert abs(social_service._aware(doc.published_at) - (now - timedelta(hours=25))) < timedelta(
        seconds=1
    )
    assert fake.calls == [("w1", ["123"])]
    (row,) = await MetricDoc.find({}).to_list()
    assert (row.workspace, row.profile, row.idea, row.age_bucket) == ("w1", "p1", str(idea.id), 1)
    assert (row.impressions, row.likes, row.bookmarks) == (100, 1, 6)

    # Same day again: nothing due, no X read, the stored snapshot is untouched.
    fake.likes = 99
    again = await social_service.run_post_metrics(now + timedelta(hours=1))
    assert again["filled"] == 0 and again["stored"] == 0
    assert fake.calls == [("w1", ["123"])]
    (row,) = await MetricDoc.find({}).to_list()
    assert row.likes == 1

    # Day three: the 3-day bucket, as a new row.
    await social_service.run_post_metrics(now + timedelta(days=2))
    rows = await MetricDoc.find({}).sort("age_bucket").to_list()
    assert [(r.age_bucket, r.likes) for r in rows] == [(1, 1), (3, 99)]


@pytest.mark.asyncio
async def test_unpublished_or_non_x_ideas_are_left_alone(mongo_db: Any):
    now = _now()
    await OrgDoc(
        workspace="w1", profile="p1", postiz_org_id="o1", api_key_enc=encrypt("k1")
    ).insert()
    queued = await _idea(post_ids=["p-q"])
    reddit = await _idea(post_ids=["p-r"], platform="reddit")
    set_production_postiz(
        _FakePostiz(  # type: ignore[arg-type]
            [
                {"id": "p-q", "state": "QUEUE", "releaseURL": ""},
                {"id": "p-r", "state": "PUBLISHED", "releaseURL": "https://x.com/a/status/9"},
            ]
        )
    )
    social_metrics.set_production_metrics_fn(_FakeMetrics())
    assert (await social_service.run_post_metrics(now))["filled"] == 0
    assert (await IdeaDoc.get(queued.id)).x_post_id == ""
    assert (await IdeaDoc.get(reddit.id)).x_post_id == ""


@pytest.mark.asyncio
async def test_without_postiz_the_sweep_still_snapshots_known_posts(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(social_service, "resolve_postiz", lambda: None)
    now = _now()
    await _idea(post_ids=["p-x"])  # needs Postiz to be found; skipped quietly
    await _published("w1", "555", timedelta(days=7, hours=1), now)
    fake = _FakeMetrics()
    social_metrics.set_production_metrics_fn(fake)
    counts = await social_service.run_post_metrics(now)
    assert counts["failed"] == 0 and counts["filled"] == 0
    assert fake.calls == [("w1", ["555"])]
    (row,) = await MetricDoc.find({}).to_list()
    assert row.age_bucket == 7


@pytest.mark.asyncio
async def test_one_failing_workspace_does_not_stop_the_others(mongo_db: Any):
    now = _now()
    await _published("w1", "1", timedelta(days=1, hours=1), now)
    await _published("w2", "2", timedelta(days=1, hours=1), now)
    fake = _FakeMetrics(fail_for="w1")
    social_metrics.set_production_metrics_fn(fake)
    counts = await social_service.run_post_metrics(now)
    assert counts["failed"] == 1 and counts["stored"] == 1
    (row,) = await MetricDoc.find({}).to_list()
    assert row.workspace == "w2"
    assert {c[0] for c in fake.calls} == {"w1", "w2"}


@pytest.mark.asyncio
async def test_each_workspace_is_read_with_only_its_own_posts(mongo_db: Any):
    now = _now()
    await _published("w1", "1", timedelta(days=1, hours=1), now)
    await _published("w2", "2", timedelta(days=1, hours=1), now)
    fake = _FakeMetrics()
    social_metrics.set_production_metrics_fn(fake)
    await social_service.run_post_metrics(now)
    assert sorted(fake.calls) == [("w1", ["1"]), ("w2", ["2"])]


@pytest.mark.asyncio
async def test_sweep_respects_the_read_cap_and_never_stores_zeros(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
):
    """150 due posts need two reads; with a cap of one, the second batch is refused, the
    sweep finishes, and only the 100 posts actually read get a snapshot."""
    now = _now()
    monkeypatch.setenv(x_api.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("POCKETPAW_GROWTH_X_DAILY_READ_CAP", "1")
    http = _Http()
    http.install(monkeypatch)
    for n in range(150):
        await _published("w1", str(1000 + n), timedelta(days=1, hours=1), now)
    social_metrics.set_production_metrics_fn(social_metrics.x_post_metrics)

    counts = await social_service.run_post_metrics(now)
    assert len(http.requests) == 1
    assert counts["due"] == 150 and counts["stored"] == 100 and counts["failed"] == 0
    assert await MetricDoc.find({}).count() == 100


@pytest.mark.asyncio
async def test_no_metrics_fn_is_a_no_op(mongo_db: Any):
    await _published("w1", "1", timedelta(days=1, hours=1), _now())
    assert (await social_service.run_post_metrics())["stored"] == 0


# --- latest_metrics ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_metrics_returns_the_newest_snapshot_in_the_workspace_only(mongo_db: Any):
    now = _now()

    def _row(workspace: str, bucket: int, likes: int, at: datetime) -> MetricDoc:
        return MetricDoc(
            workspace=workspace,
            profile="p1",
            idea="idea-1",
            x_post_id="1",
            age_bucket=bucket,
            likes=likes,
            captured_at=at,
        )

    await _row("w1", 1, 5, now - timedelta(days=2)).insert()
    await _row("w1", 3, 7, now).insert()
    # Another workspace's row naming the same idea id, captured later: must not leak.
    await _row("w2", 7, 999, now + timedelta(days=1)).insert()

    out = await social_service.latest_metrics("w1", ["idea-1", "missing"])
    assert set(out) == {"idea-1"}
    assert (out["idea-1"].workspace_id, out["idea-1"].age_bucket) == ("w1", 3)
    assert out["idea-1"].likes == 7
    assert await social_service.latest_metrics("w3", ["idea-1"]) == {}


@pytest.mark.asyncio
async def test_a_snapshot_bucket_is_unique(mongo_db: Any):
    from pymongo.errors import DuplicateKeyError

    row = {"workspace": "w1", "idea": "i", "x_post_id": "1", "age_bucket": 1, "captured_at": _now()}
    await MetricDoc(**row).insert()
    with pytest.raises(DuplicateKeyError):
        await MetricDoc(**row).insert()


# --- the cron ------------------------------------------------------------------------------


def test_worker_registers_the_post_metrics_cron_at_seven():
    from pocketpaw_ee.cloud.growth.domain import GROWTH_SOCIAL_POST_METRICS_JOB_NAME
    from pocketpaw_ee.cloud.growth.worker import WorkerSettings, social_post_metrics

    assert GROWTH_SOCIAL_POST_METRICS_JOB_NAME == "growth.social_post_metrics"
    jobs = {job.name: job for job in WorkerSettings.cron_jobs}
    job = jobs[GROWTH_SOCIAL_POST_METRICS_JOB_NAME]
    assert (job.hour, job.minute) == (7, 0)
    assert job.unique is True and job.run_at_startup is False
    assert job.coroutine is social_post_metrics


@pytest.mark.asyncio
async def test_worker_job_installs_the_production_metrics_fn_when_missing(monkeypatch):
    from pocketpaw_ee.cloud.growth import worker

    async def _stub_run(now: datetime | None = None) -> dict[str, int]:
        return {"stored": 0}

    monkeypatch.setattr(social_service, "run_post_metrics", _stub_run)
    assert (await worker.social_post_metrics({}))["stored"] == 0
    assert social_metrics.resolve_metrics_fn() is social_metrics.x_post_metrics

    fake = _FakeMetrics()
    social_metrics.set_production_metrics_fn(fake)
    await worker.social_post_metrics({})
    assert social_metrics.resolve_metrics_fn() is fake
