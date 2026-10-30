# tests/ee/sites/test_crawler_budget_knobs.py — the knobs on
# ``url_crawler.crawl_site``: ``max_pages``, ``fetch_assets`` and ``user_agent``
# narrow the crawl; ``rank_pages`` only reorders which pages the budget buys
# (menu links, then sitemap pages, then other links, shallow paths first).
#
# They exist because the crawler now has TWO consumers with different needs. The
# import endpoint wants a deployable copy of a site and takes every default;
# site-KB grounding (``sites.foreign_grounding``) wants prose only and spends far
# less of a customer's bandwidth to get it. The tests that matter here are the
# ones about what the knobs CANNOT do:
#
#   * ``max_pages`` is clamped to MAX_CRAWL_PAGES, so it can only spend less of
#     the budget and never more — a caller cannot use it to widen a crawl;
#   * the defaults are byte-for-byte the import path's old behaviour, which is
#     what makes this additive rather than a change to a shipped crawl;
#   * ``user_agent`` is the identity announced AND the identity robots.txt is
#     evaluated for. Those two drifting apart would mean obeying a rule about a
#     name we do not send, which is worse than not reading robots at all.
#   * without ``rank_pages`` the walk is plain FIFO BFS with no sitemap fetch,
#     pinned by exact fetch order, so the import path does not move when
#     grounding ranks pages. ``_LinkScan``'s menu tracking is pinned at scan
#     level, including the unclosed-tag cases messy HTML produces.
#
# All network is mocked (httpx.MockTransport + a fake resolver).
from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.sites import url_crawler

pytestmark = pytest.mark.asyncio

_HOST = "example.com"
_IPS = {_HOST: ["93.184.216.34"]}


async def _resolve(host: str) -> list[str]:
    if host not in _IPS:
        raise OSError(f"no DNS for {host}")
    return _IPS[host]


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())


def _wide_site(page_count: int, assets: list[str]) -> dict[str, httpx.Response]:
    """One homepage linking `page_count` children, every page carrying `assets`.
    Flat by design: at depth 1 the page cap, not the depth cap, is what binds."""
    paths = [f"/p{i}/" for i in range(page_count)]
    nav = "".join(f'<a href="{p}">{p}</a>' for p in paths)
    refs = "".join(f'<script src="{a}"></script>' for a in assets)
    routes: dict[str, httpx.Response] = {"/robots.txt": httpx.Response(404, content=b"")}
    routes["/"] = _html(f"<html><body><nav>{nav}</nav>{refs}<p>home</p></body></html>")
    for path in paths:
        routes[path] = _html(f"<html><body>{refs}<p>page {path}</p></body></html>")
    for asset in assets:
        routes[asset] = httpx.Response(
            200, headers={"content-type": "application/javascript"}, content=b"var a=1;"
        )
    return routes


async def _crawl(routes, seen=None, **kw):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        route = routes.get(request.url.path, httpx.Response(404, content=b"nope"))
        if isinstance(route, Exception):
            raise route
        return route

    return await url_crawler.crawl_site(
        f"https://{_HOST}/",
        total_byte_cap=8 * 1024 * 1024,
        transport=httpx.MockTransport(handler),
        resolver=_resolve,
        politeness_delay=0,
        **kw,
    )


async def test_max_pages_narrows_the_crawl():
    result = await _crawl(_wide_site(20, []), max_pages=5)

    assert result.stats.pages_fetched == 5
    assert any("page cap reached (5)" in w for w in result.warnings)


async def test_max_pages_cannot_widen_past_the_module_cap():
    """The clamp is the reason this knob is safe to expose: MAX_CRAWL_PAGES stays
    the ceiling whatever a caller asks for."""
    result = await _crawl(_wide_site(80, []), max_pages=500)

    assert result.stats.pages_fetched == url_crawler.MAX_CRAWL_PAGES


async def test_max_pages_below_one_still_fetches_the_seed():
    result = await _crawl(_wide_site(5, []), max_pages=0)

    assert result.stats.pages_fetched == 1


async def test_fetch_assets_false_issues_no_asset_request():
    assets = ["/a.js", "/b.js", "/c.js"]
    seen: list[httpx.Request] = []

    result = await _crawl(_wide_site(3, assets), seen=seen, fetch_assets=False)

    assert result.stats.assets_fetched == 0
    assert not [r for r in seen if r.url.path in assets]
    assert not [path for path in result.files if path.endswith(".js")]
    assert any("harvests pages only" in w for w in result.warnings)
    assert not any("asset cap reached" in w for w in result.warnings)


async def test_the_defaults_still_fetch_assets():
    """The import path's behaviour is untouched — the knobs are opt-in."""
    assets = ["/a.js", "/b.js", "/c.js"]

    result = await _crawl(_wide_site(3, assets))

    assert result.stats.assets_fetched == 3
    assert sorted(path for path in result.files if path.endswith(".js")) == [
        "a.js",
        "b.js",
        "c.js",
    ]


async def test_the_announced_user_agent_is_the_one_robots_is_read_for():
    """Announce one name and obey another and we are obeying a rule about a name
    we never send. Both ends are asserted from the same crawl."""
    routes = _wide_site(3, [])
    routes["/robots.txt"] = httpx.Response(
        200,
        headers={"content-type": "text/plain"},
        content=b"User-agent: TestCrawler\nDisallow: /p1/\n",
    )
    seen: list[httpx.Request] = []

    result = await _crawl(routes, seen=seen, user_agent="TestCrawler/9.9 (+https://example.test)")

    assert {r.headers.get("user-agent", "") for r in seen} == {
        "TestCrawler/9.9 (+https://example.test)"
    }
    assert result.stats.skipped_by_robots == 1
    assert "p1/index.html" not in result.files


async def test_a_robots_rule_for_a_different_agent_does_not_apply():
    routes = _wide_site(3, [])
    routes["/robots.txt"] = httpx.Response(
        200,
        headers={"content-type": "text/plain"},
        content=b"User-agent: SomeoneElse\nDisallow: /p1/\n",
    )

    result = await _crawl(routes, user_agent="TestCrawler/9.9 (+https://example.test)")

    assert result.stats.skipped_by_robots == 0
    assert "p1/index.html" in result.files


async def test_a_page_that_answers_badly_is_counted_not_only_warned():
    """``pages_failed`` is what a consumer reads to decide whether a harvest is a
    trustworthy answer to "what pages does this site have". Parsing the warning
    prose for that would break the first time the wording changed."""
    routes = _wide_site(3, [])
    routes["/p1/"] = httpx.Response(502, content=b"bad gateway")
    del routes["/p2/"]  # a 404 from the handler's default

    result = await _crawl(routes)

    assert result.stats.pages_failed == 2
    assert result.stats.pages_fetched == 2  # the seed and /p0/


async def test_a_page_whose_fetch_RAISES_is_counted_too():
    """The OTHER failure branch. A page that answers 502 and a page whose
    connection never completes are two separate arms in the crawl loop, and a
    test covering one of them makes the other's counter look guarded when it is
    not — which is how a mutation that deleted it escaped once already."""
    routes: dict[str, object] = dict(_wide_site(3, []))
    routes["/p1/"] = httpx.ConnectError("connection refused")

    result = await _crawl(routes)

    assert result.stats.pages_failed == 1
    assert result.stats.pages_fetched == 3  # the seed, /p0/ and /p2/
    assert any("fetch failed" in w for w in result.warnings)


def _paths(seen: list[httpx.Request]) -> list[str]:
    return [r.url.path for r in seen if r.url.path != "/robots.txt"]


async def test_without_priority_the_walk_is_plain_bfs():
    """Exact fetch order, not a count: the import path takes this default."""
    routes = {
        "/": _html('<a href="/a/">a</a><a href="/b/">b</a>'),
        "/a/": _html('<a href="/a1/">a1</a>'),
        "/b/": _html('<a href="/b1/">b1</a>'),
        "/a1/": _html("<p>a1</p>"),
        "/b1/": _html("<p>b1</p>"),
    }
    seen: list[httpx.Request] = []
    await _crawl(routes, seen)
    assert _paths(seen) == ["/", "/a/", "/b/", "/a1/", "/b1/"]

    seen = []
    await _crawl(_wide_site(20, []), seen, max_pages=5)
    assert _paths(seen) == ["/", "/p0/", "/p1/", "/p2/", "/p3/"]


async def test_ranked_crawl_reads_menu_links_before_shallower_body_links():
    """Only the menu signal can rescue these: the body links are discovered
    first AND sit fewer path segments deep, so neither discovery order nor path
    depth puts the footer menu inside a 5-page budget."""
    body = "".join(f'<a href="/p{i}/">p{i}</a>' for i in range(20))
    menu = (
        '<a href="/join/become-a-member/">Join</a><a href="/membres/inscription/">Inscription</a>'
    )
    routes: dict[str, httpx.Response] = {
        "/robots.txt": httpx.Response(404, content=b""),
        "/": _html(
            f"<html><body><main>{body}</main><footer><nav>{menu}</nav></footer></body></html>"
        ),
    }
    for i in range(20):
        routes[f"/p{i}/"] = _html(f"<p>p{i}</p>")
    routes["/join/become-a-member/"] = _html("<p>join</p>")
    routes["/membres/inscription/"] = _html("<p>inscription</p>")
    seen: list[httpx.Request] = []

    result = await _crawl(routes, seen, max_pages=5, rank_pages=True)

    fetched = [p for p in _paths(seen) if p != "/sitemap.xml"]
    assert fetched[:3] == ["/", "/join/become-a-member/", "/membres/inscription/"]
    assert any("page cap reached (5)" in w for w in result.warnings)


async def test_ranked_crawl_orders_body_links_by_path_depth():
    """No menu at all: /apply (one segment) beats /events/<n> linked before it."""
    events = "".join(f'<a href="/events/{n}/">e{n}</a>' for n in range(10))
    routes: dict[str, httpx.Response] = {
        "/robots.txt": httpx.Response(404, content=b""),
        "/": _html(f'<html><body>{events}<a href="/apply/">Apply</a></body></html>'),
        "/apply/": _html("<p>apply</p>"),
    }
    for n in range(10):
        routes[f"/events/{n}/"] = _html(f"<p>e{n}</p>")
    seen: list[httpx.Request] = []

    await _crawl(routes, seen, max_pages=3, rank_pages=True)

    assert [p for p in _paths(seen) if p != "/sitemap.xml"] == ["/", "/apply/", "/events/0/"]


@pytest.mark.parametrize(
    ("html", "menu"),
    [
        # The happy path, plus role="navigation" on a plain element.
        ('<header><nav><a href="/a/">a</a></nav></header><a href="/b/">b</a>', {"/a/"}),
        ('<div role="navigation"><a href="/a/">a</a></div><a href="/b/">b</a>', {"/a/"}),
        # An unclosed <nav> ends when an element that was open around it closes...
        ('<div class="top"><nav><a href="/a/">a</a></div><div><a href="/b/">b</a></div>', {"/a/"}),
        # ...and at <main>, which can never sit inside a nav, header or footer.
        ('<nav><a href="/a/">a</a><main><a href="/b/">b</a></main>', {"/a/"}),
        # A stray </nav> with nothing open does not flip state either way.
        ('</nav><a href="/b/">b</a><footer><a href="/c/">c</a></footer>', {"/c/"}),
        # Void and unclosed inline tags inside the menu do not leak it.
        (
            '<nav><ul><li><a href="/a/">a<br><img src="x.png"></a>'
            '<li><a href="/c/">c</a></ul></nav><p><a href="/b/">b</a>',
            {"/a/", "/c/"},
        ),
    ],
)
async def test_link_scan_marks_only_links_inside_a_menu(html, menu):
    scan = url_crawler._LinkScan()
    scan.feed(f"<html><body>{html}</body></html>")
    assert scan.menu_links == menu
