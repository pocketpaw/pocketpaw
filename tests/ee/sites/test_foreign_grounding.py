# tests/ee/sites/test_foreign_grounding.py — the FOREIGN grounding lane
# (ee.pocketpaw_ee.sites.foreign_grounding): a concierge on a site we do not host,
# grounded by crawling that site's own verified origin.
#
# This lane fetches a CUSTOMER-CONTROLLED HOSTNAME, so the gates are the point and
# every gate test asserts a MECHANISM, not a return value: an unverified or stale
# origin is proved un-crawled by ``seen == []`` — zero requests issued — because
# "it returned an error code" is also what a version that fetched first and
# refused afterwards would do.
#
# It also pins the two decisions this slice had to make, so a later reader finds
# the numbers rather than the opinion:
#   * THE CAP MEASUREMENT. A 15-page / 10-asset small-business site through both
#     cap systems in series, asserting that kb_ingest's 60-document and
#     40k-char caps cannot bind here and that 90% of the import path's bytes are
#     assets this lane discards. That is why the crawler's page budget is the one
#     ceiling and why this lane sets it explicitly.
#   * THE ROBOTS DECISION. Grounding honours robots.txt (it is autonomous
#     discovery of pages nobody pointed us at, unlike an ownership probe), under
#     its OWN user-agent token — so an operator can allow the concierge crawl and
#     refuse the importer, or the reverse. A blocked crawl is REPORTED with its
#     own status code, never left as a concierge that silently knows nothing.
#
# All network is mocked (httpx.MockTransport + a dict-backed resolver): nothing
# here opens a socket. Fixture IPs must be genuinely public — this Python treats
# the RFC 5737 documentation ranges as private, so 203.0.113.0/24 would fail for
# the wrong reason.
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.sites import foreign_grounding, kb_ingest

pytestmark = pytest.mark.asyncio

_WS = "ws-alpha"
_HOST = "customer.example"
_WWW = "www.customer.example"
_IP = "93.184.216.34"

_PUBLIC = {_HOST: [_IP], _WWW: ["93.184.216.35"]}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _FakeSite:
    """Only the fields the lane reads. A real foreign Site row carries far more."""

    def __init__(self, **ov: Any) -> None:
        self.id = "site-foreign-1"
        self.workspace = _WS
        self.pocket_id = "pocket-1"
        self.owner = "user-1"
        self.foreign_origin = True
        self.allowed_origins = [_HOST]
        self.kb_article_ids: list[str] = []
        self.kb_synced_at = None
        self.kb_sync_error = ""
        self.set_calls: list[dict] = []
        self.__dict__.update(ov)

    async def set(self, updates: dict) -> None:
        self.set_calls.append(dict(updates))
        for key, value in updates.items():
            setattr(self, key, value)


def _resolver(table: dict[str, list[str]] | None = None):
    async def resolve(host: str) -> list[str]:
        source = _PUBLIC if table is None else table
        if host not in source:
            raise OSError(f"no DNS for {host}")
        return source[host]

    return resolve


def _transport(routes: dict[str, httpx.Response], seen: list[httpx.Request]):
    """MockTransport routing on (Host header, path). The request is pinned to the
    validated IP, so the hostname only ever appears in the Host header."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.headers.get("host", ""), request.url.path)
        if key in routes:
            return routes[key]
        return routes.get(request.url.path, httpx.Response(404, content=b"not found"))

    return httpx.MockTransport(handler)


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())


def _robots(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/plain"}, content=body.encode())


_COPY = (
    "<p>We roast in small batches and open at 8am every weekday. A flat white "
    "is 320 rupees and the back room takes bookings for up to thirty.</p>"
)

_SITE_ROUTES = {
    "/": _html(
        "<html><head><title>Brew &amp; Co</title></head><body>"
        f'<h1>Brew &amp; Co</h1>{_COPY}<a href="/hours/">Hours</a></body></html>'
    ),
    "/hours/": _html(
        f"<html><head><title>Hours</title></head><body><h1>Opening hours</h1>{_COPY}</body></html>"
    ),
}


async def _claim(
    host: str = _HOST,
    *,
    workspace: str = _WS,
    verified_at: datetime | None = None,
    status: str = "verified",
) -> SiteOriginClaim:
    now = datetime.now(UTC)
    claim = SiteOriginClaim(
        workspace=workspace,
        host=host,
        token="pawverify-token",
        status=status,
        issued_at=now,
        expires_at=now + timedelta(days=7),
        issued_by="user-1",
        method="well-known",
        verified_at=now if verified_at is None else verified_at,
    )
    await claim.insert()
    return claim


async def _harvest(site: Any = None, *, routes=None, table=None, seen=None, now=None):
    return await foreign_grounding.harvest_foreign_site(
        site if site is not None else _FakeSite(),
        transport=_transport(
            _SITE_ROUTES if routes is None else routes, seen if seen is not None else []
        ),
        resolver=_resolver(table),
        politeness_delay=0,
        now=now,
    )


# --------------------------------------------------------------------------- #
# The happy path: a verified origin becomes the html lane's source map
# --------------------------------------------------------------------------- #


async def test_a_verified_origin_is_crawled_into_the_html_lanes_source_map(beanie_test_db):
    """The crawler already emits the shape the html lane consumes, so grounding
    adds no extractor: the harvest's source map goes straight into
    ``extract_site_documents(engine="html")``."""
    await _claim()

    harvest = await _harvest()

    assert harvest.error == ""
    assert harvest.host == _HOST
    assert sorted(harvest.source) == ["hours/index.html", "index.html"]
    docs = kb_ingest.extract_site_documents(engine="html", source=harvest.source)
    assert len(docs) == 2
    assert {d.source for d in docs} == {"site-home", "site-hours-index"}
    assert "We open at 8am" in docs[0].text or "open at 8am" in docs[0].text


async def test_the_harvest_of_a_clean_crawl_is_complete(beanie_test_db):
    await _claim()
    harvest = await _harvest()
    assert harvest.pages_failed == 0
    assert harvest.complete is True


# --------------------------------------------------------------------------- #
# The verification gate — asserted as ZERO REQUESTS, not as an error code
# --------------------------------------------------------------------------- #


async def test_an_unverified_origin_is_never_fetched(beanie_test_db):
    """No claim row at all. Without this gate the feature is a crawler-for-hire:
    anyone could name a third party's host and have its pages compiled into their
    own workspace's KB."""
    seen: list[httpx.Request] = []

    harvest = await _harvest(seen=seen)

    assert harvest.error == "origin_unverified"
    assert seen == []  # not one packet left the process
    assert harvest.source == {}


async def test_a_pending_claim_is_not_a_verified_origin(beanie_test_db):
    seen: list[httpx.Request] = []
    await _claim(status="pending", verified_at=None)

    harvest = await _harvest(seen=seen)

    assert harvest.error == "origin_unverified"
    assert seen == []


async def test_a_claim_verified_for_another_workspace_does_not_open_this_one(beanie_test_db):
    seen: list[httpx.Request] = []
    await _claim(workspace="ws-beta")

    harvest = await _harvest(seen=seen)

    assert harvest.error == "origin_unverified"
    assert seen == []


async def test_a_site_with_no_origin_reports_it_and_fetches_nothing(beanie_test_db):
    seen: list[httpx.Request] = []

    harvest = await _harvest(_FakeSite(allowed_origins=[]), seen=seen)

    assert harvest.error == "origin_missing"
    assert seen == []


# --------------------------------------------------------------------------- #
# Freshness — a proof is a fact with a date, not a licence
# --------------------------------------------------------------------------- #


async def test_a_stale_verification_is_never_fetched(beanie_test_db):
    """A domain changes hands. Past the window the proof is not acted on, and the
    status says WHICH problem it is so the owner is told to re-verify rather than
    shown a generic failure."""
    seen: list[httpx.Request] = []
    await _claim(
        verified_at=datetime.now(UTC) - (foreign_grounding.VERIFICATION_MAX_AGE + timedelta(days=1))
    )

    harvest = await _harvest(seen=seen)

    assert harvest.error == "origin_verification_stale"
    assert seen == []


async def test_a_verification_inside_the_window_still_crawls(beanie_test_db):
    await _claim(
        verified_at=datetime.now(UTC) - (foreign_grounding.VERIFICATION_MAX_AGE - timedelta(days=1))
    )

    harvest = await _harvest()

    assert harvest.error == ""
    assert harvest.source


async def test_a_verified_row_with_no_date_fails_closed(beanie_test_db):
    """ "Verified at an unknown time" is exactly the claim the policy refuses to
    act on — a row that predates the field must not read as permanently fresh."""
    seen: list[httpx.Request] = []
    await _claim(verified_at=None)
    row = await SiteOriginClaim.find_one(SiteOriginClaim.workspace == _WS)
    row.verified_at = None
    await row.save()

    harvest = await _harvest(seen=seen)

    assert harvest.error == "origin_verification_stale"
    assert seen == []


async def test_a_naive_verified_at_is_read_as_utc(beanie_test_db):
    """Mongo hands naive datetimes back; a naive stamp must not be compared as if
    it were in some other zone (or raise on the subtraction)."""

    class _Row:
        verified_at = datetime.now(UTC).replace(tzinfo=None)

    assert foreign_grounding.verification_is_fresh(_Row()) is True


# --------------------------------------------------------------------------- #
# One origin, not the whole allowlist
# --------------------------------------------------------------------------- #


async def test_only_one_verified_origin_is_crawled(beanie_test_db):
    """``allowed_origins`` is the set of origins the EMBED is valid from — apex and
    www are one site. Article ids key on PATH, so crawling both would have the
    second overwrite the first article for article and pay twice for one site."""
    await _claim(_HOST)
    await _claim(_WWW)
    seen: list[httpx.Request] = []

    harvest = await _harvest(_FakeSite(allowed_origins=[_HOST, _WWW]), seen=seen)

    assert harvest.host == _HOST
    assert {r.headers.get("host", "") for r in seen} == {_HOST}


async def test_the_first_fresh_origin_wins_when_an_earlier_one_is_stale(beanie_test_db):
    await _claim(_HOST, verified_at=datetime.now(UTC) - timedelta(days=400))
    await _claim(_WWW)

    harvest = await _harvest(_FakeSite(allowed_origins=[_HOST, _WWW]))

    assert harvest.error == ""
    assert harvest.host == _WWW


# --------------------------------------------------------------------------- #
# Robots — honoured, under this lane's OWN identity, and reported when it bites
# --------------------------------------------------------------------------- #


async def test_the_crawl_announces_the_concierge_identity(beanie_test_db):
    """Read off the request the transport saw, not off the constant. The string
    goes to a customer's server and their WAF and robots groups match on it, so a
    recurring grounding read must not wear the one-shot importer's name."""
    await _claim()
    seen: list[httpx.Request] = []

    await _harvest(seen=seen)

    agents = {r.headers.get("user-agent", "") for r in seen}
    assert agents == {foreign_grounding.GROUNDING_USER_AGENT}
    assert "PawSitesConcierge" in foreign_grounding.GROUNDING_USER_AGENT


async def test_robots_disallowing_the_concierge_blocks_the_crawl_and_says_so(beanie_test_db):
    """The failure mode this lane must never be silent about: the customer's own
    robots.txt is the cause and it is unguessable from their side unless named."""
    await _claim()
    routes = dict(_SITE_ROUTES)
    routes["/robots.txt"] = _robots("User-agent: PawSitesConcierge\nDisallow: /\n")

    harvest = await _harvest(routes=routes)

    assert harvest.error == "crawl_blocked_by_robots"
    assert harvest.source == {}


async def test_robots_disallowing_only_the_importer_does_not_block_grounding(beanie_test_db):
    """The two activities are separate and separately refusable. An operator who
    blocked the site-import crawler has not blocked the concierge they bought."""
    await _claim()
    routes = dict(_SITE_ROUTES)
    routes["/robots.txt"] = _robots("User-agent: PawSitesImporter\nDisallow: /\n")

    harvest = await _harvest(routes=routes)

    assert harvest.error == ""
    assert harvest.source


async def test_a_wildcard_robots_rule_still_applies(beanie_test_db):
    await _claim()
    routes = dict(_SITE_ROUTES)
    routes["/robots.txt"] = _robots("User-agent: *\nDisallow: /\n")

    harvest = await _harvest(routes=routes)

    assert harvest.error == "crawl_blocked_by_robots"


async def test_robots_blocking_an_inner_page_is_counted_but_leaves_the_harvest_complete(
    beanie_test_db,
):
    """A robots skip is the operator's standing instruction, identical on every
    sync — so the harvest is still a trustworthy answer to "what may we read",
    and a path they told us not to read should stop being quoted."""
    await _claim()
    routes = dict(_SITE_ROUTES)
    routes["/robots.txt"] = _robots("User-agent: *\nDisallow: /hours/\n")

    harvest = await _harvest(routes=routes)

    assert harvest.error == ""
    assert harvest.skipped_by_robots == 1
    assert list(harvest.source) == ["index.html"]
    assert harvest.complete is True


# --------------------------------------------------------------------------- #
# Failure reporting — never a half-populated scope, never a silent one
# --------------------------------------------------------------------------- #


async def test_an_unreachable_origin_reports_crawl_failed(beanie_test_db):
    await _claim()

    harvest = await _harvest(routes={"/": httpx.Response(503, content=b"down")})

    assert harvest.error == "crawl_failed"
    assert harvest.source == {}


async def test_an_origin_with_no_dns_reports_crawl_failed(beanie_test_db):
    await _claim()

    harvest = await _harvest(table={})

    assert harvest.error == "crawl_failed"


async def test_an_origin_that_resolves_private_is_refused_before_any_request(beanie_test_db):
    """The SSRF floor, inherited from safe_fetch: a verified NAME whose DNS answer
    is private is refused with nothing sent. Verification proves ownership, not
    that the target is a public address."""
    await _claim()
    seen: list[httpx.Request] = []

    harvest = await _harvest(table={_HOST: ["10.0.0.7"]}, seen=seen)

    assert harvest.error == "crawl_failed"
    assert seen == []


async def test_a_page_that_fails_mid_crawl_marks_the_harvest_incomplete(beanie_test_db):
    """The signal the sync uses to refuse to prune. A transient 502 on one page
    must not be read as "that page no longer exists"."""
    await _claim()
    routes = dict(_SITE_ROUTES)
    routes["/hours/"] = httpx.Response(502, content=b"bad gateway")

    harvest = await _harvest(routes=routes)

    assert harvest.error == ""
    assert harvest.pages_failed == 1
    assert harvest.complete is False
    assert list(harvest.source) == ["index.html"]


async def test_the_byte_budget_reports_crawl_too_large(beanie_test_db, monkeypatch):
    await _claim()
    monkeypatch.setattr(foreign_grounding, "GROUNDING_BYTE_CAP", 64)

    harvest = await _harvest()

    assert harvest.error == "crawl_too_large"
    assert harvest.source == {}


async def test_a_target_that_never_finishes_answering_is_abandoned_on_the_clock(
    beanie_test_db, monkeypatch
):
    """The slow-loris backstop. Every other budget counts something the target
    HANDS US — pages, bytes — so a target that hands us almost nothing escapes all
    of them: ``safe_fetch``'s ``httpx.Timeout(10.0)`` is per-socket-OPERATION and a
    server writing one byte every nine seconds resets it forever.

    The transport below never completes a request, which is that target with the
    trickle removed. Without the wall clock this call does not return, and this
    lane is awaited INSIDE a web request (the owner's re-sync handler), so "does
    not return" means a held request coroutine and a held connection.

    ``asyncio.wait_for`` is the test's OWN deadline, deliberately much longer than
    the lane's: delete the wrapper under test and this fails in three seconds
    instead of hanging CI until somebody kills it.
    """
    await _claim()
    monkeypatch.setattr(foreign_grounding, "GROUNDING_MAX_WALL_CLOCK_SEC", 0.3)

    class _NeverAnswers(httpx.AsyncBaseTransport):
        """Accepts the connection and then says nothing, forever.

        A MockTransport cannot express this — its handler is synchronous, so it
        must return a response. This one awaits a future nothing resolves, which
        is what a request against a trickling origin looks like from up here.
        """

        def __init__(self) -> None:
            self.seen: list[httpx.Request] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

    transport = _NeverAnswers()

    harvest = await asyncio.wait_for(
        foreign_grounding.harvest_foreign_site(
            _FakeSite(),
            transport=transport,
            resolver=_resolver(),
            politeness_delay=0,
        ),
        timeout=3,
    )

    # REPORTED, not raised. The sync's contract is a status code the dashboard can
    # turn into a sentence, and an escaping exception would surface as a generic
    # "sync failed" that says nothing about whose server stopped answering.
    assert harvest.error == "crawl_timeout"
    assert harvest.source == {}
    assert transport.seen, "the transport must have been reached, or the deadline proves nothing"


async def test_the_deadline_does_not_fire_on_a_site_that_answers(beanie_test_db):
    """The other half, and the reason the number is not 1.0. A deadline that also
    refuses honest sites is an outage, so the ordinary two-page crawl is asserted
    to finish well inside the budget rather than merely to finish."""
    await _claim()

    started = time.monotonic()
    harvest = await _harvest()
    elapsed = time.monotonic() - started

    assert harvest.error == ""
    assert elapsed < foreign_grounding.GROUNDING_MAX_WALL_CLOCK_SEC / 2, (
        "a normal crawl must land far inside the deadline, not just inside it"
    )


async def test_an_unexpected_crawler_failure_is_still_reported(beanie_test_db, monkeypatch):
    """A raised exception here would surface as a generic "sync failed" and leave
    the owner guessing, so even an unclassified failure comes back as a code."""
    await _claim()

    async def _boom(*a, **kw):
        raise RuntimeError("something new")

    from pocketpaw_ee.sites import url_crawler

    monkeypatch.setattr(url_crawler, "crawl_site", _boom)

    harvest = await _harvest()

    assert harvest.error == "crawl_failed"


# --------------------------------------------------------------------------- #
# The cap measurement — the finding that made the crawler the ceiling's owner
# --------------------------------------------------------------------------- #

_MEASURED_PAGES = [
    "/",
    "/about/",
    "/services/",
    "/menu/",
    "/pricing/",
    "/contact/",
    "/team/",
    "/faq/",
    "/hours/",
    "/privacy/",
    "/terms/",
    "/blog/",
    "/blog/first-post/",
    "/blog/second-post/",
    "/blog/third-post/",
]
_MEASURED_ASSETS = [
    "/style.css",
    "/print.css",
    "/main.js",
    "/analytics.js",
    "/logo.png",
    "/hero.jpg",
    "/latte.jpg",
    "/team.jpg",
    "/interior.jpg",
]


def _measured_site() -> dict[str, httpx.Response]:
    nav = "".join(f'<a href="{p}">{p}</a>' for p in _MEASURED_PAGES)
    imgs = "".join(
        f'<img src="{a}" alt="{a}">' for a in _MEASURED_ASSETS if a.endswith((".png", ".jpg"))
    )
    links = (
        '<link rel="stylesheet" href="/style.css">'
        '<link rel="stylesheet" href="/print.css">'
        '<script src="/main.js"></script>'
        '<script src="/analytics.js"></script>'
    )
    routes: dict[str, httpx.Response] = {"/robots.txt": httpx.Response(404, content=b"")}
    for path in _MEASURED_PAGES:
        routes[path] = _html(
            f"<html><head><title>{path}</title>{links}</head><body><nav>{nav}</nav>"
            f"{_COPY * 12}{imgs}</body></html>"
        )
    for path in _MEASURED_ASSETS:
        if path.endswith(".css"):
            routes[path] = httpx.Response(
                200, headers={"content-type": "text/css"}, content=b"body{color:#111}" * 200
            )
        elif path.endswith(".js"):
            routes[path] = httpx.Response(
                200,
                headers={"content-type": "application/javascript"},
                content=b"var a=1;" * 400,
            )
        else:
            routes[path] = httpx.Response(
                200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8" + b"\x00" * 90_000
            )
    return routes


async def test_a_realistic_small_business_site_never_reaches_the_kb_ingest_caps(beanie_test_db):
    """THE MEASUREMENT behind "the crawler owns the ceiling". 15 pages, 9 referenced
    assets. kb_ingest's 60-document cap and 40,000-char page cap are both an order
    of magnitude away, so neither can bind in this lane — the crawler's page
    budget always binds first, which is why this lane names it."""
    await _claim()

    harvest = await _harvest(routes=_measured_site())

    assert harvest.error == ""
    docs = kb_ingest.extract_site_documents(engine="html", source=harvest.source)
    assert len(docs) == 15
    assert len(docs) < kb_ingest._MAX_DOCUMENTS
    assert max(len(d.text) for d in docs) < kb_ingest._MAX_DOCUMENT_CHARS // 10


async def test_grounding_fetches_no_assets_and_that_is_most_of_the_traffic(beanie_test_db):
    """90% of the import path's bytes on the measured site are assets the html
    lane discards, because it keeps only .html/.htm. Grounding does not fetch
    them at all, and this is the number that made that the right call."""
    from pocketpaw_ee.sites import url_crawler

    await _claim()
    routes = _measured_site()

    import_seen: list[httpx.Request] = []
    imported = await url_crawler.crawl_site(
        f"https://{_HOST}/",
        total_byte_cap=foreign_grounding.GROUNDING_BYTE_CAP,
        transport=_transport(routes, import_seen),
        resolver=_resolver(),
        politeness_delay=0,
    )

    ground_seen: list[httpx.Request] = []
    harvest = await _harvest(routes=routes, seen=ground_seen)

    assert imported.stats.assets_fetched == 9
    assert harvest.pages_failed == 0
    # Same pages, and every extra request the import path made was an asset.
    assert len(harvest.source) == imported.stats.pages_fetched == 15
    assert len(ground_seen) == len(import_seen) - 9
    asset_bytes = imported.stats.bytes_fetched - sum(
        len(body) for path, body in imported.files.items() if path.endswith(".html")
    )
    assert asset_bytes / imported.stats.bytes_fetched > 0.85
    assert any("harvests pages only" in w for w in harvest.warnings)


# --------------------------------------------------------------------------- #
# The bind boundary: the FOREIGN row is what reaches the sync
# --------------------------------------------------------------------------- #


async def test_the_foreign_bind_hands_the_foreign_row_to_the_sync(monkeypatch):
    """A pocket can hold BOTH a published Worker site and a foreign concierge. The
    bind must pass the foreign row through, because the published one would take a
    pocket lane and sync nothing about the customer's real pages — so the object
    that reaches the sync is asserted to be this one, not merely to exist."""
    from pocketpaw_ee.paw_bar import agent_provisioning

    site = _FakeSite(name="Brew and Co", concierge_greeting="")
    widget = type("W", (), {"id": "w1", "agent_id": "", "spec": None})()
    agent = type("A", (), {"id": "agent-1"})()
    seen: list[Any] = []
    monkeypatch.setattr(agent_provisioning, "_schedule_knowledge_sync", seen.append)

    async def _site_widget(pocket_id, workspace_id):
        return widget

    async def _get_by_slug(workspace_id, slug):
        return agent

    class _Store:
        async def update_fields(self, widget_id, fields, *, workspace_id):
            widget.agent_id = fields.get("agent_id", "")
            return widget

        async def get_widget(self, widget_id, *, workspace_id):
            return widget

    monkeypatch.setattr(agent_provisioning, "site_widget", _site_widget)
    monkeypatch.setattr(agent_provisioning, "_store", lambda: _Store())
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.get_by_slug", _get_by_slug, raising=False
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.legacy_ctx", lambda *a, **k: object(), raising=False
    )

    bound = await agent_provisioning.provision_foreign_concierge(site, _WS)

    assert bound == "agent-1"
    assert seen == [site]
    assert seen[0].foreign_origin is True
