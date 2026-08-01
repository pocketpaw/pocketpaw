# tests/cloud/test_paw_bar_reply_sources.py — visible grounding for the concierge.
# Created 2026-07-30: covers the two public grounding surfaces added on
# feat/paw-bar-reply-sources, against the exact contracts the widget team builds on:
#   * The ``sources`` SSE event on POST /paw-bar/chat — at most ONE event, shaped
#     {"sources": [{"title", "url"}]}, max 3 entries, deduped by url, entries need
#     BOTH a non-empty title and url, emitted after the model stream completes and
#     BEFORE stream_end, and emitted not at all (no empty event) when the KB is
#     empty, the search errors, or no hit maps to a synced public page. Hits are
#     filtered to ``Site.kb_article_ids`` so an owner-uploaded private file never
#     surfaces as a public source.
#   * GET /paw-bar/articles — {"articles": [{title, url, snippet}]}, sourced from
#     the site's synced KB pages, cap 20, snippet ≤160 plain chars, behind the SAME
#     front-gate chain as chat (404 unknown widget → 429 rate limit → 401 bad key →
#     403 origin/binding), empty KB → 200 with an empty list, kb errors fail-soft.
# The kb-go subprocess boundary (KnowledgeService.search_articles_for_scope /
# list_articles_for_scope) is patched — the mapping/filter/cap logic under test is
# real; only the external binary is stubbed.

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarBlock, PawBarEvent, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

_VALID_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"
_SITE_URL = "https://brewco.pawsites.dev"


def _spec(pocket_id="pocket-1") -> PawBarSpec:
    return PawBarSpec(
        widget_id="pp_seed",
        pocket_id=pocket_id,
        blocks=[PawBarBlock(type="text", content="Hi from Brew & Co")],
    )


def _widget(**ov) -> PawBarWidget:
    d = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=_spec(),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
        rate_limit_per_min=60,
        per_customer_limit_per_min=10,
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _site(**ov):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        script_name="",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
        url=_SITE_URL,
        kb_article_ids=["site-home", "site-hours", "site-menu"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


class _FakeExecutor:
    """Writes a canned reply + stream_end to the transport so the SSE tail
    terminates without a live agent run."""

    def __init__(self, transport) -> None:
        self.transport = transport
        self.submitted: list = []

    async def submit(self, spec) -> None:
        self.submitted.append(spec)
        await self.transport.append_event(
            spec.run_id, "chunk", {"content": "We open at 8am!", "type": "text"}
        )
        await self.transport.append_event(
            spec.run_id, "stream_end", {"assistant_message_id": "m1", "cancelled": False}
        )


@pytest_asyncio.fixture
async def concierge_client(tmp_path, mongo_db):
    """A public app client backed by a tmp store + Beanie — yields (client, store)."""
    from unittest.mock import patch

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)

    store = PawBarStore(tmp_path / "sources.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, store


def _payload(widget_id: str, **ov) -> dict:
    p = dict(
        widget_id=widget_id,
        signed_key=_VALID_KEY,
        customer_ref="cust-1",
        message="What time do you open?",
    )
    p.update(ov)
    return p


def _sse_events(body: str) -> list[tuple[str, dict | None]]:
    """Parse an SSE body into ordered (event, data) tuples."""
    events: list[tuple[str, dict | None]] = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name:
            events.append((name, data))
    return events


def _stub_run_machinery(monkeypatch):
    """Force the in-memory transport + a canned executor; stub create_run."""
    from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

    transport = InMemoryStreamTransport()
    fake_exec = _FakeExecutor(transport)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: fake_exec)

    async def _fake_create_run(spec):
        return SimpleNamespace(run_id=spec.run_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)
    return fake_exec


def _stub_kb_search(monkeypatch, hits, *, calls: list | None = None, boom: bool = False):
    """Patch the kb-go subprocess boundary for the sources search."""
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    async def _search(scope, query, limit=5):
        if calls is not None:
            calls.append((scope, query, limit))
        if boom:
            raise RuntimeError("kb binary unavailable")
        return hits

    monkeypatch.setattr(KnowledgeService, "search_articles_for_scope", staticmethod(_search))


async def _chat(client, widget_id: str, **payload_ov):
    res = await client.post(
        "/paw-bar/chat", json=_payload(widget_id, **payload_ov), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 200, res.text
    return _sse_events(res.text)


# --------------------------------------------------------------------------- #
# The ``sources`` SSE event
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sources_event_emitted_with_synced_kb(concierge_client, monkeypatch):
    """Hits that map to synced pages ride ONE ``sources`` event, in the exact
    contract shape, searched against the run's own pocket scope."""
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    calls: list = []
    _stub_kb_search(
        monkeypatch,
        [
            {"id": "site-hours", "title": "Opening Hours", "summary": "8am daily"},
            {"id": "site-menu", "title": "Our Menu", "summary": "Coffee and buns"},
        ],
        calls=calls,
    )

    events = await _chat(client, widget.id)

    sources = [d for name, d in events if name == "sources"]
    assert sources == [
        {
            "sources": [
                {"title": "Opening Hours", "url": f"{_SITE_URL}/hours"},
                {"title": "Our Menu", "url": f"{_SITE_URL}/menu"},
            ]
        }
    ]
    # The search ran against the SAME scope the concierge run reads, with the
    # visitor's own message as the query.
    assert calls and calls[0][0] == "pocket:pocket-1"
    assert calls[0][1] == "What time do you open?"


@pytest.mark.asyncio
async def test_sources_event_precedes_stream_end(concierge_client, monkeypatch):
    """CONTRACT: after the model stream, before stream_end — never after it."""
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [{"id": "site-hours", "title": "Opening Hours"}])

    events = await _chat(client, widget.id)

    names = [name for name, _ in events]
    assert names.count("sources") == 1
    assert names.index("chunk") < names.index("sources") < names.index("stream_end")
    assert names[-1] == "stream_end"  # nothing, sources included, follows stream_end


@pytest.mark.asyncio
async def test_sources_capped_at_three_and_deduped_by_url(concierge_client, monkeypatch):
    """Five mappable hits, the first two landing on the same page → the dupe is
    dropped and the event carries exactly three entries."""
    client, store = concierge_client
    await _site(
        kb_article_ids=["site-home", "site-hours", "site-menu", "site-about", "site-contact"]
    )
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(
        monkeypatch,
        [
            {"id": "site-hours", "title": "Opening Hours"},
            {"id": "site-hours", "title": "Opening Hours (dupe)"},  # same url → dropped
            {"id": "site-menu", "title": "Our Menu"},
            {"id": "site-about", "title": "About Us"},
            {"id": "site-contact", "title": "Contact"},  # over the cap → dropped
        ],
    )

    events = await _chat(client, widget.id)

    (payload,) = [d for name, d in events if name == "sources"]
    assert [s["url"] for s in payload["sources"]] == [
        f"{_SITE_URL}/hours",
        f"{_SITE_URL}/menu",
        f"{_SITE_URL}/about",
    ]


@pytest.mark.asyncio
async def test_sources_drop_unsynced_and_untitled_hits(concierge_client, monkeypatch):
    """A hit outside ``Site.kb_article_ids`` (an owner-uploaded file in the same
    pocket scope) and a hit with no title never surface."""
    client, store = concierge_client
    await _site(kb_article_ids=["site-hours"])
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(
        monkeypatch,
        [
            {"id": "internal-price-sheet", "title": "Internal Price Sheet"},  # not synced
            {"id": "site-hours", "title": ""},  # no title
            {"id": "site-hours", "title": "Opening Hours"},
        ],
    )

    events = await _chat(client, widget.id)

    (payload,) = [d for name, d in events if name == "sources"]
    assert payload == {"sources": [{"title": "Opening Hours", "url": f"{_SITE_URL}/hours"}]}


@pytest.mark.asyncio
async def test_no_sources_event_when_kb_is_empty(concierge_client, monkeypatch):
    """No qualifying hit → NOTHING is emitted (no empty ``sources`` event)."""
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [])

    events = await _chat(client, widget.id)

    assert "sources" not in [name for name, _ in events]
    assert [name for name, _ in events][-1] == "stream_end"  # the stream still ends cleanly


@pytest.mark.asyncio
async def test_no_sources_event_when_search_errors(concierge_client, monkeypatch):
    """Fail-soft: a kb failure costs the sources, never the reply."""
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [], boom=True)

    events = await _chat(client, widget.id)

    names = [name for name, _ in events]
    assert "sources" not in names
    assert "chunk" in names and names[-1] == "stream_end"


@pytest.mark.asyncio
async def test_no_sources_event_when_site_has_no_synced_pages(concierge_client, monkeypatch):
    """A site whose pages never synced (empty kb_article_ids) emits nothing —
    and never even hits the kb binary."""
    client, store = concierge_client
    await _site(kb_article_ids=[])
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    calls: list = []
    _stub_kb_search(monkeypatch, [{"id": "site-hours", "title": "Opening Hours"}], calls=calls)

    events = await _chat(client, widget.id)

    assert "sources" not in [name for name, _ in events]
    assert calls == []


@pytest.mark.asyncio
async def test_home_article_maps_to_site_root(concierge_client, monkeypatch):
    """The index page's ``site-home`` article links to the site root, not /home."""
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [{"id": "site-home", "title": "Brew & Co"}])

    events = await _chat(client, widget.id)

    (payload,) = [d for name, d in events if name == "sources"]
    assert payload == {"sources": [{"title": "Brew & Co", "url": f"{_SITE_URL}/"}]}


# --------------------------------------------------------------------------- #
# GET /paw-bar/articles
# --------------------------------------------------------------------------- #


def _stub_kb_list(monkeypatch, articles, *, boom: bool = False):
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    async def _list(scope):
        if boom:
            raise RuntimeError("kb binary unavailable")
        return articles

    monkeypatch.setattr(KnowledgeService, "list_articles_for_scope", staticmethod(_list))


def _articles_params(widget_id: str, **ov) -> dict:
    p = dict(widget_id=widget_id, signed_key=_VALID_KEY)
    p.update(ov)
    return p


@pytest.mark.asyncio
async def test_articles_happy_path(concierge_client, monkeypatch):
    """Synced pages come back as {title, url, snippet}; an owner-uploaded article
    sharing the pocket scope is never listed."""
    client, store = concierge_client
    await _site(kb_article_ids=["site-home", "site-hours"])
    widget = await store.create_widget(_widget())
    _stub_kb_list(
        monkeypatch,
        [
            {"id": "site-home", "title": "Brew & Co", "summary": "A  neighbourhood\ncoffee shop"},
            {"id": "site-hours", "title": "Opening Hours", "summary": "8am to 6pm, daily"},
            {"id": "owner-upload", "title": "Supplier Contract", "summary": "private"},
        ],
    )

    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200
    assert res.json() == {
        "articles": [
            {
                "title": "Brew & Co",
                "url": f"{_SITE_URL}/",
                "snippet": "A neighbourhood coffee shop",
            },
            {
                "title": "Opening Hours",
                "url": f"{_SITE_URL}/hours",
                "snippet": "8am to 6pm, daily",
            },
        ]
    }


@pytest.mark.asyncio
async def test_articles_snippet_is_capped_at_160_plain_chars(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site(kb_article_ids=["site-hours"])
    widget = await store.create_widget(_widget())
    _stub_kb_list(monkeypatch, [{"id": "site-hours", "title": "Hours", "summary": "x" * 400}])

    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200
    (article,) = res.json()["articles"]
    assert len(article["snippet"]) == 160


@pytest.mark.asyncio
async def test_articles_capped_at_twenty(concierge_client, monkeypatch):
    client, store = concierge_client
    ids = [f"site-page-{i}" for i in range(30)]
    await _site(kb_article_ids=ids)
    widget = await store.create_widget(_widget())
    _stub_kb_list(
        monkeypatch, [{"id": i, "title": f"Page {i}", "summary": "text here"} for i in ids]
    )

    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200
    assert len(res.json()["articles"]) == 20


@pytest.mark.asyncio
async def test_articles_empty_kb_is_200_with_empty_list(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site(kb_article_ids=[])
    widget = await store.create_widget(_widget())
    _stub_kb_list(monkeypatch, [])

    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200
    assert res.json() == {"articles": []}


@pytest.mark.asyncio
async def test_articles_kb_error_is_200_with_empty_list(concierge_client, monkeypatch):
    """Fail-soft like the rest of the KB reads — a kb hiccup never 500s a visitor."""
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_kb_list(monkeypatch, [], boom=True)

    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200
    assert res.json() == {"articles": []}


@pytest.mark.asyncio
async def test_articles_unknown_widget_is_404(concierge_client):
    client, _store = concierge_client
    await _site()
    res = await client.get(
        "/paw-bar/articles", params=_articles_params("nope"), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_articles_rate_limit_is_429(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget(rate_limit_per_min=2))
    for _ in range(2):
        await store.record_event(
            PawBarEvent(widget_id=widget.id, type="pawbar_articles_read", customer_ref="cust-1")
        )
    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_articles_bad_key_is_401(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    res = await client.get(
        "/paw-bar/articles",
        params=_articles_params(widget.id, signed_key="short"),
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_articles_wrong_origin_is_403(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    res = await client.get(
        "/paw-bar/articles",
        params=_articles_params(widget.id),
        headers={"Origin": "https://evil.example"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_articles_widget_bound_to_sibling_pocket_is_403(concierge_client):
    client, store = concierge_client
    await _site(pocket_id="pocket-A")
    widget = await store.create_widget(
        _widget(pocket_id="pocket-B", spec=_spec(pocket_id="pocket-B"))
    )
    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_articles_widget_from_other_workspace_is_403(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget(workspace_id="ws-other"))
    res = await client.get(
        "/paw-bar/articles", params=_articles_params(widget.id), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 403


class TestHumanizedTitles:
    """Slug-shaped article ids never reach the visitor as titles (2026-07-30
    rig smoke: chips read "site-services")."""

    def test_slug_titles_humanize(self) -> None:
        from pocketpaw_ee.paw_bar.router import _humanize_article_title

        assert _humanize_article_title("site-home") == "Home"
        assert _humanize_article_title("site-services") == "Services"
        assert _humanize_article_title("site-contact-us") == "Contact Us"
        assert _humanize_article_title("site-") == "Home"
        # A real compiled title passes through untouched.
        assert _humanize_article_title("Our Service Areas") == "Our Service Areas"


class TestSameOriginFrameGets:
    """Same-origin GETs from OUR frame carry no Origin header (browsers omit it)
    — the gates must resolve Sec-Fetch-Site: same-origin as the frame origin
    instead of 403ing (2026-07-30 rig find: the frame's articles fetch and
    decision poll were dead)."""

    async def test_articles_same_origin_no_origin_header_passes(
        self, concierge_client, monkeypatch
    ) -> None:
        client, store = concierge_client
        await _site(kb_article_ids=["site-home"])
        widget = await store.create_widget(_widget())
        _stub_kb_list(monkeypatch, [{"id": "site-home", "title": "Home", "summary": "hi"}])

        res = await client.get(
            "/paw-bar/articles",
            params=_articles_params(widget.id),
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert res.status_code == 200

    async def test_articles_originless_without_fetch_metadata_stays_403(
        self, concierge_client, monkeypatch
    ) -> None:
        client, store = concierge_client
        await _site(kb_article_ids=["site-home"])
        widget = await store.create_widget(_widget())
        _stub_kb_list(monkeypatch, [{"id": "site-home", "title": "Home", "summary": "hi"}])

        res = await client.get("/paw-bar/articles", params=_articles_params(widget.id))
        assert res.status_code == 403

    async def test_articles_cross_site_fetch_metadata_stays_gated(
        self, concierge_client, monkeypatch
    ) -> None:
        # A cross-site GET (Sec-Fetch-Site: cross-site, no Origin) must not
        # inherit the frame's pass — it stays on the fail-closed allowlist gate.
        client, store = concierge_client
        await _site(kb_article_ids=["site-home"])
        widget = await store.create_widget(_widget())
        _stub_kb_list(monkeypatch, [{"id": "site-home", "title": "Home", "summary": "hi"}])

        res = await client.get(
            "/paw-bar/articles",
            params=_articles_params(widget.id),
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert res.status_code == 403
