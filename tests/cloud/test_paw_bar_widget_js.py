# tests/cloud/test_paw_bar_widget_js.py — GET /paw-bar/widget.js, the public
# glass-bar loader route.
# Created 2026-07-30 (feat/paw-bar-autoembed). Before this route there was no URL
# that served the bar at all: the only one ever advertised pointed at an
# unprovisioned CDN placeholder, so even a hand-pasted embed snippet 404'd. The
# publish path now bakes this URL into customers' deployed HTML, which makes three
# things worth pinning down:
#   * it serves the bundle as JavaScript, cacheably, with NOTHING tenant-specific
#     in it (it is one file shared by every visitor of every site — the credential
#     is presented later, by the iframe it mounts, at /paw-bar/frame);
#   * the path resolves from PAW_BAR_WIDGET_JS when set and from the copy vendored
#     in the package otherwise, so a machine with no sibling checkout still serves
#     a working loader;
#   * a missing bundle is a clean 404 naming the fix, not a FileNotFoundError
#     surfacing as an opaque 500 to whoever is debugging a barless site.
# The vendored default is also syntax-relevant: it is served raw to a foreign page,
# so the last test asserts it stays wrapped in one IIFE and keeps its globals to
# window.PawBar.

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def widget_client():
    """A bare app client. The route reads no DB and needs no store — that IS the
    contract under test, so mounting it alone is the honest harness."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.mark.asyncio
async def test_serves_the_loader_as_javascript(widget_client):
    res = await widget_client.get("/paw-bar/widget.js")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/javascript")
    assert "max-age" in res.headers["cache-control"]
    # It is the loader, not some other asset: it mounts the concierge iframe.
    assert "/paw-bar/frame" in res.text


@pytest.mark.asyncio
async def test_the_loader_carries_nothing_tenant_specific(widget_client):
    """This one file is served byte-identically to every visitor of every site. A
    key, a widget id or a workspace baked into it would leak across tenants; the
    per-site config rides on the embedding <script> tag's data attributes instead."""
    res = await widget_client.get("/paw-bar/widget.js")

    body = res.text
    assert "site_key_" not in body
    assert "workspace" not in body.lower()
    # It READS the config attributes; it must not contain a resolved value.
    assert "data-site-key" in body


@pytest.mark.asyncio
async def test_env_override_wins(widget_client, tmp_path, monkeypatch):
    """PAW_BAR_WIDGET_JS is the seam for serving a freshly built bundle without a
    redeploy."""
    custom = tmp_path / "built-widget.js"
    # write_bytes, not write_text: text mode translates "\n" to "\r\n" on Windows,
    # so the route served CRLF and this assertion failed on a Windows dev box while
    # passing in Linux CI. The route copies bytes; the fixture must write bytes.
    custom.write_bytes(b"/* a newer build */\n")
    monkeypatch.setenv("PAW_BAR_WIDGET_JS", str(custom))

    res = await widget_client.get("/paw-bar/widget.js")

    assert res.status_code == 200
    assert res.text == "/* a newer build */\n"


@pytest.mark.asyncio
async def test_missing_bundle_is_a_clean_404(widget_client, tmp_path, monkeypatch):
    """The operator hitting this is debugging why a live site shows no bar, so the
    message has to name the fix rather than dump a traceback."""
    monkeypatch.setenv("PAW_BAR_WIDGET_JS", str(tmp_path / "nope.js"))

    res = await widget_client.get("/paw-bar/widget.js")

    assert res.status_code == 404
    assert "PAW_BAR_WIDGET_JS" in res.json()["detail"]


def test_default_path_is_the_vendored_copy(monkeypatch):
    """The default must live IN the package: the publish path bakes this URL into
    customers' HTML, so it has to resolve on every machine running the backend, not
    only on a developer's with a sibling checkout."""
    from pocketpaw_ee.paw_bar.router import paw_bar_widget_file

    monkeypatch.delenv("PAW_BAR_WIDGET_JS", raising=False)
    path = paw_bar_widget_file()

    assert path.is_file()
    assert path.parent.name == "static"
    assert path.parent.parent.name == "paw_bar"


def test_vendored_loader_keeps_its_globals_to_itself():
    """It is served raw as a CLASSIC script onto a page we do not own. A bare
    top-level ``const clamp`` would collide with the host site's own globals (and a
    duplicate ``const`` is a hard SyntaxError that kills the whole file), so the
    bundle must stay wrapped and expose only window.PawBar."""
    from pocketpaw_ee.paw_bar.router import paw_bar_widget_file

    source = paw_bar_widget_file().read_text(encoding="utf-8")
    body = [ln for ln in source.splitlines() if ln and not ln.startswith("//")]

    # esbuild emits `"use strict";` then an arrow IIFE; the hand-vendored copy
    # opened with `(function`. Either is a wrapper — what matters is that the
    # file opens with one and declares nothing at column 0 outside it.
    opener = [ln for ln in body if ln != '"use strict";'][0]
    assert opener.startswith("(function") or opener.startswith("(() =>")
    # Nothing declared at column 0 except the wrapper itself and its close.
    stray = [
        ln for ln in body[1:] if (ln.startswith(("const ", "let ", "var ", "function ", "class ")))
    ]
    assert stray == []
    assert "win.PawBar" in source


def test_the_vendored_loader_docks_a_column_rather_than_covering_the_page():
    """The bug this exists for: the vendored copy was hand-transcribed from the
    paw-bar source with the type annotations stripped BY HAND, so it drifted
    silently. It sat a whole session behind, still calling ``goFullscreen()`` on
    ``pawbar:open`` — a real published site opened the messenger over the entire
    viewport and made the page unclickable, while the source it claimed to mirror
    had docked it to a 400px column for days.

    Nothing could see it. paw-bar's own tests load paw-bar's build, not this
    file, and this file is what every published site actually downloads.

    Asserting on the specific behaviours the served widget depends on, rather
    than diffing against a sibling checkout that is not present on a CI box.
    """
    from pocketpaw_ee.paw_bar.router import paw_bar_widget_file

    source = paw_bar_widget_file().read_text(encoding="utf-8")
    code = chr(10).join(ln for ln in source.splitlines() if not ln.lstrip().startswith("//"))

    # The open panel is a sized column. A full-viewport iframe swallows every
    # click on the host page whether or not the app paints a backdrop.
    assert "PANEL_W = 400" in code
    assert "PANEL_MAX_H = 720" in code

    # Both doors into the widget — the app's message and the host's own button —
    # must dock. goFullscreen() survives ONLY in the drag protocol.
    assert code.count("goFullscreen()") == 1, "goFullscreen belongs to drag alone"

    # The opt-in big reading surface, and the box animation, both reached here.
    assert "pawbar:expand" in code
    assert "BOX_MS" in code


def test_the_vendored_loader_speaks_the_frame_protocol_the_app_expects():
    """The loader and the glass app are two halves of one protocol shipped from two
    places, and only this half lives in this repo. The app is built in paw-bar and
    dropped into ``PAWBAR_APP_DIR``; nothing here can see its version. So when the
    loader falls behind, the app keeps sending messages into a copy with no case for
    them and the failures are all silent and all cosmetic-looking.

    That is not hypothetical. The vendored copy sat two loader commits behind while
    the app had already moved, and every symptom was a half of this protocol going
    unanswered:

      * ``scheme.ts`` reads the ``s`` query param off its own iframe URL. Only the
        loader can know the HOST page's colour scheme — the iframe's own
        ``prefers-color-scheme`` is the visitor's OS, not the site's. Without ``s``
        a light site got a dark widget bolted to it.
      * the app posts ``pawbar:overlay`` whenever a menu opens, expecting
        ``pawbar:host-pointerdown`` back when the visitor clicks the page outside
        the frame. An old loader ignores the first and never sends the second, so
        popovers stay open forever.

    Asserting on the wire vocabulary rather than diffing against a sibling checkout,
    which is not present on a CI box. This is the same reasoning as the dock test
    above, applied to the messages instead of the geometry.
    """
    from pocketpaw_ee.paw_bar.router import paw_bar_widget_file

    source = paw_bar_widget_file().read_text(encoding="utf-8")
    code = chr(10).join(ln for ln in source.splitlines() if not ln.lstrip().startswith("//"))

    # The host's scheme rides the frame URL, and it is DERIVED from the host page
    # rather than read off the iframe's own media query.
    assert '"&s=" + hostScheme(win)' in code

    # ...and it stays live: a host that changes theme after load tells the frame.
    assert "pawbar:scheme" in code

    # The overlay handshake, both directions.
    assert "pawbar:overlay" in code, "app announces menus; loader must have a case"
    assert "pawbar:host-pointerdown" in code, "loader must report host clicks back"


def test_the_vendored_loader_is_generated_not_hand_edited():
    """A header that says where it came from is the only thing standing between
    this file and the silent drift above. If someone hand-edits it again, the
    next reader has no way to know which source it is behind."""
    from pocketpaw_ee.paw_bar.router import paw_bar_widget_file

    header = paw_bar_widget_file().read_text(encoding="utf-8").split('"use strict";')[0]

    assert "GENERATED, DO NOT EDIT BY HAND" in header
    assert "loader/src/loader.ts" in header


# --------------------------------------------------------------------------- #
# frame-ancestors port matching
#
# Found 2026-07-30 by framing a real published site: the bar rendered as an empty
# grey box because a CSP host-source with no port matches only the scheme's
# DEFAULT port, so a site served on any other port could not be framed at all.
# --------------------------------------------------------------------------- #


def test_portless_allowlist_entry_permits_any_port():
    """``allowed_origins`` is normalized to bare HOSTS, so without this every site
    not on 80/443 was unframeable — every local, dev and demo deploy."""
    from pocketpaw_ee.paw_bar.router import _frame_ancestors_csp

    assert _frame_ancestors_csp(["localhost", "127.0.0.1"]) == (
        "frame-ancestors localhost:* 127.0.0.1:*"
    )


def test_explicit_port_is_honored_as_written():
    from pocketpaw_ee.paw_bar.router import _frame_ancestors_csp

    assert _frame_ancestors_csp(["example.com:8443"]) == "frame-ancestors example.com:8443"


def test_scheme_and_path_are_still_stripped_and_junk_still_dropped():
    """The port change must not weaken the header-injection guard."""
    from pocketpaw_ee.paw_bar.router import _frame_ancestors_csp

    assert _frame_ancestors_csp(["https://shop.example.com/embed"]) == (
        "frame-ancestors shop.example.com:*"
    )
    # A newline would split the header into extra directives; still refused.
    assert _frame_ancestors_csp(["evil.example\nscript-src *"]) is None
