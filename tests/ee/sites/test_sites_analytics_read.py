# tests/ee/sites/test_sites_analytics_read.py — SA-4, the read half of Paw Sites
# visitor analytics: GET /sites/{site_id}/analytics and the aggregation behind it.
#
# Created 2026-09-02 (feat/sites-analytics-read).
#
# Updated 2026-09-04 (AD-2 — visits, bounce rate and visit duration): section 8 covers
# the visit block and the FOURTH empty state, a window whose rows all predate the visit
# id and which has to read as "republish to start measuring visits" rather than as a zero
# bounce rate. Its fixtures are RAW PAGEVIEWS rather than scripted aggregates, so the
# hour-boundary split is present in the data instead of being asserted into existence —
# the section's own note says why that distinction is the point.
#
# THE REQUIREMENT THIS WHOLE FILE EXISTS FOR: never invent a zero. Four situations can
# leave a dashboard looking empty and they are four different sentences to a customer:
#
#   1. this plan does not buy analytics
#   2. it does, but no publish has deployed a counter, so nothing was ever recorded
#   3. a counter is up, and genuinely nobody visited
#   4. the read itself failed
#
# Only the third is a report about their traffic. Serving 0 for the first two tells
# someone their marketing is dead when the truth is they have not upgraded, or have not
# republished since they did. And the fourth is deliberately NOT a status value: a
# client that maps an unfamiliar status to "no data" would render a Cloudflare outage
# as a quiet week, so a failed read is an error response. Each of the four is asserted
# here, and asserted as DISTINGUISHABLE rather than merely as "not a crash".
#
# THE ROW SHAPE IS A MOVING TARGET, which the device tests are about. SA-3 appends a
# device class at ``blobs[4]`` in a separate slice that may or may not land. A row
# written before it has four blobs and one written after has five, Analytics Engine has
# no schema and answers an empty string for a blob a row never carried, so BOTH shapes
# arrive inside one window. Neither may crash, and neither may silently miscount — an
# empty device is bucketed as unknown rather than dropped, because dropping it rescales
# every other device's share.
#
# The Cloudflare client is FAKED at the seam that speaks HTTP (``query_analytics_sql``)
# rather than at the service function, so everything between the route and the wire —
# the entitlement gate, the SQL, the row mapping, the cache — is real code under test.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, ValidationError  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402

SITE_TIER = "site"


class _FakeAnalyticsCF:
    """A Cloudflare client that answers SQL from a scripted table.

    Faked at ``query_analytics_sql`` — the one method that speaks HTTP — so the SQL
    text, the row mapping, the status decision and the cache are all real code here.
    Every statement is recorded, which is how the injection and window tests assert on
    what was actually sent rather than on what the caller meant to send.
    """

    def __init__(
        self,
        rows_by_blob: dict[str, list[dict]] | None = None,
        totals=None,
        visits: dict | None = None,
    ) -> None:
        self.rows_by_blob = rows_by_blob or {}
        self.totals = totals
        self.visits = visits
        self.queries: list[str] = []

    async def query_analytics_sql(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        if "blob6 AS visit" in sql:
            return [self.visits if self.visits is not None else self._default_visits()]
        if "GROUP BY" not in sql:
            return [] if self.totals is None else [self.totals]
        for blob, rows in self.rows_by_blob.items():
            if f"{blob} AS label" in sql:
                return rows
        return []

    def _default_visits(self) -> dict:
        """The smallest visit answer COHERENT with this fake's scripted totals.

        Every test written before AD-2 needs one. Defaulting to nothing would drop all of
        them into the fourth empty state — "no row here has ever carried a visit id" —
        which is a real answer to a question they are not asking, and they would then be
        asserting it by accident. So a fixture that scripts pageviews gets one visit that
        bounced, and a fixture that scripts none gets none. Section 8 does not use this
        path: its fake aggregates raw rows."""
        raw = (self.totals or {}).get("pageviews", 0)
        counted = int(raw) if str(raw).isdigit() else 0
        if counted <= 0:
            return {"visits": 0, "bounces": 0, "total_seconds": 0}
        return {"visits": 1, "bounces": 1, "total_seconds": 0}


class _ExplodingCF:
    """A client whose read fails the way the real one does — see
    ``cloudflare_client.query_analytics_sql``, which is fail-closed on a non-2xx, a
    non-JSON body, and a 2xx with no data array."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def query_analytics_sql(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        raise ValidationError(
            "sites.cloudflare_error", "Analytics Engine SQL 503: service unavailable"
        )


def _rows(*pairs: tuple[str, int, int]) -> list[dict]:
    return [{"label": label, "pageviews": pv, "visitors": v} for label, pv, v in pairs]


async def _seed_site(
    *,
    workspace_id: str = "ws-read",
    pocket_id: str = "pk-read",
    plan_tier: str | None = SITE_TIER,
    subscription_status: str | None = "active",
    counting_since: datetime | None = None,
) -> Any:
    """Insert a Site document directly.

    Built rather than published because every case here is about the state of a row a
    publish has ALREADY produced — the publish path's own writes are asserted in
    test_sites_analytics_since.py, and going through it would make each of these tests
    depend on a build, a deploy and a badge stamper that have nothing to do with the
    read.
    """
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site

    doc = Site(
        id=ObjectId(),
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner="u1",
        name="Read Site",
        deployed=True,
        plan_tier=plan_tier,
        subscription_status=subscription_status,
        analytics_since=counting_since,
    )
    await doc.insert()
    return doc


@pytest.fixture(autouse=True)
def _empty_cache():
    """Clear the read cache around every test.

    It is process-global, so without this a response assembled by one test would be
    served to the next — which would also hide a cache bug behind whichever test ran
    first."""
    sites_service._analytics_cache.clear()
    yield
    sites_service._analytics_cache.clear()


# ── 1. the four outcomes ─────────────────────────────────────────────────────
#
# Named for the CUSTOMER SITUATION rather than for the input, because the failure being
# guarded against is one where all four render the same and only the wording of the
# assertion tells you which one broke.


@pytest.mark.asyncio
async def test_a_site_whose_plan_does_not_buy_analytics_says_so(beanie_test_db):
    """State 1. Nothing was ever recorded and nothing can be until the plan changes, so
    every metric is None — a UI that renders the numbers without reading the status
    shows blanks rather than a confident zero."""
    site = await _seed_site(plan_tier="free", counting_since=datetime.now(UTC))
    cf = _FakeAnalyticsCF()

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "not_entitled"
    assert out.pageviews is None
    assert out.visitors is None
    assert out.counting_since is None
    assert cf.queries == [], "an unentitled site must not spend a billed read"


@pytest.mark.asyncio
async def test_an_entitled_site_that_has_never_counted_says_so(beanie_test_db):
    """State 2, and the state the ``analytics_since`` field exists to make knowable.
    The site is paying; no publish has deployed a counter, so there is nothing to read
    and republishing is the fix. Distinguishable from state 3 below, which is the whole
    point."""
    site = await _seed_site(counting_since=None)
    cf = _FakeAnalyticsCF()

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "never_counted"
    assert out.pageviews is None
    assert out.counting_since is None
    assert cf.queries == [], "there is nothing recorded to query"


@pytest.mark.asyncio
async def test_a_counting_site_with_no_traffic_reports_a_real_zero(beanie_test_db):
    """State 3. A counter IS up, the query really ran, and it really found nothing.
    THIS is the only state in which a zero is honest — and it carries
    ``counting_since``, so the panel can say how long that zero has been true."""
    began = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    site = await _seed_site(counting_since=began)
    cf = _FakeAnalyticsCF(totals={"pageviews": 0, "visitors": 0})

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "ok"
    assert out.pageviews == 0
    assert out.visitors == 0
    assert out.counting_since == began.isoformat()
    assert cf.queries, "state 3 is the one that actually queries"


@pytest.mark.asyncio
async def test_a_failed_read_raises_rather_than_reporting_a_quiet_week(beanie_test_db):
    """State 4, and the reason it is NOT a status value. A Cloudflare outage arriving on
    the same shape as a report would be read by any client as zero traffic — silently,
    and exactly when the customer is least able to tell.

    The status vocabulary is asserted as CLOSED here too: were a failure ever added to
    it, this test is what fails."""
    site = await _seed_site(counting_since=datetime.now(UTC))
    cf = _ExplodingCF()

    with pytest.raises(CloudError) as exc:
        await sites_service.site_analytics(
            workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
        )

    assert exc.value.code == "sites.cloudflare_error"
    assert "not_entitled" not in str(exc.value)
    from pocketpaw_ee.sites import dto

    assert {
        dto.ANALYTICS_STATUS_OK,
        dto.ANALYTICS_STATUS_NOT_ENTITLED,
        dto.ANALYTICS_STATUS_NEVER_COUNTED,
    } == {"ok", "not_entitled", "never_counted"}


@pytest.mark.asyncio
async def test_the_four_outcomes_are_actually_distinguishable(beanie_test_db):
    """The claim the four tests above make INDIVIDUALLY, asserted as a set. Each one
    alone would pass against an implementation that answered the same thing for two of
    them, and "your plan does not include this" collapsing into "nobody visited" is the
    exact failure this slice exists to prevent."""
    began = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    unentitled = await _seed_site(pocket_id="pk-d1", plan_tier="free", counting_since=began)
    never = await _seed_site(pocket_id="pk-d2", counting_since=None)
    quiet = await _seed_site(pocket_id="pk-d3", counting_since=began)
    # A FOURTH site for the outage, not a re-read of ``quiet``. A site read successfully
    # a moment ago is cached, so a Cloudflare failure inside the TTL is correctly served
    # from the cache and never reaches the client — real behaviour, and it would make
    # this assertion pass or fail on cache timing rather than on the status vocabulary.
    broken = await _seed_site(pocket_id="pk-d4", counting_since=began)

    async def _read(site, cf):
        return await sites_service.site_analytics(
            workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
        )

    a = await _read(unentitled, _FakeAnalyticsCF())
    b = await _read(never, _FakeAnalyticsCF())
    c = await _read(quiet, _FakeAnalyticsCF(totals={"pageviews": 0, "visitors": 0}))

    assert len({a.status, b.status, c.status}) == 3
    # And the fourth is not on the same axis at all.
    with pytest.raises(CloudError):
        await _read(broken, _ExplodingCF())


# ── 2. real aggregates ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_site_with_traffic_returns_its_aggregates(beanie_test_db):
    """The endpoint's actual job. Totals plus the three dimensions the stored row
    carries, in the order the query returned them (the SQL sorts by pageviews)."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(
        totals={"pageviews": 1280, "visitors": 431},
        rows_by_blob={
            "blob1": _rows(("/", 800, 300), ("/pricing", 480, 210)),
            "blob2": _rows(("news.ycombinator.com", 300, 260), ("", 700, 190)),
            "blob3": _rows(("US", 900, 320), ("DE", 380, 111)),
        },
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "ok"
    assert (out.pageviews, out.visitors) == (1280, 431)
    assert [(r.label, r.pageviews, r.visitors) for r in out.top_pages] == [
        ("/", 800, 300),
        ("/pricing", 480, 210),
    ]
    assert [r.label for r in out.countries] == ["US", "DE"]


@pytest.mark.asyncio
async def test_an_empty_referrer_is_reported_as_direct_and_never_dropped(beanie_test_db):
    """A blank referrer is a REAL answer — a direct visit or a same-site link, which the
    counting Worker deliberately writes as empty. Dropping the row would inflate every
    remaining referrer's share, which is how a chart lies without a single wrong number
    in it."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(
        totals={"pageviews": 10, "visitors": 8},
        rows_by_blob={"blob2": _rows(("", 7, 6), ("example.com", 3, 2))},
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert [(r.label, r.pageviews) for r in out.referrers] == [("(direct)", 7), ("example.com", 3)]


@pytest.mark.asyncio
async def test_a_64_bit_sum_arriving_as_a_string_is_still_a_number(beanie_test_db):
    """ClickHouse-shaped APIs serialise 64-bit aggregates as JSON STRINGS to survive
    JavaScript's integer limit, so ``SUM(_sample_interval)`` can arrive quoted. Read as
    text it would reach the wire as a string on a field typed ``int`` and 500 the
    endpoint for exactly the busiest sites."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(
        totals={"pageviews": "9007199254740993", "visitors": "12"},
        rows_by_blob={"blob1": [{"label": "/", "pageviews": "5", "visitors": "4"}]},
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.pageviews == 9007199254740993
    assert out.visitors == 12
    assert out.top_pages[0].pageviews == 5


# ── 3. the row shape SA-3 is changing under this reader ──────────────────────


@pytest.mark.asyncio
async def test_a_four_blob_row_reads_as_devices_unrecorded(beanie_test_db):
    """The row shape shipped by SA-1. There is no fifth blob, so Analytics Engine
    answers an empty string for it and no device is knowable. Reported as
    ``devices: None`` plus ``"devices"`` in ``unrecorded`` — a client renders "not
    recorded", where an empty list would render "no devices" and an omitted field is
    indistinguishable from a version skew."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(
        totals={"pageviews": 40, "visitors": 20},
        rows_by_blob={"blob1": _rows(("/", 40, 20)), "blob5": _rows(("", 40, 20))},
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "ok"
    assert out.devices is None
    assert out.unrecorded == ["devices"]
    assert out.pageviews == 40, "the rest of the panel is unaffected"


@pytest.mark.asyncio
async def test_a_five_blob_row_reads_its_device_class(beanie_test_db):
    """The shape SA-3 writes. The same reader, no version flag, and ``unrecorded`` goes
    empty on its own the moment real device rows exist."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(
        totals={"pageviews": 100, "visitors": 60},
        rows_by_blob={"blob5": _rows(("mobile", 70, 44), ("desktop", 30, 16))},
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert [(r.label, r.pageviews) for r in out.devices] == [("mobile", 70), ("desktop", 30)]
    assert out.unrecorded == []


@pytest.mark.asyncio
async def test_both_row_shapes_in_one_window_bucket_the_old_rows_as_unknown(beanie_test_db):
    """THE CASE THAT ACTUALLY HAPPENS on the day SA-3 deploys: a seven-day window spans
    the change, so four-blob and five-blob rows come back together. The old rows must be
    BUCKETED, not dropped — dropping them would rescale mobile and desktop against a
    smaller total and quietly overstate both."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(
        totals={"pageviews": 100, "visitors": 60},
        rows_by_blob={"blob5": _rows(("", 55, 30), ("mobile", 30, 20), ("desktop", 15, 10))},
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    labels = {r.label: r.pageviews for r in out.devices}
    assert labels == {"unknown": 55, "mobile": 30, "desktop": 15}
    assert sum(labels.values()) == out.pageviews
    assert out.unrecorded == []


# ── 4. tenancy, windows and the SQL ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_another_workspaces_site_is_a_404(beanie_test_db):
    """Matches every sibling per-site read. The tenancy check runs BEFORE the
    entitlement one, so a caller cannot learn that another workspace's site exists —
    let alone what it is paying — by watching which error comes back."""
    site = await _seed_site(workspace_id="ws-owner", counting_since=datetime.now(UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 5, "visitors": 5})

    with pytest.raises(NotFound):
        await sites_service.site_analytics(
            workspace_id="ws-intruder", site_id=str(site.id), _cloudflare=cf
        )
    assert cf.queries == []


@pytest.mark.asyncio
async def test_a_malformed_site_id_is_a_404_and_never_reaches_the_sql(beanie_test_db):
    """``_load`` guards the ObjectId cast, so a malformed id is "no such site" rather
    than a 500 — and, on this endpoint specifically, never becomes a fragment of a raw
    SQL statement."""
    cf = _FakeAnalyticsCF()
    with pytest.raises(NotFound):
        await sites_service.site_analytics(
            workspace_id="ws-read", site_id="' OR 1=1 --", _cloudflare=cf
        )
    assert cf.queries == []


@pytest.mark.asyncio
async def test_an_unknown_window_is_refused_before_any_query(beanie_test_db):
    """THE WINDOW IS A SQL CONTROL, not only input hygiene. The Analytics Engine
    endpoint takes raw text with no parameter binding, so a window that reached the
    statement would be an injection. It selects a row from a closed table instead, and
    anything else is a 422 raised before a client is even built."""
    site = await _seed_site(counting_since=datetime.now(UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 1, "visitors": 1})

    with pytest.raises(ValidationError):
        await sites_service.site_analytics(
            workspace_id="ws-read",
            site_id=str(site.id),
            window="7d' OR '1'='1",
            _cloudflare=cf,
        )
    assert cf.queries == []


@pytest.mark.asyncio
async def test_the_window_selects_the_interval_and_the_site_scopes_the_query(beanie_test_db):
    """Every statement is scoped to THIS site's index and to the requested window. The
    index filter is the tenancy boundary inside a dataset every site shares, so a
    missing one would serve another customer's numbers with no error anywhere."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 1, "visitors": 1})

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), window="30d", _cloudflare=cf
    )

    assert out.window == "30d"
    assert cf.queries
    for sql in cf.queries:
        assert f"index1 = '{site.id}'" in sql
        assert "INTERVAL '30' DAY" in sql
    # Sampling is accounted for. A plain COUNT() under-reports precisely the busiest
    # sites, which are the ones that would notice.
    assert "SUM(_sample_interval)" in cf.queries[0]


@pytest.mark.asyncio
async def test_the_default_window_is_seven_days(beanie_test_db):
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 1, "visitors": 1})

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.window == "7d"
    assert "INTERVAL '7' DAY" in cf.queries[0]
    assert out.retention_days == 90, "three months at Cloudflare, on the wire so a UI can say why"


@pytest.mark.asyncio
async def test_a_naive_stored_stamp_reaches_the_wire_as_utc(beanie_test_db):
    """Mongo stores a datetime with no zone, so a stamp written as tz-aware reads back
    NAIVE. On the wire bare it would be read as the client's local time — a silent
    several-hour lie about when counting began, on the exact value a chart's x-axis
    starts at."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, 9, 0))
    cf = _FakeAnalyticsCF(totals={"pageviews": 1, "visitors": 1})

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.counting_since is not None
    assert out.counting_since.endswith("+00:00")
    assert datetime.fromisoformat(out.counting_since) == datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


# ── 5. the cache ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_read_inside_the_window_costs_no_cloudflare_query(beanie_test_db):
    """Analytics Engine bills READ queries against a small daily allowance, and this
    panel's whole usage pattern is somebody reloading it. Asserted on the query log
    rather than on a timing, so it says what it means."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 12, "visitors": 9})

    first = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )
    sent = len(cf.queries)
    second = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert len(cf.queries) == sent, "the reload re-queried Cloudflare"
    assert second.pageviews == first.pageviews == 12


@pytest.mark.asyncio
async def test_each_window_is_cached_separately(beanie_test_db):
    """A cache keyed on the site alone would serve the 7-day numbers for a 90-day
    request — the same panel, a different question, and no error anywhere to notice
    it."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 3, "visitors": 3})

    week = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), window="7d", _cloudflare=cf
    )
    quarter = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), window="90d", _cloudflare=cf
    )

    assert (week.window, quarter.window) == ("7d", "90d")
    assert any("INTERVAL '90' DAY" in sql for sql in cf.queries)


@pytest.mark.asyncio
async def test_the_cache_never_answers_a_different_tenant(beanie_test_db):
    """TENANCY BEATS THE CACHE. Two workspaces cannot share a site id in practice, but a
    key that omitted the workspace would make that assumption load-bearing across a
    process that serves every tenant — and the tenancy check runs first regardless, so a
    cached entry can never short-circuit it."""
    site = await _seed_site(workspace_id="ws-owner", counting_since=datetime.now(UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 77, "visitors": 40})

    await sites_service.site_analytics(
        workspace_id="ws-owner", site_id=str(site.id), _cloudflare=cf
    )
    with pytest.raises(NotFound):
        await sites_service.site_analytics(
            workspace_id="ws-intruder", site_id=str(site.id), _cloudflare=cf
        )


@pytest.mark.asyncio
async def test_an_expired_entry_is_re_queried(beanie_test_db):
    """The TTL actually expires. Driven by rewriting the stored deadline rather than by
    sleeping — a real wait would add a minute to the suite to prove arithmetic."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 12, "visitors": 9})

    await sites_service.site_analytics(workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf)
    sent = len(cf.queries)
    key = ("ws-read", str(site.id), "7d")
    _, cached = sites_service._analytics_cache[key]
    sites_service._analytics_cache[key] = (0.0, cached)

    await sites_service.site_analytics(workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf)

    assert len(cf.queries) > sent


@pytest.mark.asyncio
async def test_a_failed_read_is_not_cached(beanie_test_db):
    """Caching a failure would turn a Cloudflare blip into a minute of errors for
    everyone who reloads, and would hold the panel dark after the outage ended."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))

    with pytest.raises(CloudError):
        await sites_service.site_analytics(
            workspace_id="ws-read", site_id=str(site.id), _cloudflare=_ExplodingCF()
        )

    healthy = _FakeAnalyticsCF(totals={"pageviews": 5, "visitors": 4})
    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=healthy
    )

    assert out.status == "ok"
    assert healthy.queries, "the recovery read must reach Cloudflare"


@pytest.mark.asyncio
async def test_the_cache_is_bounded(beanie_test_db):
    """A workspace with thousands of sites must not grow this without limit. The policy
    is deliberately dumb — sweep the expired, and clear if that was not enough — because
    an entry is worth a fraction of a cent and the next request simply re-queries."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cap = sites_service._ANALYTICS_CACHE_MAX_ENTRIES
    for i in range(cap + 5):
        sites_service._analytics_cache[("ws-x", f"site-{i}", "7d")] = (
            float("inf"),
            sites_service._analytics_empty(f"site-{i}", "7d", "ok", None),
        )

    await sites_service.site_analytics(
        workspace_id="ws-read",
        site_id=str(site.id),
        _cloudflare=_FakeAnalyticsCF(totals={"pageviews": 1, "visitors": 1}),
    )

    assert len(sites_service._analytics_cache) <= cap


# ── 6. over HTTP ─────────────────────────────────────────────────────────────


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Member of the test workspace — ``fabric.read`` is member-tier."""

    def __init__(self, workspace_id: str) -> None:
        self.id = "user-test-1"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id)]


def _build_app(workspace_id: str) -> FastAPI:
    """The sites router behind stubbed auth, mirroring test_router.py.

    No plan patch here — the tree's conftest already patches ``get_workspace_plan``, and
    a second patch on the same target unwinds in the wrong order and leaks a mock across
    test trees. See test_router.py's own note.
    """
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    fake_user = _FakeUser(workspace_id)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=str(fake_user.id),
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def client(beanie_test_db):
    app = _build_app("ws-http")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_the_endpoint_serves_the_aggregates(client, monkeypatch):
    """End to end through the route, which is where the response MODEL is enforced —
    the service tests above would pass against a DTO the wire cannot serialise."""
    site = await _seed_site(
        workspace_id="ws-http", counting_since=datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    )
    cf = _FakeAnalyticsCF(
        totals={"pageviews": 210, "visitors": 88},
        rows_by_blob={"blob1": _rows(("/", 210, 88))},
    )
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics?window=7d")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["pageviews"] == 210
    assert body["visitors"] == 88
    assert body["window"] == "7d"
    assert body["counting_since"].startswith("2026-08-01T09:00:00")
    assert body["top_pages"][0]["label"] == "/"
    assert body["devices"] is None
    assert body["unrecorded"] == ["devices"]


@pytest.mark.asyncio
async def test_the_endpoint_404s_a_cross_tenant_site(client):
    """Same as the sibling per-site endpoints. Asserted over HTTP because the mapping
    from ``NotFound`` to a 404 lives in the error handler, not in the service."""
    site = await _seed_site(workspace_id="ws-somebody-else", counting_since=datetime.now(UTC))

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics")

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_endpoint_reports_an_outage_as_an_error_not_as_zero(client, monkeypatch):
    """The end-to-end form of state 4, and the one that matters most on the wire. A
    5xx is unmissable; a 200 carrying zeros is indistinguishable from a quiet week to
    every client that will ever be written against this."""
    site = await _seed_site(workspace_id="ws-http", counting_since=datetime.now(UTC))
    monkeypatch.setattr(sites_service, "_cf_client", _ExplodingCF)

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics")

    assert resp.status_code >= 400, resp.text
    assert "pageviews" not in resp.json()


@pytest.mark.asyncio
async def test_the_endpoint_refuses_an_unknown_window(client):
    site = await _seed_site(workspace_id="ws-http", counting_since=datetime.now(UTC))

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics?window=all-time")

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_the_endpoint_states_the_plan_case_without_numbers(client):
    """What a free site's panel is built from. A 200 rather than a 402: the site exists
    and the answer is a real one — "not on this plan" — which the UI turns into an
    upgrade prompt rather than an error."""
    site = await _seed_site(
        workspace_id="ws-http", plan_tier="free", counting_since=datetime.now(UTC)
    )

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_entitled"
    assert body["pageviews"] is None
    assert body["top_pages"] is None


@pytest.mark.asyncio
async def test_the_endpoint_distinguishes_never_counted_from_a_quiet_week(client, monkeypatch):
    """The two states a customer is most likely to confuse, side by side over HTTP.
    Same status code, same shape, and the only thing separating "republish to start
    counting" from "nobody came" is the field this test reads."""
    never = await _seed_site(workspace_id="ws-http", pocket_id="pk-h1", counting_since=None)
    quiet = await _seed_site(
        workspace_id="ws-http",
        pocket_id="pk-h2",
        counting_since=datetime.now(UTC) - timedelta(days=30),
    )
    monkeypatch.setattr(
        sites_service,
        "_cf_client",
        lambda: _FakeAnalyticsCF(totals={"pageviews": 0, "visitors": 0}),
    )

    a = (await client.get(f"/api/v1/sites/{never.id}/analytics")).json()
    b = (await client.get(f"/api/v1/sites/{quiet.id}/analytics")).json()

    assert a["status"] == "never_counted"
    assert a["pageviews"] is None
    assert b["status"] == "ok"
    assert b["pageviews"] == 0


# ── 7. gaps the first mutation sweep found ───────────────────────────────────
#
# Each of these was written because a mutation ESCAPED — the code was already right
# and nothing would have noticed it going wrong. The failures they pin are all of one
# family: a guard that is load-bearing but whose absence changes nothing any OTHER test
# looks at.


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["totals", "breakdown"])
async def test_a_failure_on_ONE_query_still_fails_the_whole_read(beanie_test_db, failing):
    """A read is FIVE queries — totals plus four breakdowns — and a swallow on ANY ONE of
    them is the outage-as-quiet-week failure wearing a smaller hat. A panel whose totals
    are real and whose referrer chart is silently empty is worse than an error, because
    nothing on screen says a query failed.

    BOTH DIRECTIONS, because a client that fails on every query does not distinguish
    them and that is precisely how the first sweep's mutation escaped: a try/except
    around the totals call alone left every assertion in this file passing, since the
    breakdown loop went on raising. Each half here has to fail on its own."""

    class _FailsOne:
        def __init__(self, which: str) -> None:
            self.which = which
            self.queries: list[str] = []

        async def query_analytics_sql(self, sql: str) -> list[dict]:
            self.queries.append(sql)
            is_breakdown = "GROUP BY" in sql
            if (self.which == "breakdown") == is_breakdown:
                raise ValidationError("sites.cloudflare_error", "Analytics Engine SQL 500")
            return [] if is_breakdown else [{"pageviews": 500, "visitors": 200}]

    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))

    with pytest.raises(CloudError):
        await sites_service.site_analytics(
            workspace_id="ws-read", site_id=str(site.id), _cloudflare=_FailsOne(failing)
        )


@pytest.mark.asyncio
async def test_the_sql_builder_refuses_anything_that_is_not_a_site_id(beanie_test_db):
    """THE LAST GUARD IN FRONT OF RAW SQL, asserted directly on the builder.

    Every path that reaches it today goes through ``_load``, which round-trips the id
    through ``ObjectId`` — so removing this check breaks no end-to-end test, which is
    why the mutation escaped. It stays because the cost of the next caller not having
    done that round-trip is an injected query against the account's whole analytics
    dataset, and a guard nothing tests is a guard somebody deletes."""
    with pytest.raises(ValidationError):
        sites_service._analytics_sql(select="1", site_id="' OR 1=1 --", days=7)
    with pytest.raises(ValidationError):
        sites_service._analytics_sql(select="1", site_id="507F1F77BCF86CD799439011", days=7)
    with pytest.raises(ValidationError):
        sites_service._analytics_sql(select="1", site_id="507f1f77bcf86cd7994390", days=7)

    ok = sites_service._analytics_sql(select="1", site_id="507f1f77bcf86cd799439011", days=7)
    assert "507f1f77bcf86cd799439011" in ok


@pytest.mark.asyncio
async def test_the_endpoint_honours_a_window_other_than_the_default(client, monkeypatch):
    """The route must FORWARD the window, not merely accept it. A handler that dropped
    it would answer 200 with correct-looking numbers for the wrong period — and every
    service test would still pass, because they call the service directly.

    Asserted on the response AND on the SQL, so a route that echoed the parameter back
    without using it is caught too."""
    site = await _seed_site(workspace_id="ws-http", counting_since=datetime(2026, 6, 1, tzinfo=UTC))
    cf = _FakeAnalyticsCF(totals={"pageviews": 9, "visitors": 4})
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics?window=90d")

    assert resp.status_code == 200, resp.text
    assert resp.json()["window"] == "90d"
    assert cf.queries and all("INTERVAL '90' DAY" in sql for sql in cf.queries)


# ── 8. AD-2: visits, bounce rate and visit duration ──────────────────────────
#
# THE FIXTURES HERE ARE RAW PAGEVIEWS, NOT SCRIPTED AGGREGATES, and that is the whole
# design of the section. A fake that answers ``{"visits": 3, "bounces": 1}`` can only
# contain the cases whoever wrote the query already had in mind, and the case this
# feature is most likely to get wrong is one nobody types in by hand: a person reading
# two pages either side of the top of an hour, whom the hourly visit salt splits into TWO
# visits. A fixture built to match the query leaves that out and then certifies the split
# as absent — the same trap as a contract test whose one payload is the shape where a
# wrong expression gives the right answer.
#
# So ``_RawRowsCF`` models what ANALYTICS ENGINE does with the statement: group the rows
# by the stored visit id, sum the sampling intervals, take the span from a visit's first
# pageview to its last. The visit id itself comes from ``_pv``, which derives it from the
# HOUR the pageview happened in the way the generated Worker does, so the split is a
# property of the data rather than of the assertion. Bounce rate and mean duration are
# the service's own arithmetic and every test below asserts them against numbers worked
# out by hand.


def _pv(when: datetime, visitor: str, *, sample: int = 1, visit: str | None = None) -> dict:
    """One stored pageview, as the counting Worker would have written it.

    ``visit`` defaults to the id AD-1 stamps: the visitor under a salt that rotates on the
    hour, so two pageviews by one person inside one hour share it and two either side of
    the hour do not. Pass ``""`` for a row written BEFORE AD-1, which is the only way a
    stored row carries no visit id at all."""
    return {
        "when": when,
        "visitor": visitor,
        "sample": sample,
        "visit": f"{visitor}@{when:%Y-%m-%dT%H}" if visit is None else visit,
    }


class _RawRowsCF:
    """An Analytics Engine that AGGREGATES a fixture of raw pageviews.

    It reads the two parts of the statement that change the answer — which blob the
    visits are grouped by, and whether rows carrying no visit id are filtered out — so a
    query that grouped by the device class, or that stopped excluding the unrecorded
    rows, gets a different answer here instead of the same one. What the statement does
    that no fake can check on the reader's behalf (the sampling sum, the bounce
    condition, the duration's own filter) is asserted as TEXT in
    ``test_the_visit_statement_is_the_one_cloudflare_will_run``, because Cloudflare is the
    only thing that can execute this SQL and the statement is therefore the artifact."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def query_analytics_sql(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        if "blob6 AS visit" in sql:
            return [self._visits(drop_unrecorded="blob6 != ''" in sql)]
        if "GROUP BY" in sql:
            return []
        return [
            {
                "pageviews": sum(row["sample"] for row in self.rows),
                "visitors": len({row["visitor"] for row in self.rows}),
            }
        ]

    def _visits(self, *, drop_unrecorded: bool) -> dict:
        """The outer aggregate, computed the way the two-level statement computes it."""
        grouped: dict[str, list[dict]] = {}
        for row in self.rows:
            if drop_unrecorded and row["visit"] == "":
                continue
            grouped.setdefault(row["visit"], []).append(row)
        visits = bounces = total_seconds = 0
        for group in grouped.values():
            weight = max(row["sample"] for row in group)
            pageviews = sum(row["sample"] for row in group)
            visits += weight
            if pageviews == 1:
                bounces += weight
            else:
                span = max(row["when"] for row in group) - min(row["when"] for row in group)
                total_seconds += int(span.total_seconds()) * weight
        return {"visits": visits, "bounces": bounces, "total_seconds": total_seconds}


@pytest.mark.asyncio
async def test_a_visit_that_crosses_the_top_of_the_hour_counts_as_two(beanie_test_db):
    """THE ACCEPTED ERROR, asserted so that it stays accepted rather than becoming a
    surprise. One person, three pageviews, never more than eight minutes apart — one
    reading session to any human looking at the log. The visit id rotates on the hour, so
    the two before 11:00 are one visit and the one after is another, and the endpoint
    answers two. It over-splits and never over-merges, the direction the daily visitor
    salt already accepts.

    Driven from raw pageviews for exactly this reason: a fixture that scripted the
    aggregate would say two because its author typed two."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF(
        [
            _pv(datetime(2026, 9, 3, 10, 50, tzinfo=UTC), "reader"),
            _pv(datetime(2026, 9, 3, 10, 58, tzinfo=UTC), "reader"),
            _pv(datetime(2026, 9, 3, 11, 3, tzinfo=UTC), "reader"),
        ]
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.pageviews == 3
    assert out.visitors == 1, "one person, whatever the visit id was split into"
    assert out.visits.count == 2, "the hourly salt cut one reading session in two"
    # The first visit ran 10:50 to 10:58. The second is a single pageview and therefore a
    # bounce, which is what the tail of a split visit always looks like.
    assert out.visits.bounce_rate == 0.5
    assert out.visits.duration_seconds == 480.0


@pytest.mark.asyncio
async def test_a_one_page_visit_is_a_bounce_and_never_dilutes_the_duration(beanie_test_db):
    """A visit of one pageview has no measurable duration — its single row carries a
    single timestamp — so it counts as a bounce and stays OUT of the duration's
    denominator. The mean here is 300 over the one visit that has a duration, not 300 over
    both visits: dividing by visits would make a site's average time on page fall every
    time somebody read one page and left, which is backwards."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF(
        [
            _pv(datetime(2026, 9, 3, 10, 0, tzinfo=UTC), "deep"),
            _pv(datetime(2026, 9, 3, 10, 2, tzinfo=UTC), "deep"),
            _pv(datetime(2026, 9, 3, 10, 5, tzinfo=UTC), "deep"),
            _pv(datetime(2026, 9, 3, 10, 1, tzinfo=UTC), "quick"),
        ]
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert (out.pageviews, out.visitors) == (4, 2)
    assert out.visits.count == 2
    assert out.visits.bounce_rate == 0.5
    assert out.visits.duration_seconds == 300.0, "300 / 1 measurable visit, not 300 / 2"


@pytest.mark.asyncio
async def test_every_visit_a_bounce_leaves_the_duration_unmeasurable(beanie_test_db):
    """Three people, one page each. The bounce rate is a real 100% and the duration is
    genuinely unknown — every visit carries one timestamp, so there is nothing to measure.
    Reporting 0 seconds would claim everyone left instantly, which is a statement about
    the site that the stored rows cannot support."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF(
        [
            _pv(datetime(2026, 9, 3, 10, 0, tzinfo=UTC), "a"),
            _pv(datetime(2026, 9, 3, 10, 10, tzinfo=UTC), "b"),
            _pv(datetime(2026, 9, 3, 10, 20, tzinfo=UTC), "c"),
        ]
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.visits.count == 3
    assert out.visits.bounce_rate == 1.0
    assert out.visits.duration_seconds is None


@pytest.mark.asyncio
async def test_a_window_written_before_the_visit_id_says_republish_not_zero(beanie_test_db):
    """THE FOURTH EMPTY STATE, and the reason this slice needed one. The site is counting
    and the pageviews are real; every row was written by a publish that predates the visit
    id, so not one of them can be attributed to a visit.

    A zero bounce rate here would be the worst invented zero on this endpoint. The other
    three empty states look empty; 0% bounce looks like a triumph — and it would be
    printed for every site that has not republished since the day this shipped."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF(
        [
            _pv(datetime(2026, 9, 3, 10, 0, tzinfo=UTC), "old", visit=""),
            _pv(datetime(2026, 9, 3, 10, 4, tzinfo=UTC), "old", visit=""),
            _pv(datetime(2026, 9, 3, 11, 0, tzinfo=UTC), "older", visit=""),
        ]
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "ok", "the read succeeded; only one part of it is unanswerable"
    assert out.pageviews == 3, "the rest of the panel is unaffected"
    assert out.visits is None
    assert "visits" in out.unrecorded


@pytest.mark.asyncio
async def test_a_quiet_window_reports_zero_visits_rather_than_calling_them_unrecorded(
    beanie_test_db,
):
    """The state next door, which must not collapse into the one above. Nothing was
    recorded because nobody came, not because the rows cannot answer — so the block is
    served with a real zero, and only the two RATIOS are None, because a bounce rate over
    zero visits is undefined rather than zero."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF([])

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "ok"
    assert out.pageviews == 0
    assert out.visits is not None, "a quiet week is not a version skew"
    assert out.visits.count == 0
    assert out.visits.bounce_rate is None
    assert out.visits.duration_seconds is None
    assert "visits" not in out.unrecorded


@pytest.mark.asyncio
async def test_a_window_spanning_the_republish_measures_the_rows_that_can_be(beanie_test_db):
    """THE CASE THAT ACTUALLY HAPPENS on the day a site republishes: old rows with no
    visit id and new rows with one, inside a single window. The measurable ones are
    reported and the block does NOT go unrecorded — refusing it while any old row survives
    would black the panel out for up to ninety days after the upgrade that fixed it.

    The cost is that visits under-state the window until it has rolled past the publish,
    which is the shape ``counting_since`` already has and is transient by the window's own
    length."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF(
        [
            _pv(datetime(2026, 9, 3, 9, 0, tzinfo=UTC), "before", visit=""),
            _pv(datetime(2026, 9, 3, 9, 30, tzinfo=UTC), "before", visit=""),
            _pv(datetime(2026, 9, 3, 12, 0, tzinfo=UTC), "after"),
            _pv(datetime(2026, 9, 3, 12, 4, tzinfo=UTC), "after"),
        ]
    )

    out = await sites_service.site_analytics(
        workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf
    )

    assert out.pageviews == 4, "every row still counts as a pageview"
    assert out.visits.count == 1, "only the rows that carry a visit id can be a visit"
    assert out.visits.bounce_rate == 0.0
    assert out.visits.duration_seconds == 240.0
    assert "visits" not in out.unrecorded


@pytest.mark.asyncio
async def test_the_visit_statement_is_the_one_cloudflare_will_run(beanie_test_db):
    """The statement is the artifact, so it is asserted as text.

    Nothing in this suite executes SQL — Cloudflare is the only thing that can — and a
    fake will happily answer a query that reads the wrong column, aggregates the wrong
    way, or filters nothing. The sampling rule is already pinned this way for the totals
    query above; these are the same assertions for a statement that has two levels and
    four expressions to get wrong.

    ``blob6`` rather than ``blob5`` is the one that raises nothing anywhere: the device
    class sits in the slot next door, and grouping by it would report the number of device
    classes as the number of visits and look entirely plausible on a chart."""
    site = await _seed_site(counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF([_pv(datetime(2026, 9, 3, 10, 0, tzinfo=UTC), "one")])

    await sites_service.site_analytics(workspace_id="ws-read", site_id=str(site.id), _cloudflare=cf)

    visit_sql = next(sql for sql in cf.queries if "AS visits" in sql)
    assert "blob6 AS visit" in visit_sql, "blob5 is the device class"
    assert "SUM(_sample_interval) AS pageviews" in visit_sql
    assert "SUM(sample_interval) AS visits" in visit_sql
    assert "COUNT(" not in visit_sql, "a plain count under-reports a downsampled index"
    assert "sumIf(sample_interval, pageviews = 1) AS bounces" in visit_sql
    assert "sumIf(seconds * sample_interval, pageviews > 1) AS total_seconds" in visit_sql
    assert "blob6 != ''" in visit_sql, "rows with no visit id would merge into one visit"
    assert "GROUP BY visit" in visit_sql
    assert visit_sql.count("SELECT") == 2, "the subquery form is what makes this reachable"
    # The site and the window still scope it, because both live on the inner half.
    assert f"index1 = '{site.id}'" in visit_sql
    assert "INTERVAL '7' DAY" in visit_sql
    # Neither is supported by the SQL API, and both are the shapes a reader reaches for
    # first when a query needs two levels.
    assert "WITH " not in visit_sql
    assert "JOIN" not in visit_sql


@pytest.mark.asyncio
async def test_the_endpoint_serves_the_visit_metrics(client, monkeypatch):
    """End to end, which is where the nested block's serialisation is enforced. The
    service tests above would pass against a model the wire cannot render."""
    site = await _seed_site(
        workspace_id="ws-http", counting_since=datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    )
    cf = _RawRowsCF(
        [
            _pv(datetime(2026, 9, 3, 14, 0, tzinfo=UTC), "reader"),
            _pv(datetime(2026, 9, 3, 14, 30, tzinfo=UTC), "reader"),
            _pv(datetime(2026, 9, 3, 14, 5, tzinfo=UTC), "passer-by"),
        ]
    )
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["visits"]["count"] == 2
    assert body["visits"]["bounce_rate"] == 0.5
    assert body["visits"]["duration_seconds"] == 1800.0
    assert "visits" not in body["unrecorded"]


@pytest.mark.asyncio
async def test_the_endpoint_says_republish_rather_than_zero_bounce(client, monkeypatch):
    """The fourth empty state on the wire, where a client will read it. A null block plus
    a named reason is a message; a zeroed one is a claim about the site's traffic that
    nothing recorded."""
    site = await _seed_site(workspace_id="ws-http", counting_since=datetime(2026, 8, 1, tzinfo=UTC))
    cf = _RawRowsCF([_pv(datetime(2026, 9, 3, 10, 0, tzinfo=UTC), "old", visit="")])
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)

    resp = await client.get(f"/api/v1/sites/{site.id}/analytics")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pageviews"] == 1
    assert body["visits"] is None
    assert "visits" in body["unrecorded"]
