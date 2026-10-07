# tests/cloud/test_site_kb_sections.py — a long site page is several kb articles.
#
# ``sites.page_sections.split_page`` cuts a page's Markdown at its H1/H2
# headings (H3 inside an H2 section that is still too big), merges tiny pieces
# into a neighbour and falls back to ``knowledge_sections`` for heading-less
# text. A page up to ``SHORT_PAGE_CHARS`` stays one article. Pinned here:
#   * the split: breadcrumb titles ("Page › Heading"), a stable unique source per
#     section ("<page source>#<heading-slug>", repeats get -2, -3), the anchor a
#     heading carried in the page's HTML, every section within the size cap;
#   * the sync (real ``KnowledgeService`` section path; the LLM and the kb binary
#     faked at their boundaries): one article per section, the page index's new
#     ``sections`` shape, an unchanged re-sync that compiles nothing, a removed
#     section that deletes exactly its article, a failed compile or an unreadable
#     pocket that deletes nothing, and an old-shape index entry read and replaced.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pocketpaw_ee.cloud.agents import knowledge
from pocketpaw_ee.sites import kb_ingest, page_sections

from tests.cloud.agents.test_knowledge_sectioned_ingest import _Compiler, _FakeKb, _install

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_DOCS_HTML = (_FIXTURES / "docs_page.html").read_text(encoding="utf-8")
_SIZE_GUIDE_HTML = (_FIXTURES / "size_guide.html").read_text(encoding="utf-8")

_DOCS_TITLES = [
    "Installing Brewkit › Requirements",
    "Installing Brewkit › Install on macOS",
    "Installing Brewkit › Install on Windows › With winget",
    "Installing Brewkit › Install on Windows › With the MSI",
    "Installing Brewkit › Configuration",
    "Installing Brewkit › Troubleshooting",
]
_DOCS_SOURCES = [
    "site-docs-install#requirements",
    "site-docs-install#install-on-macos",
    "site-docs-install#with-winget",
    "site-docs-install#with-the-msi",
    "site-docs-install#configuration",
    "site-docs-install#troubleshooting",
]


def _doc(html: str, path: str = "docs/install.html") -> Any:
    [doc] = kb_ingest.extract_site_documents(engine="html", source={path: html})
    return doc


# --------------------------------------------------------------------------- #
# The split
# --------------------------------------------------------------------------- #


def test_a_long_docs_page_splits_at_its_headings():
    sections = page_sections.split_page(_doc(_DOCS_HTML))

    assert [s.title for s in sections] == _DOCS_TITLES
    assert [s.source for s in sections] == _DOCS_SOURCES
    assert all(len(s.text) <= page_sections.MAX_SECTION_CHARS for s in sections)
    # The breadcrumb leads each section so the compile keeps its context.
    assert all(s.text.startswith(f"{s.title}\n\n") for s in sections)
    # The intro under the H1 is too small alone; it joins the first section.
    assert "command-line tool" in sections[0].text
    # Every word of the page lands somewhere.
    joined = "\n".join(s.text for s in sections)
    for fact in ("E-102", "$19 per month", "1603", "LocalService", "90 days", "--verbose"):
        assert fact in joined


def test_sections_carry_the_heading_ids_of_the_page_html():
    sections = page_sections.split_page(_doc(_DOCS_HTML))

    assert [s.anchor for s in sections] == [
        "requirements",
        "macos",
        "windows-winget",
        "windows-msi",
        "configuration",
        "troubleshooting",
    ]


def test_the_size_guide_splits_into_its_tables():
    sections = page_sections.split_page(_doc(_SIZE_GUIDE_HTML, "size-guide.html"))

    footwear = [s for s in sections if s.heading == "Footwear"]
    assert len(footwear) == 1
    assert footwear[0].title == "Size guide › Footwear"
    assert footwear[0].source == "site-size-guide#footwear"
    assert footwear[0].anchor == "footwear"
    # The whole table travels with its heading.
    assert "| 13 " in footwear[0].text and "47.5" in footwear[0].text
    assert len({s.source for s in sections}) == len(sections) > 2


def test_a_short_page_stays_one_article():
    html = "<title>Hours</title><h1>Hours</h1><h2>Weekdays</h2><p>Open 8 to 6.</p>" * 3
    doc = _doc(html, "hours.html")
    assert len(doc.text) <= page_sections.SHORT_PAGE_CHARS

    [section] = page_sections.split_page(doc)

    assert section.source == "site-hours"  # the page's own source, as before
    assert section.text == doc.text  # no breadcrumb: the page is its own context
    assert section.heading == ""


def test_repeated_headings_get_unique_sources():
    body = "Plenty to say about this part of the guide, and every word matters. " * 12
    text = "\n\n".join(
        [
            "# Guide",
            f"## Notes\n\n{body}",
            f"## Setup\n\n{body}",
            f"## Notes\n\n{body}",
            f"## Notes\n\n{body}",
        ]
    )
    doc = kb_ingest.SiteDocument(path="guide.html", source="site-guide", text=text)

    sources = [s.source for s in page_sections.split_page(doc)]

    assert sources == [
        "site-guide#notes",
        "site-guide#setup",
        "site-guide#notes-2",
        "site-guide#notes-3",
    ]


def test_heading_less_long_text_falls_back_to_the_size_split():
    sentence = "Sourdough is proofed for eighteen hours and baked at dawn every day. "
    doc = kb_ingest.SiteDocument(
        path="src/routes/story/+page.svelte",
        source="site-story",
        text="\n\n".join(sentence * 6 for _ in range(12)),
    )

    sections = page_sections.split_page(doc)

    assert len(sections) > 1
    assert len({s.source for s in sections}) == len(sections)
    assert all(s.source.startswith("site-story#") for s in sections)
    assert all(len(s.text) <= page_sections.MAX_SECTION_CHARS for s in sections)


def test_a_heading_inside_a_code_fence_is_not_a_boundary():
    filler = "Each step below is required and runs in order on a clean machine. " * 16
    text = (
        f"# Deploy\n\n## Steps\n\n{filler}\n\n```sh\n# not a heading\nmake deploy\n```\n\n"
        f"{filler}\n\n## After\n\n{filler}"
    )
    doc = kb_ingest.SiteDocument(path="deploy.html", source="site-deploy", text=text)

    headings = [s.heading for s in page_sections.split_page(doc)]

    assert "not a heading" not in headings
    assert headings == ["Steps", "After"]


# --------------------------------------------------------------------------- #
# The sync
# --------------------------------------------------------------------------- #


class _FakeSite:
    def __init__(self, **ov: Any) -> None:
        self.id = "site-1"
        self.pocket_id = "pocket-1"
        self.owner = "user:maya"
        self.workspace = "ws-1"
        self.kb_article_ids: list[str] = []
        self.kb_page_index: dict = {}
        self.kb_synced_at = None
        self.kb_sync_error = ""
        self.__dict__.update(ov)

    async def set(self, updates: dict) -> None:
        for key, value in updates.items():
            setattr(self, key, value)


def _pocket(monkeypatch, source: dict[str, str] | None) -> None:
    async def _get(pocket_id, user_id):
        if source is None:
            raise RuntimeError("pocket store down")
        return {"engine": "html", "source": source}

    monkeypatch.setattr("pocketpaw_ee.cloud.pockets.service.get", _get)


@pytest.fixture
def kb(monkeypatch) -> _FakeKb:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeKb()
    monkeypatch.setattr(knowledge, "_kb", fake)
    return fake


@pytest.fixture
def compiler(monkeypatch) -> _Compiler:
    return _install(monkeypatch, _Compiler())


@pytest.mark.asyncio
async def test_a_long_page_ingests_one_article_per_section(monkeypatch, kb, compiler):
    _pocket(monkeypatch, {"docs/install.html": _DOCS_HTML})
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert (report.ingested, report.error) == (1, "")
    assert [p["article"]["title"] for p in kb.payloads] == _DOCS_TITLES
    assert [p["article"]["source"] for p in kb.payloads] == _DOCS_SOURCES
    assert all(p["raw_text"].startswith(p["article"]["title"]) for p in kb.payloads)
    assert len(compiler.prompts) == len(_DOCS_TITLES)
    entry = site.kb_page_index["docs/install"]
    ids = [s["id"] for s in entry["sections"]]
    assert ids == site.kb_article_ids and sorted(ids) == sorted(kb.articles)
    # Old readers keep working: the page's first section stands for the page.
    assert entry["id"] == ids[0]
    assert entry["title"] == "Installing Brewkit"
    assert [s["title"] for s in entry["sections"]] == _DOCS_TITLES
    assert [s["source"] for s in entry["sections"]] == _DOCS_SOURCES
    assert entry["sections"][2]["anchor"] == "windows-winget"
    assert all(s["hash"] for s in entry["sections"])


@pytest.mark.asyncio
async def test_an_unchanged_page_is_not_recompiled(monkeypatch, kb, compiler):
    _pocket(monkeypatch, {"docs/install.html": _DOCS_HTML})
    site = _FakeSite()
    await kb_ingest.sync_site_knowledge(site)
    first_ids = list(site.kb_article_ids)
    prompts = len(compiler.prompts)

    report = await kb_ingest.sync_site_knowledge(site)

    assert (report.ingested, report.error, report.removed) == (1, "", 0)
    assert len(compiler.prompts) == prompts  # nothing compiled again
    assert kb.deleted == []
    assert site.kb_article_ids == first_ids


@pytest.mark.asyncio
async def test_a_removed_section_deletes_exactly_its_article(monkeypatch, kb, compiler):
    _pocket(monkeypatch, {"docs/install.html": _DOCS_HTML})
    site = _FakeSite()
    await kb_ingest.sync_site_knowledge(site)
    by_source = {s["source"]: s["id"] for s in site.kb_page_index["docs/install"]["sections"]}
    prompts = len(compiler.prompts)

    head, _, tail = _DOCS_HTML.partition('<h2 id="configuration">')
    without_config = head + '<h2 id="troubleshooting">' + tail.split('<h2 id="troubleshooting">')[1]
    _pocket(monkeypatch, {"docs/install.html": without_config})
    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == ""
    assert kb.deleted == [by_source["site-docs-install#configuration"]]
    assert report.removed == 1
    assert len(compiler.prompts) == prompts  # the other sections did not change
    assert by_source["site-docs-install#configuration"] not in site.kb_article_ids
    assert len(site.kb_article_ids) == len(_DOCS_SOURCES) - 1


@pytest.mark.asyncio
async def test_a_failed_section_compile_deletes_nothing(monkeypatch, kb, compiler):
    _pocket(monkeypatch, {"docs/install.html": _DOCS_HTML})
    site = _FakeSite()
    await kb_ingest.sync_site_knowledge(site)
    before = list(site.kb_article_ids)

    def _broken(section, prompt):
        raise RuntimeError("model provider down")

    compiler.respond = _broken
    changed = _DOCS_HTML.replace("200 MB of free disk space", "300 MB of free disk space")
    _pocket(monkeypatch, {"docs/install.html": changed})
    await kb_ingest.sync_site_knowledge(site)

    assert kb.deleted == []
    assert sorted(site.kb_article_ids) == sorted(before)


@pytest.mark.asyncio
async def test_an_unreadable_pocket_deletes_nothing(monkeypatch, kb, compiler):
    _pocket(monkeypatch, {"docs/install.html": _DOCS_HTML})
    site = _FakeSite()
    await kb_ingest.sync_site_knowledge(site)
    before = list(site.kb_article_ids)

    _pocket(monkeypatch, None)
    report = await kb_ingest.sync_site_knowledge(site)

    assert report.error == "pocket_unavailable"
    assert kb.deleted == []
    assert site.kb_article_ids == before


@pytest.mark.asyncio
async def test_an_old_shape_index_entry_is_read_and_replaced(monkeypatch, kb, compiler):
    _pocket(monkeypatch, {"docs/install.html": _DOCS_HTML})
    site = _FakeSite(
        kb_article_ids=["installing-brewkit"],
        kb_page_index={"docs/install": {"id": "installing-brewkit", "title": "Installing"}},
    )

    await kb_ingest.sync_site_knowledge(site)

    assert len(compiler.prompts) == len(_DOCS_TITLES)  # nothing to reuse
    assert kb.deleted == ["installing-brewkit"]
    entry = site.kb_page_index["docs/install"]
    assert len(entry["sections"]) == len(_DOCS_TITLES)


def test_page_index_readers_accept_both_shapes():
    old = {"id": "a1", "title": "Returns"}
    new = {
        "id": "s1",
        "title": "Docs",
        "sections": [
            {"id": "s1", "title": "Docs › A", "source": "site-docs#a", "hash": "h", "anchor": ""},
            {"id": "s2", "title": "Docs › B", "source": "site-docs#b", "hash": "h", "anchor": "b"},
        ],
    }

    assert [s["id"] for s in page_sections.index_sections(old)] == ["a1"]
    assert page_sections.index_sections(old)[0]["title"] == "Returns"
    assert [s["id"] for s in page_sections.index_sections(new)] == ["s1", "s2"]
    assert page_sections.index_sections({}) == []
    assert page_sections.index_sections("junk") == []


# --------------------------------------------------------------------------- #
# Every site article is compiled by PocketPaw, fact-preserving
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_short_page_gets_the_fact_preserving_compile(monkeypatch, kb, compiler):
    """Business pages get no compression: a short page is compiled with the same
    restructure-not-compress prompt as a section, under the page's own source."""
    html = "<title>Hours</title><h1>Hours</h1><p>Open 8 to 6, Monday to Saturday.</p>" * 3
    _pocket(monkeypatch, {"hours.html": html})
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert (report.ingested, report.error) == (1, "")
    [prompt] = compiler.prompts
    assert "Restructure, do not compress" in prompt
    assert "COMPRESSES" not in prompt
    [payload] = kb.payloads
    assert payload["article"]["source"] == "site-hours"
    assert payload["article"]["title"] == "Hours"
    assert site.kb_page_index["hours"]["title"] == "Hours"


@pytest.mark.asyncio
async def test_with_an_api_key_kb_still_never_compiles(monkeypatch, kb, compiler):
    """PocketPaw never lets kb-go compile: with ANTHROPIC_API_KEY set, short pages
    and sections alike go through the agent backend and ``ingest --article-json``."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    short = "<title>Hours</title><h1>Hours</h1><p>Open 8 to 6, Monday to Saturday.</p>"
    _pocket(monkeypatch, {"docs/install.html": _DOCS_HTML, "hours.html": short * 3})
    site = _FakeSite()

    report = await kb_ingest.sync_site_knowledge(site)

    assert (report.ingested, report.error) == (2, "")
    assert len(compiler.prompts) == len(_DOCS_TITLES) + 1
    assert len(kb.payloads) == len(_DOCS_TITLES) + 1  # every write is --article-json
