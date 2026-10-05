# tests/cloud/test_site_kb_ingest.py — site content → the pocket KB its concierge
# reads (ee.pocketpaw_ee.sites.kb_ingest). A dedicated concierge reads exactly ONE
# scope, pocket:<pocket_id>, and this module is what fills it. Layers:
#   * Extraction (pure, no I/O): HTML strips script/style but keeps the title and
#     image alt text; a crawled/hosted HTML page reaches the KB as Markdown, so a
#     table stays a table and headings stay headings (the size-guide fixture is
#     our own demo store's page); Svelte drops script/style blocks and template expressions;
#     ripple walks the spec for copy while skipping structural keys, and renders
#     price-ish numbers with their key so they are retrievable.
#   * Article sources: deterministic and kb-safe, so a re-sync UPDATES rather than
#     duplicating, and "/", "index.html" and a SvelteKit root route are one article.
#   * Sync: ingests into pocket:<id>, records the ids (and the crawl index) on the
#     Site, prunes only the ids it previously wrote (the scope is shared with
#     owner-uploaded files), reports rather than raises on an empty or broken read,
#     and records a crash (sync_failed, previous ids kept) without ever raising.
#   * Triggers: a live publish and an agent provision each schedule a sync; a
#     PREVIEW publish does not. A sync that read the pocket schedules the catalog
#     sync, and a failing catalog import changes neither the report nor the catalog.

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from pocketpaw_ee.sites import kb_ingest

# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_html_keeps_copy_and_drops_code():
    html = (
        "<html><head><title>Brew &amp; Co | Hours</title>"
        "<style>body{color:red}</style></head>"
        "<body><h1>Brew &amp; Co</h1><p>We open at 8am.</p>"
        "<script>var tracker = 1;</script>"
        '<img src="latte.jpg" alt="Latte art"></body></html>'
    )
    text = kb_ingest.html_to_text(html)
    assert "Brew & Co | Hours" in text  # the title is the page's own summary
    assert "We open at 8am." in text
    assert "Latte art" in text  # alt text is real content on a visual page
    assert "color:red" not in text
    assert "tracker" not in text


def test_html_survives_malformed_markup():
    """Customer-authored and crawler-harvested pages are not always well formed;
    partial text beats failing the publish that scheduled the sync."""
    text = kb_ingest.html_to_text("<p>Open daily<p>Closed Sunday</div></span>")
    assert "Open daily" in text
    assert "Closed Sunday" in text


_SIZE_GUIDE = Path(__file__).resolve().parents[1] / "fixtures" / "size_guide.html"
# The US men's 10 row of the demo store's footwear chart, as a Markdown table row.
SHOE_ROW = re.compile(r"\|\s*10\s*\|\s*11\.5\s*\|\s*9\s*\|\s*44\s*\|\s*28\.0\s*\|")


def test_a_site_page_keeps_its_tables_and_headings_as_markdown():
    """A size chart flattened to one cell per line made the compile model rebuild
    the table, and the concierge then said the chart "isn't available". The page
    reaches the KB as Markdown instead: rows stay rows, headings stay headings."""
    html = _SIZE_GUIDE.read_text(encoding="utf-8")

    [doc] = kb_ingest.extract_site_documents(engine="html", source={"size-guide.html": html})

    assert SHOE_ROW.search(doc.text), doc.text
    assert re.search(r"^#{1,6} Footwear\s*$", doc.text, re.MULTILINE)
    assert re.search(r"^# Size guide\s*$", doc.text, re.MULTILINE)
    assert "Size guide | Cairn & Co." in doc.text  # the <title> is still read
    assert "(541) 555-0142" in doc.text  # the footer's contact details are copy
    assert "__sveltekit" not in doc.text  # script content stays out
    assert "Tents & Sleep" not in doc.text  # navigation link lists stay out
    assert "\n\n\n" not in doc.text


def test_html_to_markdown_keeps_title_and_alt_text_and_drops_code():
    html = (
        "<html><head><title>Brew &amp; Co | Hours</title>"
        "<style>body{color:red}</style></head>"
        "<body><nav><a href='/'>Home</a></nav><h1>Brew &amp; Co</h1><p>We open at 8am.</p>"
        "<script>var tracker = 1;</script>"
        '<img src="latte.jpg" alt="Latte art"></body></html>'
    )
    text = kb_ingest.html_to_markdown(html)
    assert "Brew & Co | Hours" in text
    assert re.search(r"^# Brew & Co\s*$", text, re.MULTILINE)
    assert "We open at 8am." in text
    assert "Latte art" in text
    assert "color:red" not in text
    assert "tracker" not in text
    assert "Home" not in text


@pytest.mark.parametrize(
    "html",
    [
        "<p>Open daily<p>Closed Sunday</div></span>",
        "<table><tr><td>Open daily<td>Closed Sunday",
        "<<<>>>Open daily</p></p></table> Closed Sunday",
    ],
)
def test_html_to_markdown_survives_malformed_markup(html):
    text = kb_ingest.html_to_markdown(html)
    assert "Open daily" in text
    assert "Closed Sunday" in text


def test_html_to_markdown_falls_back_to_plain_text_when_conversion_fails(monkeypatch):
    def _boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("converter crashed")

    monkeypatch.setattr(kb_ingest, "_convert_markdown", _boom)
    text = kb_ingest.html_to_markdown("<h1>Menu</h1><p>Flat white</p>")
    assert "Menu" in text and "Flat white" in text


def test_svelte_drops_code_blocks_and_expressions():
    source = (
        "<script>let price = 320; import X from './X.svelte';</script>"
        "<h1>Menu</h1><p>Flat white {price} rupees</p>"
        "<style>h1{font-size:2rem}</style>"
    )
    text = kb_ingest.svelte_to_text(source)
    assert "Menu" in text
    assert "Flat white" in text and "rupees" in text
    assert "import" not in text
    assert "font-size" not in text


def test_spec_collects_copy_and_skips_structure():
    spec = {
        "type": "page",
        "id": "p1",
        "blocks": [
            {
                "type": "hero",
                "heading": "Acme Bakery",
                "sub": "Fresh sourdough daily",
                "class": "text-lg",
                "href": "https://acme.test/order",
                "color": "#ff0055",
            },
            {"type": "catalog", "items": [{"name": "Sourdough", "price_cents": 500}]},
        ],
    }
    text = kb_ingest.spec_to_text(spec)
    assert "Acme Bakery" in text
    assert "Fresh sourdough daily" in text
    assert "Sourdough" in text
    assert "price_cents 500" in text  # a bare 500 would be unretrievable
    assert "text-lg" not in text
    assert "acme.test" not in text
    assert "#ff0055" not in text


def test_spec_walk_terminates_on_a_self_referencing_dict():
    spec: dict[str, Any] = {"heading": "Acme Bakery"}
    spec["self"] = spec
    assert "Acme Bakery" in kb_ingest.spec_to_text(spec)


def test_spec_ignores_booleans():
    """A flag is never copy, and bool is an int, so it must be checked first."""
    assert kb_ingest.spec_to_text({"visible": True, "heading": "Acme"}).strip() == "Acme"


# --------------------------------------------------------------------------- #
# Article sources (idempotence depends on these being stable)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "site-home"),
        ("index.html", "site-home"),
        ("src/routes/+page.svelte", "site-home"),
        ("/about.html", "site-about"),
        ("/about", "site-about"),
        ("src/routes/menu/+page.svelte", "site-menu"),
        ("/products/coffee.html", "site-products-coffee"),
    ],
)
def test_article_source_is_stable_and_kb_safe(path, expected):
    """kb-go derives an article id from the source and splits it on "/", so a source
    must carry no slashes, must be prefixed so it cannot collide with an
    owner-uploaded file in the same pocket scope, and must be identical across syncs
    (that is what makes a re-sync version the article instead of duplicating it)."""
    source = kb_ingest._path_slug(path)
    assert source == expected
    assert "/" not in source
    assert source.startswith("site-")


# --------------------------------------------------------------------------- #
# Document assembly
# --------------------------------------------------------------------------- #


def _long(text: str) -> str:
    return (text + " ") * 8


def test_html_lane_makes_one_document_per_page_and_skips_assets():
    docs = kb_ingest.extract_site_documents(
        engine="html",
        source={
            "index.html": f"<h1>Home</h1><p>{_long('Open 8am to 6pm daily.')}</p>",
            "about.html": f"<p>{_long('Baking here since 1998.')}</p>",
            "styles.css": "body{margin:0}",
            "app.js": "console.log(1)",
        },
    )
    assert [d.source for d in docs] == ["site-about", "site-home"]
    assert all("margin:0" not in d.text for d in docs)


def test_ripple_lane_makes_one_document_for_the_whole_spec():
    docs = kb_ingest.extract_site_documents(
        engine="ripple",
        ripple_spec={"blocks": [{"heading": _long("Acme Bakery opens at 8am.")}]},
    )
    assert len(docs) == 1
    assert docs[0].source == "site-home"


def test_near_empty_pages_are_skipped():
    """An empty layout or a redirect stub has nothing answerable in it and would
    only dilute BM25 scoring."""
    docs = kb_ingest.extract_site_documents(
        engine="html", source={"index.html": "<html><body><div></div></body></html>"}
    )
    assert docs == []


def test_foreign_site_with_an_empty_pocket_yields_nothing():
    """A concierge embedded on a site we do not host has no content in its pocket.
    That is a real state, not a crash — grounding it means crawling the customer's
    own origin, which is a separate, SSRF-hardened path."""
    assert kb_ingest.extract_site_documents(engine="html", source=None) == []


def test_page_count_is_capped():
    source = {f"p{i}.html": f"<p>{_long('Page about our services.')}</p>" for i in range(200)}
    docs = kb_ingest.extract_site_documents(engine="html", source=source)
    assert len(docs) == kb_ingest._MAX_DOCUMENTS


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


class _FakeSite:
    """Stands in for the Beanie Site doc — only the fields the sync touches."""

    def __init__(self, **ov: Any) -> None:
        self.id = "site-1"
        self.pocket_id = "pocket-1"
        self.owner = "user:maya"
        self.workspace = "ws-1"
        self.kb_article_ids: list[str] = []
        self.kb_synced_at = None
        self.kb_sync_error = ""
        self.set_calls: list[dict] = []
        self.__dict__.update(ov)

    async def set(self, updates: dict) -> None:
        """Mirrors Beanie's ``$set``: applies only the named fields. The sync must
        never write anything outside these, so the fake records what it was asked
        for and the tests assert on it."""
        self.set_calls.append(dict(updates))
        for key, value in updates.items():
            setattr(self, key, value)


def patch_page_ingest(monkeypatch, fake):
    """Fake the sync's one ingest call, ``ingest_sections_to_scope``, with a
    per-article ``fake(scope, text, source)`` returning a kb receipt. A raise
    fails that section; a page whose every section failed raises, as the real
    one does."""
    from pocketpaw_ee.cloud.agents.knowledge import (
        KnowledgeEngineUnavailable,
        KnowledgeService,
        extract_ingest_article_id,
    )

    async def _sections(scope, sections):
        ids = []
        for section in sections:
            try:
                ids.append(
                    extract_ingest_article_id(await fake(scope, section.text, section.source))
                )
            except KnowledgeEngineUnavailable:
                raise
            except Exception:
                if len(sections) == 1:
                    raise
                ids.append("")
        if not any(ids):
            raise RuntimeError("no section landed")
        return {"article": next(i for i in ids if i), "section_articles": ids}

    monkeypatch.setattr(KnowledgeService, "ingest_sections_to_scope", staticmethod(_sections))


def _patch_kb(monkeypatch, *, ingested: list[str], removed: list[str]):
    """Capture what the sync asks kb-go to do, without a subprocess."""
    calls: dict[str, list] = {"ingest": [], "remove": []}

    async def _ingest(scope, text, source):
        calls["ingest"].append((scope, source, text))
        return {"article": ingested.pop(0) if ingested else source}

    async def _remove(scope, article_id):
        calls["remove"].append((scope, article_id))
        removed.append(article_id)
        return True

    patch_page_ingest(monkeypatch, _ingest)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.remove_article", _remove
    )
    return calls


def _patch_pocket(monkeypatch, pocket: dict | None):
    async def _get(pocket_id, user_id):
        if pocket is None:
            raise RuntimeError("pocket gone")
        return pocket

    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.service.get", _get)


@pytest.mark.asyncio
async def test_sync_ingests_pages_into_the_pocket_scope(monkeypatch):
    """The scope must be the one a CONCIERGE run reads — pocket:<id> and nothing
    else. Any other scope means the ingest is invisible to the agent."""
    calls = _patch_kb(monkeypatch, ingested=[], removed=[])
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('We open at 8am.')}</p>"}},
    )
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.ingested == 1
    assert report.error == ""
    assert calls["ingest"][0][0] == "pocket:pocket-1"
    assert site.kb_article_ids == ["site-home"]
    assert site.kb_synced_at is not None


@pytest.mark.asyncio
async def test_resync_prunes_only_pages_that_disappeared(monkeypatch):
    """A renamed or deleted page must stop being quotable, but the pocket scope is
    SHARED with owner-uploaded files, so the sync may only delete ids it wrote."""
    removed: list[str] = []
    _patch_kb(monkeypatch, ingested=[], removed=removed)
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('We open at 8am.')}</p>"}},
    )
    site = _FakeSite(kb_article_ids=["site-home", "site-old-page", "an-uploaded-file"])

    await kb_ingest.sync_site_knowledge(site)

    assert removed == ["site-old-page", "an-uploaded-file"]  # both were OURS to prune
    assert site.kb_article_ids == ["site-home"]


@pytest.mark.asyncio
async def test_sync_never_prunes_an_id_it_did_not_record(monkeypatch):
    """The uploads a pocket holds are not in kb_article_ids, so they are untouched."""
    removed: list[str] = []
    _patch_kb(monkeypatch, ingested=[], removed=removed)
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('We open at 8am.')}</p>"}},
    )
    site = _FakeSite(kb_article_ids=[])

    await kb_ingest.sync_site_knowledge(site)

    assert removed == []


@pytest.mark.asyncio
async def test_sync_keeps_previous_ids_when_there_is_nothing_to_ingest(monkeypatch):
    """An empty read may be transient. Forgetting the ids would strand those
    articles beyond the reach of any future prune, so they are kept and the reason
    is recorded."""
    _patch_kb(monkeypatch, ingested=[], removed=[])
    _patch_pocket(monkeypatch, {"engine": "html", "source": {}})
    site = _FakeSite(kb_article_ids=["site-home"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == "no_content"
    assert report.ingested == 0
    assert site.kb_article_ids == ["site-home"]  # not stranded
    assert site.kb_sync_error == "no_content"


@pytest.mark.asyncio
async def test_sync_reports_an_unreadable_pocket_instead_of_raising(monkeypatch):
    _patch_kb(monkeypatch, ingested=[], removed=[])
    _patch_pocket(monkeypatch, None)
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == "pocket_unavailable"
    assert site.kb_sync_error == "pocket_unavailable"


@pytest.mark.asyncio
async def test_one_failed_page_does_not_lose_the_others(monkeypatch):
    async def _ingest(scope, text, source):
        if source == "site-about":
            raise RuntimeError("kb exploded")
        return {"article": source}

    patch_page_ingest(monkeypatch, _ingest)
    _patch_pocket(
        monkeypatch,
        {
            "engine": "html",
            "source": {
                "index.html": f"<p>{_long('We open at 8am.')}</p>",
                "about.html": f"<p>{_long('Baking since 1998.')}</p>",
            },
        },
    )
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.ingested == 1
    assert report.skipped == 1
    assert site.kb_article_ids == ["site-home"]


@pytest.mark.asyncio
async def test_safe_sync_swallows_everything(monkeypatch):
    """The background form is used from publish and from an agent bind, neither of
    which may fail because a KB sync did."""

    async def _boom(site):
        raise RuntimeError("nope")

    monkeypatch.setattr(kb_ingest, "sync_site_knowledge", _boom)
    report = await kb_ingest.safe_sync_site_knowledge(_FakeSite())
    assert report.error == "sync_failed"


@pytest.mark.asyncio
async def test_safe_sync_records_a_crash_on_the_site(monkeypatch):
    """A crash is a failure like any other, so it lands on the Site the way the
    others do: the reason in kb_sync_error, a fresh kb_synced_at, and the previous
    article ids kept (they are still in the KB, so a future prune must reach them).
    Before this, the crash returned sync_failed and wrote nothing, and the owner's
    knowledge panel went on showing the last clean sync."""

    async def _boom(site):
        raise RuntimeError("nope")

    monkeypatch.setattr(kb_ingest, "sync_site_knowledge", _boom)
    site = _FakeSite(kb_article_ids=["site-home"], kb_sync_error="")
    report = await kb_ingest.safe_sync_site_knowledge(site)

    assert report.error == "sync_failed"
    assert len(site.set_calls) == 1
    assert set(site.set_calls[0]) == {"kb_article_ids", "kb_synced_at", "kb_sync_error"}
    assert site.kb_sync_error == "sync_failed"
    assert site.kb_synced_at is not None
    assert site.kb_article_ids == ["site-home"]


@pytest.mark.asyncio
async def test_safe_sync_survives_a_failure_to_record_the_crash(monkeypatch):
    """Recording is best-effort: a Site that cannot be written still gets the
    sync_failed report back, never an exception."""

    async def _boom(site):
        raise RuntimeError("nope")

    class _Unwritable(_FakeSite):
        async def set(self, updates: dict) -> None:
            raise RuntimeError("mongo is down")

    monkeypatch.setattr(kb_ingest, "sync_site_knowledge", _boom)
    report = await kb_ingest.safe_sync_site_knowledge(_Unwritable())
    assert report.error == "sync_failed"

    class _Broken:
        """No kb fields at all and no ``set``: the recording path must not raise
        even on a Site object this malformed."""

    report = await kb_ingest.safe_sync_site_knowledge(_Broken())
    assert report.error == "sync_failed"


@pytest.mark.asyncio
async def test_sync_only_writes_its_own_fields(monkeypatch):
    """The sync runs in the background, minutes after the publish that scheduled it,
    holding a Site instance snapshotted at that moment. A whole-document save would
    silently roll back anything written to the same Site in between — a connected
    domain, a stamped subscription. It must touch only its own kb_* fields.
    """
    _patch_kb(monkeypatch, ingested=[], removed=[])
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('We open at 8am.')}</p>"}},
    )
    site = _FakeSite()

    await kb_ingest.sync_site_knowledge(site)

    assert len(site.set_calls) == 1
    assert set(site.set_calls[0]) == {
        "kb_article_ids",
        "kb_page_index",
        "kb_synced_at",
        "kb_sync_error",
    }


@pytest.mark.asyncio
async def test_total_ingest_failure_is_reported_not_called_clean(monkeypatch):
    """A site that HAS pages where not one of them made it in means the ingest
    engine is unreachable. Reporting that as a clean sync of nothing leaves the
    dashboard saying "nothing learned yet" while the real problem is invisible."""

    async def _broken(scope, text, source):
        raise RuntimeError("kb binary not found")

    patch_page_ingest(monkeypatch, _broken)
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('We open at 8am.')}</p>"}},
    )
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.ingested == 0
    assert report.skipped == 1
    assert report.error == "ingest_failed"
    assert site.kb_sync_error == "ingest_failed"


@pytest.mark.asyncio
async def test_a_failed_ingest_never_purges_the_existing_knowledge(monkeypatch):
    """The fresh set is what "no longer produced" is measured against, so an empty
    one from a failed run would mark every existing article stale. A transient
    outage must not wipe the site's whole knowledge base."""
    removed: list[str] = []

    async def _broken(scope, text, source):
        raise RuntimeError("kb down")

    async def _remove(scope, article_id):
        removed.append(article_id)
        return True

    patch_page_ingest(monkeypatch, _broken)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.remove_article", _remove
    )
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('We open at 8am.')}</p>"}},
    )
    site = _FakeSite(kb_article_ids=["site-home", "site-about", "site-pricing"])

    await kb_ingest.sync_site_knowledge(site)

    assert removed == []  # nothing deleted
    assert site.kb_article_ids == ["site-home", "site-about", "site-pricing"]  # all kept


# --------------------------------------------------------------------------- #
# The kb engine itself is missing or outdated
# --------------------------------------------------------------------------- #

_THREE_PAGES = {
    "engine": "html",
    "source": {
        "index.html": f"<p>{_long('We open at 8am.')}</p>",
        "about.html": f"<p>{_long('Baking since 1998.')}</p>",
        "menu.html": f"<p>{_long('Sourdough, rye and seeded loaves.')}</p>",
    },
}


def _patch_real_ingest(monkeypatch, kb_result):
    """Run the REAL section ingest, faking only the agent's section compile and
    the kb subprocess. Returns the call counters."""
    from pocketpaw_ee.cloud.agents import knowledge

    calls = {"compile": 0, "kb": 0}

    async def _compile(section, source, index, total, lang=None, **_kw):
        calls["compile"] += 1
        return {"title": source, "summary": "s", "content": section.text, "source": source,
                "concepts": [], "categories": [], "compiled_with": "test"}  # fmt: skip

    def _kb(*args, input_text=None, timeout=120):
        calls["kb"] += 1
        if isinstance(kb_result, BaseException):
            raise kb_result
        return kb_result

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(knowledge, "_compile_section_with_agent", _compile)
    monkeypatch.setattr(knowledge, "_kb", _kb)
    return calls


@pytest.mark.asyncio
async def test_an_outdated_kb_binary_stops_the_sync_and_names_the_engine(monkeypatch):
    """A kb-go build older than `ingest --article-json` ignores the flag, stores
    the payload verbatim and answers without `compiled_with`. Every page then
    failed on its own: one paid agent compile, one junk article and one warning
    PER PAGE, ending in an `ingest_failed` that blamed the save. The first such
    answer must stop the sync and report the engine, not the site."""
    old_binary_receipt = {"article": "manual", "title": "manual", "words": 8}
    calls = _patch_real_ingest(monkeypatch, old_binary_receipt)
    _patch_pocket(monkeypatch, _THREE_PAGES)
    site = _FakeSite(kb_article_ids=["site-home"])

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == "kb_unavailable"
    assert site.kb_sync_error == "kb_unavailable"
    assert calls["kb"] == 1  # stopped at the first page
    assert calls["compile"] == 1  # no further paid compiles
    assert site.kb_article_ids == ["site-home"]  # nothing pruned


@pytest.mark.asyncio
async def test_a_missing_kb_binary_reports_the_engine(monkeypatch):
    from pocketpaw_ee.cloud.agents import knowledge

    monkeypatch.setattr(knowledge, "KB_BIN", "Z:/definitely/not/here/kb-go-missing")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def _compile(section, source, index, total, lang=None, **_kw):
        return {"title": source, "summary": "s", "content": section.text, "source": source,
                "concepts": [], "categories": [], "compiled_with": "test"}  # fmt: skip

    monkeypatch.setattr(knowledge, "_compile_section_with_agent", _compile)
    _patch_pocket(monkeypatch, _THREE_PAGES)
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == "kb_unavailable"
    assert report.ingested == 0


@pytest.mark.asyncio
async def test_a_per_page_kb_error_still_skips_only_that_page(monkeypatch):
    """An ordinary kb failure is still per page: the engine works, one page broke."""
    calls = _patch_real_ingest(monkeypatch, RuntimeError("kb failed: bad utf-8"))
    _patch_pocket(monkeypatch, _THREE_PAGES)
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert calls["kb"] == 3
    assert report.error == "ingest_failed"


# --------------------------------------------------------------------------- #
# The catalog follows the knowledge: a sync schedules the site's catalog sync
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_hosted_sync_schedules_the_catalog_sync(monkeypatch, scheduled_catalog_syncs):
    _patch_kb(monkeypatch, ingested=[], removed=[])
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('Mugs for sale.')}</p>"}},
    )
    site = _FakeSite()

    await kb_ingest.sync_site_knowledge(site)

    assert scheduled_catalog_syncs == [site]


@pytest.mark.asyncio
async def test_an_unreadable_pocket_schedules_no_catalog_sync(monkeypatch, scheduled_catalog_syncs):
    _patch_pocket(monkeypatch, None)

    await kb_ingest.sync_site_knowledge(_FakeSite())

    assert scheduled_catalog_syncs == []


@pytest.mark.asyncio
async def test_a_failing_catalog_import_changes_neither_the_report_nor_the_catalog(
    monkeypatch, scheduled_catalog_syncs, tmp_path
):
    from pocketpaw_ee.paw_bar import catalog_import, catalog_sync
    from pocketpaw_ee.paw_bar.catalog_import import CatalogImportPreview

    from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget
    from pocketpaw.paw_bar.store import PawBarStore

    store = PawBarStore(tmp_path / "paw_bar.db")
    widget = await store.create_widget(
        PawBarWidget(
            pocket_id="pocket-1",
            owner="user:maya",
            workspace_id="ws-1",
            spec=PawBarSpec(widget_id="w", pocket_id="pocket-1"),
        )
    )
    await store.upsert_catalog_items(widget.id, [{"id": "p1", "name": "Mine", "price_cents": 5}])
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)

    async def _failed(site, **_kw):
        return CatalogImportPreview(status="failed", reason="fetch_failed")

    monkeypatch.setattr(catalog_import, "preview_catalog_import", _failed)
    _patch_kb(monkeypatch, ingested=[], removed=[])
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('Mugs for sale.')}</p>"}},
    )
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)
    before = (asdict(report), list(site.kb_article_ids), site.kb_sync_error, site.kb_synced_at)
    [scheduled] = scheduled_catalog_syncs
    await catalog_sync.safe_sync_site_catalog(scheduled)

    after = (asdict(report), list(site.kb_article_ids), site.kb_sync_error, site.kb_synced_at)
    assert after == before
    assert report.error == "" and report.ingested == 1
    items, _ = await store.list_catalog(widget.id)
    assert [i.id for i in items] == ["p1"]
    assert site.catalog_sync_status == "fetch_failed"


# --------------------------------------------------------------------------- #
# A long page is several section articles
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_long_page_records_every_section_and_prunes_the_old_set(monkeypatch):
    """The real sectioned ingest, faked at the LLM and the kb binary: a page past
    the section size lands as several articles, all recorded on the Site, the
    first standing for the page; a re-sync that produces a new set prunes every
    id of the old one."""
    from pocketpaw_ee.cloud.agents import knowledge

    from tests.cloud.agents.test_knowledge_sectioned_ingest import (
        _Compiler,
        _FakeKb,
        _install,
        _long_doc,
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    kb = _FakeKb()
    monkeypatch.setattr(knowledge, "_kb", kb)
    _install(monkeypatch, _Compiler())

    def page(topic: str, count: int) -> dict:
        body = "".join(
            f"<h2>{line[2:]}</h2>" if line.startswith("# ") else f"<p>{line}</p>"
            for line in _long_doc(count).replace("Chapter", topic).split("\n\n")
        )
        return {"engine": "html", "source": {"pricing.html": body}}

    _patch_pocket(monkeypatch, page("Spring", 4))
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert (report.ingested, report.error) == (1, "")
    first = list(site.kb_article_ids)
    assert len(first) > 1 and sorted(first) == sorted(kb.articles)
    [entry] = site.kb_page_index.values()
    assert entry["id"] == first[0]

    _patch_pocket(monkeypatch, page("Summer", 3))
    await kb_ingest.sync_site_knowledge(site)

    assert sorted(kb.deleted) == sorted(first)
    assert sorted(site.kb_article_ids) == sorted(kb.articles)
    assert not set(site.kb_article_ids) & set(first)
