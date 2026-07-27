# tests/cloud/test_site_kb_ingest.py — site content → the pocket KB its concierge
# reads (ee.pocketpaw_ee.sites.kb_ingest).
# Created 2026-07-26. A dedicated concierge reads exactly ONE scope,
# pocket:<pocket_id>, and nothing used to put the site's own pages there, so the
# agent was live and knowledge-empty. Layers:
#   * Extraction (pure, no I/O): HTML strips script/style but keeps the title and
#     image alt text; Svelte drops script/style blocks and template expressions;
#     ripple walks the spec for copy while skipping structural keys, and renders
#     price-ish numbers with their key so they are retrievable.
#   * Article sources: deterministic and kb-safe, so a re-sync UPDATES rather than
#     duplicating, and "/", "index.html" and a SvelteKit root route are one article.
#   * Sync: ingests into pocket:<id>, records the ids on the Site, prunes only the
#     ids it previously wrote (the scope is shared with owner-uploaded files, which
#     must survive), and reports rather than raises on an empty or broken read.
#   * Triggers: a live publish and an agent provision each schedule a sync; a
#     PREVIEW publish does not.

from __future__ import annotations

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

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.ingest_text_to_scope", _ingest
    )
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

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.ingest_text_to_scope", _ingest
    )
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
async def test_sync_only_writes_its_own_fields(monkeypatch):
    """The sync runs in the background, minutes after the publish that scheduled it,
    holding a Site instance snapshotted at that moment. A whole-document save would
    silently roll back anything written to the same Site in between — a connected
    domain, a stamped subscription. It must touch only the three kb_* fields.
    """
    _patch_kb(monkeypatch, ingested=[], removed=[])
    _patch_pocket(
        monkeypatch,
        {"engine": "html", "source": {"index.html": f"<p>{_long('We open at 8am.')}</p>"}},
    )
    site = _FakeSite()

    await kb_ingest.sync_site_knowledge(site)

    assert len(site.set_calls) == 1
    assert set(site.set_calls[0]) == {"kb_article_ids", "kb_synced_at", "kb_sync_error"}
