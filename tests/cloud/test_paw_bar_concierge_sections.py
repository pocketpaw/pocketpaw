# tests/cloud/test_paw_bar_concierge_sections.py — the v2 concierge on sectioned
# site pages, and the per-site knowledge budget.
#
# A long site page is several kb articles (``sites.page_sections``); the page
# index lists them under ``sections``. Pinned here:
#   * ``resolve_page`` reads both index shapes; ``_source_items`` gives a section
#     hit its page url, plus ``#anchor`` when the page's heading had an id;
#   * ``_with_page_article`` hands the model the visitor's page sections that
#     match the query (the first section when none does), within the page share;
#     ``_with_page_siblings`` brings the rest of the top hit's page after it;
#   * ``Site.concierge_knowledge_chars`` (None = 12,000, bounded 4,000..60,000)
#     sets the <knowledge> budget and how many hits are searched; items are cut
#     in rank order, each to min(item cap, what is left);
#   * the owner settings GET/PATCH expose and validate the field.
# ruff: noqa: F811

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    _site as _settings_site,
)
from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    client,
)
from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 — fixtures
    _chat,
    _seed_kb,
    _site,
    _widget,
    concierge_client,
    model,
)

_SECTIONS = [
    {
        "id": "brewkit-requirements",
        "title": "Installing Brewkit › Requirements",
        "source": "site-docs-install#requirements",
        "hash": "h1",
        "anchor": "requirements",
    },
    {
        "id": "brewkit-msi",
        "title": "Installing Brewkit › Install on Windows › With the MSI",
        "source": "site-docs-install#with-the-msi",
        "hash": "h2",
        "anchor": "windows-msi",
    },
    {
        "id": "brewkit-troubleshooting",
        "title": "Installing Brewkit › Troubleshooting",
        "source": "site-docs-install#troubleshooting",
        "hash": "h3",
        "anchor": "",
    },
]
_INDEX = {
    "docs/install": {
        "id": "brewkit-requirements",
        "title": "Installing Brewkit",
        "sections": _SECTIONS,
    },
    "returns": {"id": "returns-policy", "title": "Returns"},  # an old-shape entry
}
_BODIES = {
    "brewkit-requirements": "Brewkit runs on macOS 13, Windows 11 and Ubuntu 22.04.",
    "brewkit-msi": "Run msiexec /i brewkit.msi /qn ACCEPT_EULA=1 for a silent install.",
    "brewkit-troubleshooting": "Error E-101 means the machine did not answer on port 7443.",
}


def _page_site(**ov: Any) -> SimpleNamespace:
    d: dict[str, Any] = dict(
        pocket_id="pocket-1",
        allowed_origins=["brewco.com"],
        kb_page_index=dict(_INDEX),
        url="https://brewco.com",
    )
    d.update(ov)
    return SimpleNamespace(**d)


def _resolve(url: str, site: Any = None):
    from pocketpaw_ee.paw_bar.concierge_runtime import resolve_page

    return resolve_page(_widget(), {"url": url, "title": "x"}, site=site or _page_site())


def _seed_show(monkeypatch) -> list[str]:
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    seen: list[str] = []

    async def _show(scope: str, article_id: str) -> dict:
        seen.append(article_id)
        title = next(s["title"] for s in _SECTIONS if s["id"] == article_id)
        return {"id": article_id, "title": title, "summary": "s", "content": _BODIES[article_id]}

    monkeypatch.setattr(KnowledgeService, "get_article_for_scope", staticmethod(_show))
    return seen


def _item(i: int, size: int, source: str = "pocket:pocket-1"):
    from pocketpaw_ee.paw_bar.concierge_runtime import KnowledgeItem

    return KnowledgeItem(id=f"a{i}", source=source, text="x" * size, score=1.0 / (i + 1))


# --------------------------------------------------------------------------- #
# The page index, both shapes
# --------------------------------------------------------------------------- #


def test_resolve_page_reads_a_sectioned_entry():
    ctx = _resolve("https://brewco.com/docs/install")

    assert ctx is not None and ctx.indexed
    assert ctx.article_id == "brewkit-requirements"
    assert ctx.section_ids == ("brewkit-requirements", "brewkit-msi", "brewkit-troubleshooting")
    assert ctx.title == "Installing Brewkit"


def test_resolve_page_still_reads_an_old_entry():
    ctx = _resolve("https://brewco.com/returns")

    assert ctx is not None and ctx.indexed
    assert ctx.article_id == "returns-policy"
    assert ctx.section_ids == ("returns-policy",)
    assert ctx.title == "Returns"


def test_a_section_hit_cites_its_page_and_anchor():
    from pocketpaw_ee.paw_bar.concierge_runtime import KnowledgeItem, _source_items

    scope = "pocket:pocket-1"
    items = [
        KnowledgeItem(id="brewkit-msi", source=scope, text="## t\nb", score=1.0),
        KnowledgeItem(id="brewkit-troubleshooting", source=scope, text="## t\nb", score=0.5),
        KnowledgeItem(id="returns-policy", source=scope, text="x", score=0.3),
        KnowledgeItem(id="upload", source=scope, text="x", score=0.2),
    ]

    out = _source_items(items, _page_site(), None)

    assert out == [
        {
            "id": "brewkit-msi",
            "title": "Installing Brewkit › Install on Windows › With the MSI",
            "url": "https://brewco.com/docs/install#windows-msi",
        },
        {
            "id": "brewkit-troubleshooting",
            "title": "Installing Brewkit › Troubleshooting",
            "url": "https://brewco.com/docs/install",
        },
        {"id": "returns-policy", "title": "Returns", "url": "https://brewco.com/returns"},
        {"id": "upload", "title": "", "url": ""},
    ]


# --------------------------------------------------------------------------- #
# The visitor's page, by section
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_page_article_is_the_section_that_matches_the_query(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import _with_page_article

    _seed_show(monkeypatch)
    page = _resolve("https://brewco.com/docs/install")

    got = await _with_page_article(
        page, _page_site(), query="silent msiexec install with ACCEPT_EULA, or port 7443"
    )

    assert got is not None and got.chunk is not None
    assert got.chunk.id == "brewkit-msi"
    assert "ACCEPT_EULA" in got.chunk.text
    # The other match follows; "install" is in every breadcrumb and sets none apart,
    # so the requirements section, which matches nothing else, stays out.
    assert [c.id for c in got.extra_chunks] == ["brewkit-troubleshooting"]


@pytest.mark.asyncio
async def test_the_page_article_is_the_first_section_when_nothing_matches(monkeypatch):
    from pocketpaw_ee.paw_bar import concierge_runtime
    from pocketpaw_ee.paw_bar.concierge_runtime import _with_page_article

    _seed_show(monkeypatch)
    page = _resolve("https://brewco.com/docs/install")

    got = await _with_page_article(page, _page_site(), query="opening hours of Brewkit")

    assert got is not None and got.chunk is not None
    assert got.chunk.id == "brewkit-requirements"
    assert got.extra_chunks == ()
    assert len(got.chunk.text) <= concierge_runtime._ITEM_CHARS


@pytest.mark.asyncio
async def test_page_sections_lead_the_knowledge_and_are_not_repeated(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import (
        KnowledgeItem,
        _with_page_article,
        select_knowledge,
    )

    _seed_show(monkeypatch)
    page = await _with_page_article(
        _resolve("https://brewco.com/docs/install"), _page_site(), query="msiexec"
    )
    hits = [
        KnowledgeItem(id="brewkit-troubleshooting", source="pocket:pocket-1", text="dup", score=1),
        KnowledgeItem(id="other", source="pocket:pocket-1", text="other", score=0.5),
    ]

    ids = [i.id for i in select_knowledge(hits, page)]

    assert ids == ["brewkit-msi", "brewkit-troubleshooting", "other"]


@pytest.mark.asyncio
async def test_the_top_section_hit_brings_the_rest_of_its_page(monkeypatch):
    """A question can miss the words of the section that answers it ("shoe sizes"
    against a "Footwear" table). The lead, the first hit carrying the message's
    words, brings its page's other sections, right after it and in page order."""
    from pocketpaw_ee.paw_bar.concierge_runtime import KnowledgeItem, _with_page_siblings

    _seed_show(monkeypatch)
    scope = "pocket:pocket-1"
    hits = [
        KnowledgeItem(id="returns-policy", source=scope, text="## Returns\n30 days.", score=1.0),
        KnowledgeItem(id="brewkit-troubleshooting", source=scope, text="## T\nE-101", score=0.5),
        KnowledgeItem(id="upload", source=scope, text="notes", score=0.3),
    ]

    got, lead = await _with_page_siblings(
        hits, _page_site(), None, budget=12_000, message="what does E-101 mean?"
    )

    assert lead == (1, 4)
    assert [i.id for i in got] == [
        "returns-policy",
        "brewkit-troubleshooting",
        "brewkit-requirements",
        "brewkit-msi",
        "upload",
    ]
    assert "ACCEPT_EULA" in got[3].text


@pytest.mark.asyncio
async def test_the_visitor_page_is_not_expanded_twice(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import KnowledgeItem, _with_page_siblings

    seen = _seed_show(monkeypatch)
    hits = [KnowledgeItem(id="brewkit-msi", source="pocket:pocket-1", text="x", score=1.0)]
    page = _resolve("https://brewco.com/docs/install")

    got, lead = await _with_page_siblings(hits, _page_site(), page, budget=12_000, message="x")

    assert [i.id for i in got] == ["brewkit-msi"]
    assert lead is None
    assert seen == []


# --------------------------------------------------------------------------- #
# The knowledge budget
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 12_000), (20_000, 20_000), (1, 4_000), (10**9, 60_000), ("junk", 12_000)],
)
def test_the_site_budget_reads_its_setting(value, expected):
    from pocketpaw_ee.paw_bar.concierge_runtime import knowledge_chars

    assert knowledge_chars(SimpleNamespace(concierge_knowledge_chars=value)) == expected
    assert knowledge_chars(SimpleNamespace()) == 12_000


def test_the_budget_decides_how_many_items_fit():
    from pocketpaw_ee.paw_bar.concierge_runtime import select_knowledge

    items = [_item(i, 2_000) for i in range(10)]

    assert len(select_knowledge(items, budget=4_000)) == 2
    assert len(select_knowledge(items, budget=20_000)) == 10
    assert len(select_knowledge(items)) == 6  # the 12,000 default


def test_items_are_cut_in_rank_order_to_what_is_left():
    """Each item gets min(item cap, remaining): the top hits arrive whole and the
    next one is cut to the rest of the budget instead of being dropped."""
    from pocketpaw_ee.paw_bar import concierge_runtime
    from pocketpaw_ee.paw_bar.concierge_runtime import select_knowledge

    cap = concierge_runtime._ITEM_CHARS
    items = [_item(i, cap) for i in range(3)] + [_item(3, 100)]

    got = select_knowledge(items, budget=2 * cap + 1_500)

    assert [len(i.text) for i in got] == [cap, cap, 1_500]
    # Left-overs too small to be useful end the list.
    assert [i.id for i in select_knowledge(items, budget=2 * cap + 100)] == ["a0", "a1"]


def test_the_search_depth_follows_the_budget():
    from pocketpaw_ee.paw_bar.concierge_runtime import _TOP_K, _top_k

    assert _top_k(12_000) == _TOP_K
    assert _top_k(4_000) == _TOP_K
    assert _top_k(40_000) == 20
    assert _top_k(60_000) == 20


@pytest.mark.asyncio
async def test_the_site_budget_changes_what_reaches_the_prompt(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    _seed_kb(
        monkeypatch,
        {
            "pocket:pocket-1": [
                {"id": f"k{i}", "title": f"K{i}", "summary": "", "content": "y" * 1_500}
                for i in range(20)
            ]
        },
    )
    widget = await store.create_widget(_widget())

    def _ids() -> list[str]:
        return re.findall(r'<item id="([^"]*)"', model.user_prompt())

    site = await _site()
    await _chat(client, widget.id)
    default_ids = _ids()

    site.concierge_knowledge_chars = 40_000
    await site.save()
    await _chat(client, widget.id)
    wide_ids = _ids()

    site.concierge_knowledge_chars = 4_000
    await site.save()
    await _chat(client, widget.id)
    narrow_ids = _ids()

    assert len(default_ids) == 6
    assert len(wide_ids) == 20
    assert len(narrow_ids) == 3  # two whole, the third cut to what is left


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_expose_the_knowledge_budget_default_unset(client):
    c, _store = client
    site = await _settings_site()

    res = await c.get(f"/paw-bar/admin/site/{site.id}/settings")

    assert res.status_code == 200
    assert res.json()["concierge_knowledge_chars"] is None


@pytest.mark.asyncio
async def test_settings_patch_sets_and_resets_the_knowledge_budget(client):
    c, _store = client
    site = await _settings_site()
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(url, json={"concierge_knowledge_chars": 30_000})
    assert res.status_code == 200, res.text
    assert res.json()["concierge_knowledge_chars"] == 30_000
    assert (await c.get(url)).json()["concierge_knowledge_chars"] == 30_000

    # Another field's PATCH leaves it alone; an explicit null puts the default back.
    await c.patch(url, json={"concierge_greeting": "Hi"})
    assert (await c.get(url)).json()["concierge_knowledge_chars"] == 30_000
    res = await c.patch(url, json={"concierge_knowledge_chars": None})
    assert res.json()["concierge_knowledge_chars"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [3_999, 60_001, 0, -5, "lots", 12_000.5])
async def test_settings_patch_rejects_an_out_of_range_budget(client, value):
    c, _store = client
    site = await _settings_site()
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(url, json={"concierge_knowledge_chars": value})

    assert res.status_code == 422
    assert (await c.get(url)).json()["concierge_knowledge_chars"] is None


@pytest.mark.asyncio
async def test_a_section_hit_brings_its_page_in_a_real_turn(concierge_client, model, monkeypatch):
    client, store = concierge_client
    _seed_kb(
        monkeypatch,
        {
            "pocket:pocket-1": [
                {
                    "id": "brewkit-troubleshooting",
                    "title": "Installing Brewkit › Troubleshooting",
                    "summary": "",
                    "content": _BODIES["brewkit-troubleshooting"],
                }
            ]
        },
    )
    _seed_show(monkeypatch)
    await _site(kb_page_index=dict(_INDEX), url="https://brewco.com")
    widget = await store.create_widget(_widget())

    await _chat(client, widget.id, message="what does error E-101 mean?")

    ids = re.findall(r'<item id="([^"]*)"', model.user_prompt())
    assert ids == ["brewkit-troubleshooting", "brewkit-requirements", "brewkit-msi"]


@pytest.mark.asyncio
async def test_the_best_matching_section_leads_not_the_first_in_page_order(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import _with_page_article

    _seed_show(monkeypatch)
    page = _resolve("https://brewco.com/docs/install")

    got = await _with_page_article(page, _page_site(), query="silent msiexec on windows")

    assert got is not None and got.chunk is not None
    assert [got.chunk.id, *(c.id for c in got.extra_chunks)] == [
        "brewkit-msi",
        "brewkit-requirements",
    ]


# --------------------------------------------------------------------------- #
# The question beats the page
# --------------------------------------------------------------------------- #

_HOME_SECTIONS = [
    {
        "id": f"home-{n}",
        "title": f"Home › {n}",
        "source": f"site-home#{n}",
        "hash": "h",
        "anchor": "",
    }
    for n in ("hero", "shop-by-kit", "fine-print", "reviews")
]
_GUIDE_SECTIONS = [
    {
        "id": f"guide-{n}",
        "title": f"Size guide › {n}",
        "source": f"site-size-guide#{n}",
        "hash": "h",
        "anchor": n,
    }  # fmt: skip
    for n in ("jackets", "socks", "footwear")
]
_HOME_INDEX = {
    "": {"id": "home-hero", "title": "Home", "sections": _HOME_SECTIONS},
    "size-guide": {"id": "guide-jackets", "title": "Size guide", "sections": _GUIDE_SECTIONS},
}
_HOME_BODIES = {
    # Each home section mentions sizes in passing: a weak match for "shoe sizes".
    "home-hero": "Gear for every trail, trail shoes in every size. " * 25,
    "home-shop-by-kit": "Shop jackets, packs and trail shoes, all sizes in stock. " * 20,
    "home-fine-print": "Free size exchanges on boots and shoes within 60 days. " * 20,
    "home-reviews": "Great fit, true to size, said Dana. " * 20,
    "guide-jackets": "Chest 38-40 in is a size M jacket.",
    "guide-socks": "Merino Trail Sock M fits men's 6-8.5.",
    "guide-footwear": "| US men's | EU | Foot length (cm) |\n| 10 | 44 | 28.0 |",
}


def _seed_home(monkeypatch) -> None:
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    titles = {s["id"]: s["title"] for s in _HOME_SECTIONS + _GUIDE_SECTIONS}

    async def _show(scope: str, article_id: str) -> dict:
        return {"id": article_id, "title": titles[article_id], "summary": "s",
                "content": _HOME_BODIES[article_id]}  # fmt: skip

    monkeypatch.setattr(KnowledgeService, "get_article_for_scope", staticmethod(_show))
    # kb ranks the home sections first: the page title rides in the query.
    _seed_kb(
        monkeypatch,
        {
            "pocket:pocket-1": [
                {"id": i, "title": titles[i], "summary": "", "content": _HOME_BODIES[i]}
                for i in ("home-hero", "home-shop-by-kit", "home-fine-print", "guide-socks")
            ]
        },
    )


@pytest.mark.asyncio
async def test_from_the_home_page_the_answering_section_beats_the_page(
    concierge_client, model, monkeypatch
):
    """Base PR #2397 answered "shoe sizes" with the chart from the home page.
    Sections must not do worse: the answering page's section comes before the
    visitor's weakly matching home sections, which share a quarter of the budget."""
    client, store = concierge_client
    _seed_home(monkeypatch)
    await _site(kb_page_index=dict(_HOME_INDEX), url="https://brewco.com")
    widget = await store.create_widget(_widget())

    await _chat(
        client,
        widget.id,
        message="tell me guide for shoe sizes",
        page={"url": "https://brewco.com/", "title": "Home"},
    )

    ids = re.findall(r'<item id="([^"]*)"', model.user_prompt())
    assert ids[:3] == ["guide-socks", "guide-jackets", "guide-footwear"]
    home = [i for i in ids if i.startswith("home-")]
    assert home, ids
    assert ids.index("guide-footwear") < min(ids.index(i) for i in home)
    # Three home sections match "shoe" (3,500 characters between them); behind
    # the lead they share a quarter of the budget.
    item_re = r'<item id="home-[^"]*"[^>]*>\n(.*?)\n</item>'
    bodies = re.findall(item_re, model.user_prompt(), re.S)
    assert len(bodies) >= 2
    assert sum(len(b) for b in bodies) <= 12_000 // 4


@pytest.mark.asyncio
async def test_a_deictic_question_keeps_the_visitors_page_first(
    concierge_client, model, monkeypatch
):
    """'how much is this?' names nothing: the page the visitor is on leads."""
    client, store = concierge_client
    _seed_home(monkeypatch)
    await _site(kb_page_index=dict(_HOME_INDEX), url="https://brewco.com")
    widget = await store.create_widget(_widget())

    await _chat(
        client,
        widget.id,
        message="how much is this?",
        page={"url": "https://brewco.com/", "title": "Home"},
    )

    ids = re.findall(r'<item id="([^"]*)"', model.user_prompt())
    assert ids[0] == "home-hero"
