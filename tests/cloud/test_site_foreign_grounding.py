# tests/cloud/test_site_foreign_grounding.py — the FOREIGN lane end to end:
# ``kb_ingest.sync_site_knowledge`` on a site PocketPaw does not host.
#
# The lane's own gates and caps are covered in tests/ee/sites/test_foreign_grounding.py.
# This file covers what the SYNC does with a harvest, and the one property the
# whole design rests on:
#
#   * a bound foreign site syncs a NON-ZERO document count into
#     ``pocket:<pocket_id>``, and a concierge's own KB read returns that content —
#     the two halves are asserted separately, because an ingest into the wrong
#     scope would still look like a successful sync;
#   * THE VISITOR READ PATH FETCHES NOTHING. Asserted by running the read with
#     every outbound primitive wired to raise, so the claim is about the code path
#     rather than about how long a turn took;
#   * an unverified or stale origin syncs nothing, and a failed crawl reports the
#     failure instead of half-filling the scope — a partial crawl ingests what it
#     got but prunes nothing, because a page that 502'd is not a page that was
#     deleted;
#   * the three pocket-backed lanes are untouched: a hosted site still reads its
#     pocket and a foreign one never does.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.sites import foreign_grounding, kb_ingest
from pocketpaw_ee.sites.foreign_grounding import ForeignHarvest

_LONG = (
    "Brew and Co opens at 8am every weekday and 9am on Saturday. A flat white is "
    "320 rupees and the back room seats thirty."
)


class _FakeSite:
    def __init__(self, **ov: Any) -> None:
        self.id = "site-foreign-1"
        self.workspace = "ws-alpha"
        self.pocket_id = "pocket-1"
        self.owner = "user-1"
        self.foreign_origin = True
        self.allowed_origins = ["customer.example"]
        self.kb_article_ids: list[str] = []
        self.kb_synced_at = None
        self.kb_sync_error = ""
        self.set_calls: list[dict] = []
        self.__dict__.update(ov)

    async def set(self, updates: dict) -> None:
        self.set_calls.append(dict(updates))
        for key, value in updates.items():
            setattr(self, key, value)


def _page(title: str, body: str) -> str:
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1><p>{body}</p></body></html>"
    )


_HARVEST_SOURCE = {
    "index.html": _page("Brew and Co", _LONG),
    "hours/index.html": _page("Opening hours", _LONG),
}


def _patch_kb(monkeypatch) -> dict[str, Any]:
    """An in-memory stand-in for kb-go: records ingests per scope, and serves them
    back through the SAME read a concierge turn uses."""
    store: dict[str, dict[str, str]] = {}
    calls: dict[str, list] = {"ingest": [], "remove": []}

    async def _ingest(scope, text, source):
        calls["ingest"].append((scope, source, text))
        store.setdefault(scope, {})[source] = text
        return {"article": source}

    async def _remove(scope, article_id):
        calls["remove"].append((scope, article_id))
        store.get(scope, {}).pop(article_id, None)
        return True

    async def _search(scope, query, limit=3):
        hits = list(store.get(scope, {}).values())[:limit]
        return "\n".join(hits)

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.ingest_text_to_scope", _ingest
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.remove_article", _remove
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope", _search
    )
    calls["store"] = store  # type: ignore[assignment]
    return calls


def _patch_harvest(monkeypatch, harvest: ForeignHarvest) -> list[Any]:
    """Stand in for the crawl. The crawl itself is exercised for real (over a mock
    transport) in tests/ee/sites/test_foreign_grounding.py; here the harvest is the
    INPUT and the sync's handling of it is what is under test."""
    seen: list[Any] = []

    async def _harvest(site, **kw):
        seen.append(site)
        return harvest

    monkeypatch.setattr(foreign_grounding, "harvest_foreign_site", _harvest)
    return seen


def _forbid_pocket_read(monkeypatch) -> list[Any]:
    """The foreign lane must not read the pocket: a foreign pocket holds no pages,
    and reading it would make the lane's source of truth ambiguous."""
    reads: list[Any] = []

    async def _get(pocket_id, user_id):
        reads.append(pocket_id)
        raise AssertionError("the foreign lane must not read the pocket")

    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.service.get", _get)
    return reads


# --------------------------------------------------------------------------- #
# Criterion 1: a bound foreign site syncs non-zero, into the concierge's scope
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_foreign_site_syncs_a_non_zero_document_count(monkeypatch):
    calls = _patch_kb(monkeypatch)
    _patch_harvest(monkeypatch, ForeignHarvest(host="customer.example", source=_HARVEST_SOURCE))
    _forbid_pocket_read(monkeypatch)
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.ingested == 2
    assert report.error == ""
    assert {scope for scope, _, _ in calls["ingest"]} == {"pocket:pocket-1"}
    assert sorted(site.kb_article_ids) == ["site-home", "site-hours-index"]
    assert site.kb_synced_at is not None


@pytest.mark.asyncio
async def test_a_concierge_read_returns_the_crawled_content(monkeypatch):
    """The other half of criterion 1: the ingest landed in the ONE scope a public
    concierge run reads, so the crawled page actually reaches the model. An ingest
    into any other scope would still have reported a clean sync."""
    from pocketpaw_ee.cloud.chat import agent_service

    _patch_kb(monkeypatch)
    _patch_harvest(monkeypatch, ForeignHarvest(host="customer.example", source=_HARVEST_SOURCE))
    _forbid_pocket_read(monkeypatch)
    await kb_ingest.sync_site_knowledge(_FakeSite())

    ctx = agent_service.ScopeContext(
        kind=agent_service.ScopeKind.CONCIERGE,
        scope_id="session-1",
        workspace_id="ws-alpha",
        user_id="",
        members=[],
        target_agent_id="agent-concierge-1",
        pocket_id="pocket-1",
    )
    block = await agent_service._build_kb_snippets_block(ctx, "when do you open")

    assert "pocket:pocket-1" in block
    assert "opens at 8am" in block


@pytest.mark.asyncio
async def test_the_visitor_read_path_fetches_nothing(monkeypatch):
    """THE PROPERTY THE DESIGN RESTS ON. A concierge run must perform no
    server-side fetch of a customer-controlled hostname, so the crawl happens only
    at bind and on the owner's explicit re-sync.

    Asserted on the PATH, not on timing: every outbound primitive — the crawler,
    both safe_fetch entry points and httpx's own transport — is wired to raise, and
    the visitor's read still completes and still returns the content. If a fetch
    ever creeps onto this path the assertion fires from inside it.
    """
    import httpx
    from pocketpaw_ee.cloud.chat import agent_service
    from pocketpaw_ee.sites import safe_fetch, url_crawler

    _patch_kb(monkeypatch)
    harvested = _patch_harvest(
        monkeypatch, ForeignHarvest(host="customer.example", source=_HARVEST_SOURCE)
    )
    _forbid_pocket_read(monkeypatch)
    await kb_ingest.sync_site_knowledge(_FakeSite())
    assert len(harvested) == 1  # the SYNC crawled, once

    def _no_fetch(*a: Any, **kw: Any):
        raise AssertionError("the visitor read path must not fetch anything")

    monkeypatch.setattr(url_crawler, "crawl_site", _no_fetch)
    monkeypatch.setattr(safe_fetch, "fetch_single_url", _no_fetch)
    monkeypatch.setattr(safe_fetch.SafeFetcher, "fetch", _no_fetch)
    monkeypatch.setattr(foreign_grounding, "harvest_foreign_site", _no_fetch)
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_fetch)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _no_fetch)

    # The trap is ARMED, proved before it is relied on: a test whose patches were
    # no-ops would pass for the wrong reason and keep passing after a real fetch
    # was added somewhere the patches do not reach.
    with pytest.raises(AssertionError):
        await foreign_grounding.harvest_foreign_site(_FakeSite())

    ctx = agent_service.ScopeContext(
        kind=agent_service.ScopeKind.CONCIERGE,
        scope_id="session-1",
        workspace_id="ws-alpha",
        user_id="",
        members=[],
        target_agent_id="agent-concierge-1",
        pocket_id="pocket-1",
    )
    block = await agent_service._build_kb_snippets_block(ctx, "when do you open")

    assert "opens at 8am" in block
    assert len(harvested) == 1  # and the READ did not crawl again


# --------------------------------------------------------------------------- #
# Criterion 4: no crawl without a fresh proof of control
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["origin_unverified", "origin_verification_stale"])
async def test_an_unproved_origin_ingests_nothing_and_reports_why(monkeypatch, reason):
    calls = _patch_kb(monkeypatch)
    _patch_harvest(monkeypatch, ForeignHarvest(error=reason))
    _forbid_pocket_read(monkeypatch)
    site = _FakeSite(kb_article_ids=["site-home"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == reason
    assert report.ingested == 0
    assert calls["ingest"] == []
    assert calls["remove"] == []  # nothing pruned on a refusal
    assert site.kb_article_ids == ["site-home"]  # what it knew, it still knows
    assert site.kb_sync_error == reason


# --------------------------------------------------------------------------- #
# Criterion 5: a failed crawl reports, and never half-fills the scope
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["crawl_failed", "crawl_blocked_by_robots", "crawl_too_large", "origin_unfetchable"]
)
async def test_a_failed_crawl_reports_the_reason_and_ingests_nothing(monkeypatch, reason):
    """Each failure keeps its own code: "your robots.txt blocks our crawler" and
    "we could not reach your site" need different sentences from the owner."""
    calls = _patch_kb(monkeypatch)
    _patch_harvest(monkeypatch, ForeignHarvest(host="customer.example", error=reason))
    _forbid_pocket_read(monkeypatch)
    site = _FakeSite(kb_article_ids=["site-home", "site-hours-index"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == reason
    assert report.ingested == 0
    assert calls["ingest"] == []
    assert calls["remove"] == []
    assert site.kb_article_ids == ["site-home", "site-hours-index"]


@pytest.mark.asyncio
async def test_a_partial_crawl_ingests_what_arrived_but_prunes_nothing(monkeypatch):
    """A page that failed mid-crawl is not a page that was deleted. The pages that
    DID arrive are re-ingested (which only versions their articles), nothing is
    removed, the ids that were not re-produced stay recorded so a later clean sync
    can still prune them, and the status says the crawl was partial."""
    calls = _patch_kb(monkeypatch)
    _patch_harvest(
        monkeypatch,
        ForeignHarvest(
            host="customer.example",
            source={"index.html": _HARVEST_SOURCE["index.html"]},
            pages_failed=1,
            warnings=["skipped https://customer.example/hours/ — HTTP 502"],
        ),
    )
    _forbid_pocket_read(monkeypatch)
    site = _FakeSite(kb_article_ids=["site-home", "site-hours-index"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.ingested == 1
    assert report.removed == 0
    assert calls["remove"] == []
    assert report.error == "crawl_partial"
    assert site.kb_article_ids == ["site-home", "site-hours-index"]


@pytest.mark.asyncio
async def test_a_clean_crawl_does_prune_what_the_site_stopped_serving(monkeypatch):
    """The other side of the previous test: a COMPLETE harvest is a trustworthy
    answer to "what pages does this site have", so a page that is gone stops
    being quotable."""
    calls = _patch_kb(monkeypatch)
    _patch_harvest(
        monkeypatch,
        ForeignHarvest(
            host="customer.example", source={"index.html": _HARVEST_SOURCE["index.html"]}
        ),
    )
    _forbid_pocket_read(monkeypatch)
    site = _FakeSite(kb_article_ids=["site-home", "site-hours-index"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == ""
    assert report.removed == 1
    assert calls["remove"] == [("pocket:pocket-1", "site-hours-index")]
    assert site.kb_article_ids == ["site-home"]


@pytest.mark.asyncio
async def test_a_crawl_that_found_no_text_reports_no_content_and_prunes_nothing(monkeypatch):
    calls = _patch_kb(monkeypatch)
    _patch_harvest(
        monkeypatch,
        ForeignHarvest(
            host="customer.example", source={"index.html": "<html><body></body></html>"}
        ),
    )
    _forbid_pocket_read(monkeypatch)
    site = _FakeSite(kb_article_ids=["site-home"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == "no_content"
    assert calls["remove"] == []
    assert site.kb_article_ids == ["site-home"]


@pytest.mark.asyncio
async def test_every_page_failing_to_ingest_reports_ingest_failed(monkeypatch):
    async def _boom(scope, text, source):
        raise RuntimeError("kb unreachable")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.ingest_text_to_scope", _boom
    )
    _patch_harvest(monkeypatch, ForeignHarvest(host="customer.example", source=_HARVEST_SOURCE))
    _forbid_pocket_read(monkeypatch)
    site = _FakeSite(kb_article_ids=["site-home"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == "ingest_failed"
    assert report.skipped == 2
    assert site.kb_article_ids == ["site-home"]


# --------------------------------------------------------------------------- #
# Criterion 6, and the three pocket lanes staying exactly as they were
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_foreign_site_is_no_longer_reported_as_skipped(monkeypatch):
    """The state this slice replaced: ``sync_site_knowledge`` used to read a
    foreign site's empty pocket, find nothing, and report zero documents."""
    _patch_kb(monkeypatch)
    _patch_harvest(monkeypatch, ForeignHarvest(host="customer.example", source=_HARVEST_SOURCE))
    _forbid_pocket_read(monkeypatch)

    report = await kb_ingest.sync_site_knowledge(_FakeSite())

    assert report.ingested > 0
    assert report.skipped == 0
    assert report.error == ""


@pytest.mark.asyncio
async def test_a_hosted_site_still_reads_its_pocket_and_never_crawls(monkeypatch):
    """The fork is on ``foreign_origin`` alone, so a hosted site's lane is
    unchanged — and must not reach the crawl."""
    calls = _patch_kb(monkeypatch)

    async def _get(pocket_id, user_id):
        return {"engine": "html", "source": {"index.html": _page("Home", _LONG)}}

    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.service.get", _get)

    def _no_crawl(*a: Any, **kw: Any):
        raise AssertionError("a hosted site must never be crawled")

    monkeypatch.setattr(foreign_grounding, "harvest_foreign_site", _no_crawl)

    report = await kb_ingest.sync_site_knowledge(_FakeSite(foreign_origin=False))

    assert report.ingested == 1
    assert calls["ingest"][0][0] == "pocket:pocket-1"
