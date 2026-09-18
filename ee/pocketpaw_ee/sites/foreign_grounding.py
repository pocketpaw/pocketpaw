# ee/pocketpaw_ee/sites/foreign_grounding.py — the FOREIGN lane behind
# ``kb_ingest``: a site PocketPaw does not host, harvested from its own live
# origin into the html lane's source map.
#
# WHY IT IS SEPARATE FROM kb_ingest. The other three lanes read the POCKET, which
# is durable and needs no network, and that is what makes them free of any SSRF
# surface. A foreign concierge (``sites.service.mint_foreign_site``) embeds on a
# page we never rendered, so its pocket holds no pages and those lanes have
# nothing to read. For this one lane the source of truth is the customer's live
# origin. Keeping it in its own module keeps the pocket-is-the-truth rule true
# where it still holds, and puts every line that treats a customer hostname as a
# content source in one file.
#
# NO EXTRACTOR LIVES HERE. The crawler already emits exactly the shape the html
# lane consumes (a path → HTML map), so this module hands that map to
# ``kb_ingest.extract_site_documents(engine="html", ...)`` and stops. A reader who
# finds themselves parsing markup in this file has duplicated the lane.
#
# WHEN IT RUNS, AND THE PROPERTY THAT DEPENDS ON IT. At bind (the concierge
# provisioning trigger) and on the owner's explicit re-sync. NEVER during a
# visitor's turn: a concierge run reads ``pocket:<pocket_id>`` out of the KB and
# performs no outbound fetch, so a visitor cannot make us request a
# customer-controlled hostname — nor time one to probe the network we sit in.
# ``kb_ingest.sync_site_knowledge`` is this module's only caller; keep it so.
#
# THE GATES, ALL FAIL-CLOSED:
#   * A host must carry a VERIFIED ownership claim for THIS workspace
#     (``ownership.verified_origin_record``). Without it the feature is a
#     crawler-for-hire.
#   * The proof must be FRESH (``VERIFICATION_MAX_AGE``). A domain changes hands;
#     a proof is a fact with a date, not a licence.
#   * Exactly ONE origin is crawled. Article ids key on PATH, not host, so
#     crawling both apex and www would have the second write over the first.
#   * Fetching goes through ``url_crawler`` → ``safe_fetch`` and nowhere else.
#     Do not add an httpx call to this file.
#   * Every failure is REPORTED as a status code, never swallowed: a concierge
#     that silently knows nothing is the bug this lane exists to end.

"""Harvest a verified foreign origin into the html lane's source map."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError

logger = logging.getLogger(__name__)

# HOW LONG A PROOF OF CONTROL STAYS GOOD FOR CRAWLING.
#
# 30 days, and the number is argued rather than picked:
#   * A domain that lapses is not available to a stranger for at least ~35 days
#     (registrar auto-renew grace, then the 30-day redemption period, then
#     pending-delete). A window inside that floor means our proof can never still
#     be fresh at the moment a new registrant could first take the name.
#   * It is the billing cadence. A foreign concierge is charged monthly, so an
#     owner who must re-prove control once a window is being asked for one click
#     per invoice — not one per week, which they would resent, and not one per
#     year, which is a proof in name only.
#   * It costs an owner nothing in the common case. The crawl runs at bind, which
#     is minutes after verification, so the window only ever bites a re-sync
#     months later — and a stale proof does not delete what the concierge already
#     knows, it declines to refresh it and says why.
VERIFICATION_MAX_AGE = timedelta(days=30)

# THE CRAWLER OWNS THIS LANE'S CEILING, AND THIS IS WHERE IT IS SET.
#
# Two cap systems sit in series here: ``url_crawler`` at 50 pages / 200 assets
# (tuned for a one-shot site IMPORT) and ``kb_ingest`` at 60 documents / 40k
# chars per page. Measured against a 15-page, 10-asset small-business site — home,
# about, services, menu, pricing, contact, team, faq, hours, privacy, terms, a
# journal index and three posts (``test_foreign_grounding``, which asserts these
# numbers so they cannot rot):
#
#   * documents produced: 15 of kb_ingest's 60 cap
#   * largest page: 2,645 chars of 40,000 — 7% of the per-page cap
#   * under IMPORT defaults: 25 requests, 514,841 bytes, of which 9 assets and
#     462,810 bytes (90% of the traffic) are discarded, because the html lane
#     keeps only .html/.htm
#   * under this lane's budget: 16 requests, 52,031 bytes, same 15 documents
#
# So kb_ingest's caps cannot bind in this lane — the crawler's page budget always
# binds first — and inheriting both would leave a limit that reads like it is
# doing work when it is not. The crawler's budget is therefore the ONE ceiling,
# and it is named here rather than defaulted, because the expensive resource is
# not bytes: it is that every page becomes a kb-go article compilation, and every
# asset becomes a request against a customer's server whose answer we discard.
#
# 25 pages: about double the measured site's entire navigation. Past that a site
# is a catalogue or a blog whose long tail dilutes BM25 more than it answers
# anything. Truncation is reported on the crawl warnings, not silent.
# 0 assets: ``fetch_assets=False`` — the 90% above, not fetched at all.
# 8 MB: 25 pages at ~320 KB of markup each, an order of magnitude past the
# measured 3.5 KB/page. Crossing it aborts and reports rather than half-filling a
# scope.
GROUNDING_MAX_PAGES = 25
GROUNDING_BYTE_CAP = 8 * 1024 * 1024

# AN HONEST, SEPARATE IDENTITY. Not the importer's: this is a recurring read of
# the customer's own pages on their own behalf, not a one-shot import, and it is
# the string their robots.txt groups and WAF rules will match on. A distinct
# token also lets an operator allow one activity and refuse the other, which one
# shared name would not. ``crawl_site`` evaluates robots for the UA it announces,
# so this string is both what we send and what we are judged by.
GROUNDING_USER_AGENT = (
    "PawSitesConcierge/1.0 (+https://pocketpaw.dev; site-knowledge crawler for the "
    "site's own concierge)"
)

# The html lane's page test (``kb_ingest._is_page``). Applied here too so a
# non-HTML body that landed in the FileMap can never reach the ingest as a page.
_PAGE_SUFFIXES = (".html", ".htm")


@dataclass
class ForeignHarvest:
    """One attempt at reading a foreign origin. ``error`` empty means it worked."""

    host: str = ""
    # Path → HTML text, shaped for ``extract_site_documents(engine="html")``.
    source: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    pages_failed: int = 0
    skipped_by_robots: int = 0
    error: str = ""

    @property
    def complete(self) -> bool:
        """Did this harvest see the whole site it was allowed to see?

        A page that FAILED (fetch error, non-200) makes it incomplete: the miss is
        transient and non-deterministic, so the harvest is not a trustworthy
        answer to "what pages does this site have" and must not be used to decide
        that an article has disappeared.

        A page skipped by ROBOTS does not. That skip is the operator's standing
        instruction, identical on every sync, so a harvest that honours it is
        complete — and a path the owner has told us not to read is a path their
        concierge should stop quoting.
        """
        return not self.error and self.pages_failed == 0


def verification_age(record: Any, *, now: datetime | None = None) -> timedelta | None:
    """How long ago ``record`` proved control, or None when it carries no date.

    Mongo hands naive datetimes back, so a stamp without a tzinfo is read as UTC
    (which is what ``ownership._record_verified`` wrote).
    """
    stamp = getattr(record, "verified_at", None)
    if not isinstance(stamp, datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - stamp


def verification_is_fresh(record: Any, *, now: datetime | None = None) -> bool:
    """Is ``record``'s proof of control recent enough to crawl on?

    FAILS CLOSED on a verified row with no ``verified_at``. Such a row predates
    the field or was written by something that did not stamp it, and "verified at
    an unknown time" is exactly the claim this policy refuses to act on.
    """
    age = verification_age(record, now=now)
    if age is None:
        return False
    return age <= VERIFICATION_MAX_AGE


async def crawlable_origin(site: Any, *, now: datetime | None = None) -> tuple[str, str]:
    """The one host this site may be crawled on, or ("", reason).

    ONE host, not all of ``allowed_origins``. That list is the set of origins the
    EMBED is valid from — apex, www, a staging name — which for a real customer
    are spellings of one site. Article ids are derived from a page's PATH
    (``kb_ingest._path_slug``), so crawling two spellings would have the second
    overwrite the first article for article, paying twice for one site's content.

    The first host with a fresh verified claim wins. A host with no claim, or a
    stale one, is passed over rather than raising — the distinction between "none
    of these are proved" and "one was proved, a while ago" is what the caller
    reports, and the two need different sentences from the owner.
    """
    from pocketpaw_ee.sites import ownership

    hosts = [
        host.strip().lower()
        for host in (getattr(site, "allowed_origins", None) or [])
        if isinstance(host, str) and host.strip()
    ]
    if not hosts:
        return "", "origin_missing"
    workspace_id = str(getattr(site, "workspace", "") or "")
    saw_verified = False
    for host in hosts:
        record = await ownership.verified_origin_record(workspace_id, host)
        if record is None:
            continue
        saw_verified = True
        if verification_is_fresh(record, now=now):
            return host, ""
    return "", ("origin_verification_stale" if saw_verified else "origin_unverified")


def _crawl_error_code(code: str) -> str:
    """The crawler's import-vocabulary failure → this lane's report status."""
    if code == "sites.import_crawl_blocked_by_robots":
        # The owner's OWN robots.txt refuses us, which is unguessable from their
        # side unless we name it. It has its own code for exactly that reason.
        return "crawl_blocked_by_robots"
    if code == "sites.import_crawl_budget_exceeded":
        return "crawl_too_large"
    return "crawl_failed"


async def harvest_foreign_site(
    site: Any,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    politeness_delay: float | None = None,
    now: datetime | None = None,
) -> ForeignHarvest:
    """Crawl this foreign site's verified origin into an html-lane source map.

    Never raises. Every failure comes back as a ``ForeignHarvest`` carrying an
    ``error`` code, because the caller's job is to record WHY a concierge has no
    knowledge — a raised exception there becomes a generic "sync failed" and the
    owner is left guessing.

    ``transport`` / ``resolver`` / ``politeness_delay`` are the crawler's test
    seams, threaded so the suite never opens a socket.
    """
    from pocketpaw_ee.sites import url_crawler

    host, reason = await crawlable_origin(site, now=now)
    if reason:
        logger.info("sites.grounding: not crawling site %s (%s)", getattr(site, "id", "?"), reason)
        return ForeignHarvest(error=reason)

    try:
        crawl = await url_crawler.crawl_site(
            f"https://{host}/",
            total_byte_cap=GROUNDING_BYTE_CAP,
            transport=transport,
            resolver=resolver,
            politeness_delay=politeness_delay,
            max_pages=GROUNDING_MAX_PAGES,
            fetch_assets=False,
            user_agent=GROUNDING_USER_AGENT,
        )
    except url_crawler.CrawlError as exc:
        logger.info("sites.grounding: crawl of %s failed (%s)", host, exc.code)
        return ForeignHarvest(host=host, error=_crawl_error_code(exc.code))
    except ValidationError as exc:
        # The origin was REFUSED before any socket — bad shape, or it resolves
        # somewhere non-public. No request was issued.
        logger.info("sites.grounding: origin %s refused (%s)", host, exc.code)
        return ForeignHarvest(host=host, error="origin_unfetchable")
    except httpx.HTTPError:
        logger.info("sites.grounding: transport failure crawling %s", host)
        return ForeignHarvest(host=host, error="crawl_failed")
    except Exception:  # noqa: BLE001 — an UNREPORTED crawl failure is the bug here
        logger.warning("sites.grounding: crawl of %s raised", host, exc_info=True)
        return ForeignHarvest(host=host, error="crawl_failed")

    source = {
        path: body.decode("utf-8", errors="replace")
        for path, body in crawl.files.items()
        if path.lower().endswith(_PAGE_SUFFIXES)
    }
    harvest = ForeignHarvest(
        host=host,
        source=source,
        warnings=list(crawl.warnings),
        pages_failed=crawl.stats.pages_failed,
        skipped_by_robots=crawl.stats.skipped_by_robots,
    )
    logger.info(
        "sites.grounding: harvested %d page(s) from %s for site %s "
        "(failed=%d robots_skipped=%d bytes=%d)",
        len(source),
        host,
        getattr(site, "id", "?"),
        crawl.stats.pages_failed,
        crawl.stats.skipped_by_robots,
        crawl.stats.bytes_fetched,
    )
    return harvest


__all__ = [
    "GROUNDING_BYTE_CAP",
    "GROUNDING_MAX_PAGES",
    "GROUNDING_USER_AGENT",
    "VERIFICATION_MAX_AGE",
    "ForeignHarvest",
    "crawlable_origin",
    "harvest_foreign_site",
    "verification_age",
    "verification_is_fresh",
]
