# tests/ee/sites/test_favicon.py — a site's gallery card wears the site's OWN mark,
# and finding that mark can never cost anyone a publish or reach a host we did not
# mean to touch.
#
# Created 2026-09-02. The card's chip was a hard-coded Lucide globe for every site
# alike; ``sites.favicon`` is the lookup that replaces it. Four things are pinned
# here, in the order they can hurt:
#
#   * THE SSRF GATE. This is the one thing in the module that is a security control
#     rather than a nicety, and it is the reason the module exists in the shape it
#     does. Unlike the screenshot lane — which only ever addresses a hostname WE
#     composed — this reads hrefs out of MARKUP, and for an imported site that
#     markup belongs to whoever we imported. The tests below prove that an
#     off-origin href causes NO REQUEST AT ALL (asserting on the transport's call
#     log, not on the return value: "" is also what a failed fetch returns, so a
#     test that only checked the result would pass with the gate deleted).
#   * THE BYTES DECIDE, not the Content-Type. A server that answers `image/png`
#     with an HTML error page must not get that page base64'd onto a card.
#   * THE RANKING. "The favicon" is three or four different tags and a page can
#     declare all of them; the card gets the best one, and rel is a TOKEN LIST so
#     rel="shortcut icon" must match on the token and not on a substring.
#   * THE RULE, shared with the screenshot lane: a lookup that fails, at any layer,
#     cannot propagate into a publish. A card with a globe is the pre-existing card.
#
# The mutations that break these tests (run via scripts/mutate.py — a gate nobody
# has watched fail is not a gate):
#   * make ``_same_origin`` return True unconditionally → the off-origin tests fail
#     with a request that should never have been made;
#   * make ``_sniff_image_mime`` fall back to the declared Content-Type → the
#     lying-server test fails;
#   * delete the ``len(data) > _MAX_ICON_BYTES`` check in ``_accept`` → the oversize
#     test fails;
#   * drop ``_svg_is_inert`` from ``_accept`` → the active-SVG test fails;
#   * tokenise ``rel`` with ``in`` instead of ``split()`` → the shortcut-icon and
#     apple-touch ranking tests fail;
#   * delete the try/except in ``service._schedule_site_favicon`` → the broken
#     scheduler test fails with "scheduler down" instead of a deployed site;
#   * delete the try/except in ``favicon.safe_take_site_favicon`` → the raising
#     lookup test fails;
#   * make ``take_site_favicon`` clear on a FAILED fetch → the "we never saw the
#     page" test fails, which is the case where clearing would wipe a good icon off
#     every card the moment a site had a bad minute.
from __future__ import annotations

import base64

import httpx
import pytest
from pocketpaw_ee.sites import favicon as favicon_mod
from pocketpaw_ee.sites import service as sites_service

# The real inline SVG a Paw site ships: a percent-encoded paw print, single-quoted
# attributes inside a double-quoted href. Held verbatim because the two things most
# likely to break extraction are the quoting and the percent-encoding, and a
# tidied-up fixture would test neither.
_PAW_SVG_URI = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'"
    "%3E%3Crect width='64' height='64' rx='14' fill='%230B0F14'/%3E"
    "%3Ccircle cx='24' cy='22' r='6' fill='%2314E19C'/%3E"
    "%3Ccircle cx='32' cy='46' r='9' fill='%2314E19C'/%3E%3C/svg%3E"
)

# A real 1x1 PNG. It has to be real: ``_accept`` sniffs magic bytes, so b"not-a-png"
# would make a happy-path test pass for the wrong reason.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_SITE_URL = "https://paw-site-abc.pawsites.test/"


def _doc(markup: str, *, head: str = "") -> str:
    return "<!doctype html><html><head>" + head + markup + "</head><body>hi</body></html>"


class _FakeSite:
    """The two things ``take_site_favicon`` touches on a Site: ``url`` and a
    targeted ``set``. Deliberately NOT a real Site doc — the point of these tests is
    the lookup, and a save() that silently worked would hide the module's rule that
    it may only ever ``set`` the one field it owns."""

    def __init__(self, url: str = _SITE_URL, favicon_url: str = "") -> None:
        self.id = "site-1"
        self.url = url
        self.favicon_url = favicon_url
        self.writes: list[dict] = []

    async def set(self, patch: dict) -> None:
        self.writes.append(patch)
        for k, v in patch.items():
            setattr(self, k, v)


def _transport(routes: dict[str, tuple[int, bytes, str]], log: list[str] | None = None):
    """A MockTransport over {path: (status, body, content_type)} that RECORDS every
    url it is asked for. The log is what the SSRF tests assert on: a gate that is
    deleted shows up as a request, and only as a request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        key = request.url.path
        if key not in routes:
            return httpx.Response(404)
        status, body, ctype = routes[key]
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# What the page declares, and which one wins
# --------------------------------------------------------------------------- #


def test_the_inline_svg_a_paw_site_actually_ships_is_found():
    """The reported case, end to end at the parse layer: the icon is a data: URI
    with single-quoted SVG attributes inside a double-quoted href, and there is no
    /favicon.ico anywhere."""
    cands = favicon_mod.extract_icon_candidates(
        _doc('<link rel="icon" href="' + _PAW_SVG_URI + '">')
    )
    assert [c.source for c in cands] == ["svg-icon"]
    assert cands[0].href == _PAW_SVG_URI


def test_rel_is_a_token_list_not_a_substring():
    """rel="shortcut icon" is the token "icon". Matching on substrings would also
    make rel="apple-touch-icon" an "icon" and collapse the ranking below."""
    cands = favicon_mod.extract_icon_candidates(_doc("<link rel='shortcut icon' href='/f.png'>"))
    assert [c.source for c in cands] == ["icon"]


def test_a_scalable_svg_outranks_every_raster_icon():
    cands = favicon_mod.extract_icon_candidates(
        _doc(
            '<link rel="apple-touch-icon" href="/apple.png">'
            '<link rel="icon" sizes="512x512" href="/big.png">'
            '<link rel="icon" type="image/svg+xml" href="/mark.svg">'
        )
    )
    assert cands[0].source == "svg-icon"


def test_apple_touch_beats_an_unsized_icon_but_loses_to_a_bigger_declared_one():
    """An unsized rel=icon is usually the 32px or the .ico, so apple-touch's
    conventional 180 is assumed larger. A rel=icon that DECLARES 512 is not."""
    cands = favicon_mod.extract_icon_candidates(
        _doc('<link rel="icon" href="/small.png"><link rel="apple-touch-icon" href="/a.png">')
    )
    assert [c.source for c in cands] == ["apple-touch-icon", "icon"]

    cands = favicon_mod.extract_icon_candidates(
        _doc(
            '<link rel="icon" sizes="512x512" href="/big.png">'
            '<link rel="apple-touch-icon" href="/a.png">'
        )
    )
    assert [c.source for c in cands] == ["icon", "apple-touch-icon"]


def test_the_windows_tile_and_the_safari_mask_are_found_but_rank_below_real_icons():
    cands = favicon_mod.extract_icon_candidates(
        _doc(
            '<link rel="mask-icon" href="/pinned.svg" color="#000">'
            '<meta name="msapplication-TileImage" content="/tile.png">'
            '<link rel="icon" href="/f.png">'
        )
    )
    assert [c.source for c in cands] == ["icon", "msapplication-tile", "mask-icon"]


def test_og_image_is_not_treated_as_an_icon():
    """A 1200x630 social banner cropped into a 24px chip is worse than the globe it
    would replace, and it was never a claim about the site's mark."""
    cands = favicon_mod.extract_icon_candidates(
        _doc('<meta property="og:image" content="/banner.png">')
    )
    assert cands == []


def test_a_page_that_declares_nothing_has_no_candidates():
    assert favicon_mod.extract_icon_candidates(_doc("<title>x</title>")) == []


# --------------------------------------------------------------------------- #
# THE SSRF GATE — the tests that assert on requests, not on return values
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_off_origin_icon_is_never_fetched():
    """Markup we did not write must not be able to point this server at an address
    of its choosing. ``169.254.169.254`` is the cloud instance-metadata endpoint —
    the payload this gate exists for.

    Asserting on the REQUEST LOG, not on the result: "" is also what a failed fetch
    returns, so a result-only assertion passes with ``_same_origin`` deleted."""
    log: list[str] = []
    transport = _transport({"/latest/meta-data/": (200, _PNG, "image/png")}, log)
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" href="http://169.254.169.254/latest/meta-data/">'),
        base_url=_SITE_URL,
        transport=transport,
    )
    assert got == ""
    assert not any("169.254.169.254" in u for u in log), (
        "the metadata endpoint must never be contacted"
    )
    # Everything the lookup DID reach was the site's own host — the /favicon.ico
    # last resort still runs, and it is same-origin by construction.
    assert all(u.startswith(_SITE_URL) for u in log), log


@pytest.mark.asyncio
async def test_a_third_party_cdn_icon_is_skipped_rather_than_hot_linked():
    """Also the IP-leak case: hot-linking would hand that host the address of
    everyone who opens the gallery."""
    log: list[str] = []
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" href="https://cdn.example.com/f.png">'),
        base_url=_SITE_URL,
        transport=_transport({}, log),
    )
    assert got == ""
    assert not any("cdn.example.com" in u for u in log), "the CDN must never be contacted"
    assert all(u.startswith(_SITE_URL) for u in log), log


@pytest.mark.asyncio
async def test_a_javascript_href_is_refused():
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" href="javascript:alert(1)">'),
        base_url=_SITE_URL,
        transport=_transport({}),
    )
    assert got == ""


@pytest.mark.asyncio
async def test_a_relative_icon_on_the_site_s_own_origin_is_fetched():
    """The other side of the gate: same-origin is the case that must still work, or
    the guard has quietly disabled the feature."""
    log: list[str] = []
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" href="/assets/f.png">'),
        base_url=_SITE_URL,
        transport=_transport({"/assets/f.png": (200, _PNG, "image/png")}, log),
    )
    assert got.startswith("data:image/png;base64,")
    assert log == ["https://paw-site-abc.pawsites.test/assets/f.png"]


# --------------------------------------------------------------------------- #
# What we are willing to put on a card
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_data_uri_icon_is_passed_through_verbatim():
    """Not re-encoded: a percent-encoded SVG is smaller than its base64 form, and
    re-encoding would change a value clients may be caching on equality."""
    got = await favicon_mod.resolve_favicon(_doc('<link rel="icon" href="' + _PAW_SVG_URI + '">'))
    assert got == _PAW_SVG_URI


@pytest.mark.asyncio
async def test_a_data_uri_icon_resolves_with_no_base_url_and_no_network():
    """The draft case. Assembled draft markup has no origin, so a data: URI is the
    only kind of icon that can resolve there — and it must, with no transport."""
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" href="' + _PAW_SVG_URI + '">'), base_url=""
    )
    assert got == _PAW_SVG_URI


def test_an_svg_carrying_script_is_refused():
    """The second of two independent controls — the card renders through <img>,
    where SVG script does not execute. This one keeps active content out of the
    stored field regardless of who renders it later."""
    hostile = b"<svg xmlns='http://www.w3.org/2000/svg'><script>fetch('/x')</script></svg>"
    assert favicon_mod._sniff_image_mime(hostile) == "image/svg+xml"
    assert favicon_mod._accept(hostile) == ""


def test_an_svg_carrying_an_event_handler_is_refused():
    hostile = b"<svg xmlns='http://www.w3.org/2000/svg'><rect onload='x()'/></svg>"
    assert favicon_mod._accept(hostile) == ""


def test_a_plain_svg_is_accepted():
    """The guard above must not be so broad it refuses ordinary marks."""
    assert favicon_mod._accept(b"<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>") == (
        "image/svg+xml"
    )


@pytest.mark.asyncio
async def test_a_server_that_lies_about_the_content_type_is_not_believed():
    """An HTML error page served as image/png. Trusting the header is trusting the
    thing being validated — and on a fetch path the header is set by whatever host
    the imported markup pointed at."""
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" href="/f.png">'),
        base_url=_SITE_URL,
        transport=_transport({"/f.png": (200, b"<!doctype html><h1>404</h1>", "image/png")}),
    )
    assert got == ""


@pytest.mark.asyncio
async def test_an_icon_over_the_cap_is_dropped_not_stored():
    """The cap is what bounds a list response carrying N inline icons. An oversized
    icon means a globe, not a truncated picture."""
    huge = _PNG + b"\x00" * (favicon_mod._MAX_ICON_BYTES + 1)
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" href="/f.png">'),
        base_url=_SITE_URL,
        transport=_transport({"/f.png": (200, huge, "image/png")}),
    )
    assert got == ""


@pytest.mark.asyncio
async def test_a_404_on_the_declared_icon_falls_through_to_the_next_candidate():
    """A page can declare an icon it no longer serves. The ladder exists so that
    does not cost the card its mark."""
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="icon" sizes="512x512" href="/gone.png"><link rel="icon" href="/f.png">'),
        base_url=_SITE_URL,
        transport=_transport({"/f.png": (200, _PNG, "image/png")}),
    )
    assert got.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_favicon_ico_is_the_last_resort_when_the_page_declares_nothing():
    got = await favicon_mod.resolve_favicon(
        _doc("<title>x</title>"),
        base_url=_SITE_URL,
        transport=_transport({"/favicon.ico": (200, _PNG, "image/x-icon")}),
    )
    assert got.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_a_web_app_manifest_s_largest_icon_is_used():
    """Where a PWA keeps its good art. Costs an extra same-origin GET, so it is
    tried only after everything already in the document."""
    manifest = (
        b'{"icons":[{"src":"/i/small.png","sizes":"48x48"},{"src":"/i/big.png","sizes":"512x512"}]}'
    )
    log: list[str] = []
    got = await favicon_mod.resolve_favicon(
        _doc('<link rel="manifest" href="/site.webmanifest">'),
        base_url=_SITE_URL,
        transport=_transport(
            {
                "/site.webmanifest": (200, manifest, "application/manifest+json"),
                "/i/big.png": (200, _PNG, "image/png"),
            },
            log,
        ),
    )
    assert got.startswith("data:image/png;base64,")
    assert any(u.endswith("/i/big.png") for u in log)
    assert not any(u.endswith("/i/small.png") for u in log)


# --------------------------------------------------------------------------- #
# Recording it on the Site — and the absence rule
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_found_icon_is_recorded_with_a_targeted_set():
    site = _FakeSite()
    got = await favicon_mod.take_site_favicon(
        site,
        transport=_transport(
            {
                "/": (
                    200,
                    _doc('<link rel="icon" href="' + _PAW_SVG_URI + '">').encode(),
                    "text/html",
                )
            }
        ),
    )
    assert got == _PAW_SVG_URI
    assert site.writes == [{"favicon_url": _PAW_SVG_URI}]


@pytest.mark.asyncio
async def test_an_unchanged_icon_is_not_rewritten():
    """A republish must not touch the document to store the value already there."""
    site = _FakeSite(favicon_url=_PAW_SVG_URI)
    await favicon_mod.take_site_favicon(
        site,
        transport=_transport(
            {
                "/": (
                    200,
                    _doc('<link rel="icon" href="' + _PAW_SVG_URI + '">').encode(),
                    "text/html",
                )
            }
        ),
    )
    assert site.writes == []


@pytest.mark.asyncio
async def test_a_page_that_no_longer_declares_an_icon_clears_the_stored_one():
    """We READ the page and it declares nothing. That is evidence, so the card
    should stop showing a mark the site has removed."""
    site = _FakeSite(favicon_url=_PAW_SVG_URI)
    got = await favicon_mod.take_site_favicon(
        site, transport=_transport({"/": (200, _doc("<title>x</title>").encode(), "text/html")})
    )
    assert got == ""
    assert site.writes == [{"favicon_url": ""}]


@pytest.mark.asyncio
async def test_a_page_we_could_not_read_leaves_the_stored_icon_alone():
    """The distinction the whole absence rule turns on. A site having a bad minute
    must not wipe the mark off its card — and this is the mutation most likely to
    look harmless: clearing unconditionally passes every other test in this file."""
    site = _FakeSite(favicon_url=_PAW_SVG_URI)
    got = await favicon_mod.take_site_favicon(site, transport=_transport({"/": (503, b"", "")}))
    assert got == ""
    assert site.writes == []
    assert site.favicon_url == _PAW_SVG_URI


@pytest.mark.asyncio
async def test_the_draft_lane_never_clears_an_icon():
    """Assembled draft markup has had its local <link> icons stripped by
    ``draft_markup.inline_document``, so absence there is an artefact of the
    assembly and not a fact about the site."""
    site = _FakeSite(url="", favicon_url=_PAW_SVG_URI)
    got = await favicon_mod.take_site_favicon(
        site, markup=_doc("<title>x</title>"), clear_when_absent=False
    )
    assert got == ""
    assert site.writes == []


@pytest.mark.asyncio
async def test_a_site_with_no_url_and_no_markup_is_skipped_not_guessed_at():
    site = _FakeSite(url="")
    assert await favicon_mod.take_site_favicon(site) == ""
    assert site.writes == []


@pytest.mark.asyncio
async def test_a_raising_lookup_is_swallowed_by_the_safe_form():
    """THE RULE. Mutation: delete the try/except in ``safe_take_site_favicon`` and
    this raises instead of returning ""."""

    class _Boom(_FakeSite):
        async def set(self, patch):  # noqa: ANN001
            raise RuntimeError("mongo down")

    site = _Boom()
    got = await favicon_mod.safe_take_site_favicon(
        site,
        transport=_transport(
            {
                "/": (
                    200,
                    _doc('<link rel="icon" href="' + _PAW_SVG_URI + '">').encode(),
                    "text/html",
                )
            }
        ),
    )
    assert got == ""


# --------------------------------------------------------------------------- #
# Wiring — a live publish looks for the icon, and cannot be hurt by looking
# --------------------------------------------------------------------------- #


@pytest.fixture
def scheduled_favicons(monkeypatch):
    """Record the sites a publish schedules a lookup for, without doing one."""
    seen: list = []
    monkeypatch.setattr(sites_service, "_schedule_site_favicon", seen.append)
    return seen


@pytest.mark.asyncio
async def test_a_live_publish_schedules_an_icon_lookup(
    beanie_test_db, scheduled_favicons, monkeypatch
):
    """The page just changed, so the mark it declares may have too."""
    monkeypatch.setattr(sites_service, "_schedule_site_screenshot", lambda *_a, **_k: None)
    from tests.ee.sites.test_screenshot_capture import _publish

    await _publish()
    assert len(scheduled_favicons) == 1
    assert scheduled_favicons[0].pocket_id == "pocket-1"


@pytest.mark.asyncio
async def test_publish_survives_a_broken_favicon_scheduler(beanie_test_db, monkeypatch):
    """THE RULE at the scheduling layer. This fires from the tail of a LIVE deploy,
    so anything escaping it fails a publish of a site that is already serving.

    Mutation: delete the try/except in ``service._schedule_site_favicon`` and this
    fails with "scheduler down" instead of returning a deployed site."""
    monkeypatch.setattr(sites_service, "_schedule_site_screenshot", lambda *_a, **_k: None)

    def _boom(coro):
        coro.close()
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(favicon_mod, "_default_favicon_scheduler", _boom)
    from tests.ee.sites.test_screenshot_capture import _publish

    site = await _publish()
    assert site is not None
