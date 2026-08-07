# tests/ee/sites/test_draft_screenshot.py — a DRAFT site's card gets a picture too
# (SC-2), and getting it can never cost anyone a create, an import, or a build they
# did not ask for.
#
# Created 2026-08-07. Four things are pinned here:
#   * the Cloudflare call — the screenshot endpoint's ``html`` body, the alternative
#     to ``url`` that lets a site with no address be photographed at all, and the
#     exactly-one-of guard so a caller bug surfaces here rather than as a CF 400;
#   * the document — Browser Rendering renders ``html`` at ``about:blank``, where
#     NOTHING relative resolves, so the assembled markup has to be self-contained.
#     These tests pin what gets folded in, what gets dropped, and what is deliberately
#     left alone;
#   * the COST LADDER — the reason this slice does not simply build every draft. An
#     already-built pocket is read off disk, an html pocket needs no build at all,
#     and a never-built ripple/svelte pocket is NOT built unless the deployment opts
#     in. The test that a build does not happen is the important one;
#   * THE RULE (inherited from SC-1) — a capture that fails, at any layer, cannot
#     propagate into the caller. A draft that cannot be photographed shows the card's
#     themed placeholder, silently.
#
# The mutations that break these tests (run via
# ``uv run python scripts/mutate.py --plan tests/mutations/site_draft_screenshot.json``
# — a gate nobody has watched fail is not a gate): dropping the exactly-one-of guard
# in ``capture_screenshot``; making ``build_allowed`` return True unconditionally;
# leaving local ``<link>``/``<script src>`` tags in the inlined document; deleting the
# try/except in ``service._schedule_draft_screenshot`` or in
# ``screenshot.safe_take_draft_screenshot``; and dropping the went-live re-read that
# stops a slow draft shot landing on top of a live one.
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import draft_markup
from pocketpaw_ee.sites import screenshot as screenshot_mod
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

# A real 1x1 PNG — the upload pipeline sniffs magic bytes, so a b"fake" payload
# would make the happy path fail for a reason that has nothing to do with SC-2.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _cf(handler) -> CloudflareClient:
    return CloudflareClient(
        account_id="acct1",
        api_token="tok",
        zone_id="zone1",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )


# --------------------------------------------------------------------------- #
# The Cloudflare call — html instead of url
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_capture_screenshot_sends_html_instead_of_a_url():
    """The whole trick of this slice: a draft has no address, so the markup itself is
    the subject. The body must carry ``html`` and NOT an empty ``url`` — Cloudflare
    would reject a body with both."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    out = await _cf(handler).capture_screenshot(
        html="<html><body>hi</body></html>",
        viewport={"width": 1280, "height": 800},
    )

    assert out == _PNG
    assert seen["body"]["html"] == "<html><body>hi</body></html>"
    assert "url" not in seen["body"]
    assert seen["body"]["viewport"] == {"width": 1280, "height": 800}


@pytest.mark.asyncio
async def test_capture_screenshot_refuses_both_or_neither():
    """Exactly one of url/html. Caught here so a caller bug reads as a caller bug
    instead of an opaque Cloudflare 400 in a fire-and-forget background task nobody
    is watching.

    Mutation: drop the guard and the ``both`` case sends a body with two subjects."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the guard must fire before any request is made")

    with pytest.raises(ValidationError):
        await _cf(handler).capture_screenshot(url="https://x.test/", html="<p>x</p>")
    with pytest.raises(ValidationError):
        await _cf(handler).capture_screenshot()


# --------------------------------------------------------------------------- #
# The document — self-contained, or it renders as unstyled text
# --------------------------------------------------------------------------- #


def _reader(files: dict[str, bytes]):
    return lambda rel: files.get(rel)


def test_a_local_stylesheet_is_folded_into_the_document():
    """``about:blank`` has no origin, so a ``<link href="app.css">`` fetches nothing
    and the page renders unstyled. Mutation: return the link tag unchanged and the
    stylesheet no longer reaches the document."""
    html = '<html><head><link rel="stylesheet" href="/app.css"></head><body>x</body></html>'
    out = draft_markup.inline_document(html, _reader({"app.css": b"body{color:red}"}))

    assert "<style>body{color:red}</style>" in out
    assert "<link" not in out


def test_a_stylesheets_own_url_references_are_folded_in_too():
    """A hero background and the site's webfonts live inside the CSS, not the markup.
    A capture that skips them photographs a different-looking site. The reference is
    resolved relative to the STYLESHEET, not to the document."""
    html = '<head><link rel="stylesheet" href="/assets/app.css"></head>'
    files = {
        "assets/app.css": b'.hero{background:url("./bg.png")}',
        "assets/bg.png": _PNG,
    }
    out = draft_markup.inline_document(html, _reader(files))

    assert "url(data:image/png;base64," in out
    assert "bg.png" not in out


def test_an_already_inline_style_block_keeps_its_url_references_too():
    """SvelteKit inlines critical CSS — which is exactly the CSS the hero needs. A
    hero background left dangling in an inline block undoes the point of inlining
    everything else."""
    html = "<style>.hero{background:url(/img/bg.png)}</style><body>x</body>"
    out = draft_markup.inline_document(html, _reader({"img/bg.png": _PNG}))

    assert "url(data:image/png;base64," in out
    assert "/img/bg.png" not in out


def test_a_local_image_becomes_a_data_uri_and_loses_its_srcset():
    """A ``srcset`` left behind would let the browser prefer a candidate that can
    never load — a broken image, which is the one outcome this slice promises never
    to produce."""
    html = '<img src="hero.png" srcset="hero@2x.png 2x" alt="hero">'
    out = draft_markup.inline_document(html, _reader({"hero.png": _PNG}))

    assert 'src="data:image/png;base64,' in out
    assert "srcset" not in out
    assert 'alt="hero"' in out


def test_local_scripts_are_dropped_and_remote_references_are_left_alone():
    """A client bundle cannot resolve its relative imports with no origin, so it is
    dead weight; the capture is of the page's prerendered RESTING state. Absolute
    http(s) references are a different matter — Cloudflare's browser is on the
    internet and can fetch them, so they ride through untouched."""
    html = (
        '<link rel="stylesheet" href="https://cdn.test/x.css">'
        '<link rel="icon" href="/favicon.ico">'
        '<script type="module" src="/_app/start.js"></script>'
        '<img src="https://cdn.test/logo.png">'
    )
    out = draft_markup.inline_document(html, _reader({}))

    assert 'href="https://cdn.test/x.css"' in out
    assert 'src="https://cdn.test/logo.png"' in out
    assert "_app/start.js" not in out
    assert "favicon.ico" not in out  # a local icon link resolves to nothing — gone


def test_an_unreadable_local_stylesheet_is_dropped_not_left_dangling():
    html = '<link rel="stylesheet" href="/missing.css">'
    assert draft_markup.inline_document(html, _reader({})) == ""


def test_an_asset_over_the_budget_is_left_alone_rather_than_inlined():
    """One pathological asset must not turn a card thumbnail into a multi-megabyte
    upload. Over budget, the reference is simply left un-inlined."""
    big = b"\x89PNG" + b"0" * (draft_markup._MAX_INLINE_BYTES + 1)
    html = '<img src="huge.png">'
    out = draft_markup.inline_document(html, _reader({"huge.png": big}))

    assert out == html


def test_a_traversal_reference_cannot_read_outside_the_static_root(tmp_path):
    """The hrefs come from our own generator, but a source-engine site's markup is
    author-controlled. ``../`` is refused against the resolved root."""
    (tmp_path / "secret.css").write_bytes(b"body{color:red}")
    root = tmp_path / "site"
    root.mkdir()
    read = draft_markup._make_disk_reader(root)

    assert read(draft_markup._norm_rel("../secret.css")) is None
    assert draft_markup._norm_rel("../secret.css") == ""


# --------------------------------------------------------------------------- #
# The cost ladder — the reason this slice does not just build every draft
# --------------------------------------------------------------------------- #


class _NeverBuilds:
    """A generator that fails the test if anything asks it to build."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def build(self, **kw):  # pragma: no cover — reaching it IS the failure
        self.calls.append(kw)
        raise AssertionError("a draft capture must not run a node build here")


def _draft(**over):
    """An in-memory stand-in for a DRAFT Site doc, recording its own ``set``."""
    sets: list[dict] = []

    class _S:
        id = over.get("id", "site-1")
        owner = "u1"
        workspace = "ws1"
        pocket_id = over.get("pocket_id", "pocket-1")
        name = "Bright Smile"
        signed_key = "site_key_x"
        url = over.get("url", "")
        writes = sets

        async def set(self, updates):
            sets.append(updates)

    return _S()


@pytest.mark.asyncio
async def test_an_html_pocket_needs_no_build_at_all(monkeypatch, tmp_path):
    """RUNG 2, and the acceptance case: a zip/from-url import mints an html pocket
    whose served artifact IS its authored source, so the source map is already the
    static tree. Zero subprocesses — which is what makes an imported draft's picture
    worth taking at all."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    pocket = {
        "engine": "html",
        "source": {
            "index.html": '<html><head><link rel="stylesheet" href="s.css">'
            "</head><body><h1>Bright Smile</h1></body></html>",
            "s.css": "h1{color:teal}",
        },
    }

    out = await draft_markup.build_draft_markup(_draft(), generator=_NeverBuilds(), pocket=pocket)

    assert "<style>h1{color:teal}</style>" in out
    assert "Bright Smile" in out


@pytest.mark.asyncio
async def test_a_never_built_ripple_draft_is_not_built_just_for_a_thumbnail(monkeypatch, tmp_path):
    """RUNG 3, defaulted OFF, and the measurement this whole design turns on: a first
    ripple build is ``bun install`` + a Vite/SvelteKit build — tens of seconds of CPU
    in the background at the exact moment the user is chatting with the agent that
    just made the site. The card takes its placeholder instead.

    Mutation: make ``build_allowed`` return True unconditionally and ``_NeverBuilds``
    fires."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    monkeypatch.delenv("PAW_SITES_DRAFT_CAPTURE_BUILD", raising=False)

    out = await draft_markup.build_draft_markup(
        _draft(), generator=_NeverBuilds(), pocket={"engine": "ripple", "rippleSpec": {}}
    )

    assert out == ""


@pytest.mark.asyncio
async def test_an_already_built_pocket_is_read_off_disk(monkeypatch, tmp_path):
    """RUNG 1 — the cheap rung that makes this feature actually land for ripple and
    svelte drafts. A pocket that has ever been previewed, armed or published already
    has its build sitting in the persistent per-pocket dir, so the capture is a few
    file reads and no generator at all."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    built = tmp_path / "pocket-1" / ".svelte-kit" / "cloudflare"
    built.mkdir(parents=True)
    (built / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="/_app/a.css"></head>'
        "<body><h1>Built</h1></body></html>",
        encoding="utf-8",
    )
    (built / "_app").mkdir()
    (built / "_app" / "a.css").write_text("h1{color:navy}", encoding="utf-8")

    out = await draft_markup.build_draft_markup(
        _draft(), generator=_NeverBuilds(), pocket={"engine": "ripple", "rippleSpec": {}}
    )

    assert "<style>h1{color:navy}</style>" in out
    assert "Built" in out


@pytest.mark.asyncio
async def test_the_build_rung_runs_when_the_deployment_opts_in(monkeypatch, tmp_path):
    """The escape hatch exists and is wired: with the env knob on, a never-built
    ripple draft DOES build."""
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    monkeypatch.setenv("PAW_SITES_DRAFT_CAPTURE_BUILD", "1")
    project = tmp_path / "proj"
    static = project / ".svelte-kit" / "cloudflare"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<body><h1>Fresh</h1></body>", encoding="utf-8")

    class _Builds:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def build(self, **kw):
            from pocketpaw_ee.sites.generator_client import BuildResult

            self.calls.append(kw)
            return BuildResult(project_dir=str(project), ripple_version="0.2.0")

    gen = _Builds()
    out = await draft_markup.build_draft_markup(
        _draft(), generator=gen, pocket={"engine": "ripple", "rippleSpec": {"type": "container"}}
    )

    assert "Fresh" in out
    # The SSR fail-gate protects a page about to be served; a draft thumbnail is not.
    assert gen.calls[0]["smoke"] is False
    # Built into the persistent per-pocket dir, so the NEXT capture finds rung 1.
    assert gen.calls[0]["pocket_id"] == "pocket-1"


# --------------------------------------------------------------------------- #
# The capture itself
# --------------------------------------------------------------------------- #


def _markup(value: str):
    """Stand in for ``draft_markup.build_draft_markup`` — these tests are about the
    capture, not about assembling the document (which has its own cases above)."""

    async def _fake(site, **_kw):
        return value

    return _fake


class _FakeCFScreenshot:
    def __init__(self, result: bytes | Exception = _PNG) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def capture_screenshot(self, **kw):
        self.calls.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_a_draft_capture_stores_the_image_and_records_it(monkeypatch, tmp_path):
    """The happy path end to end: render the draft's markup, put the bytes in the
    tenant's blob storage, remember the link on the Site."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup("<h1>Draft</h1>"))
    cf = _FakeCFScreenshot()
    site = _draft()

    url = await screenshot_mod.take_draft_screenshot(site, cloudflare=cf)

    assert cf.calls[0]["html"] == "<h1>Draft</h1>"
    assert "url" not in cf.calls[0]
    assert url.startswith("/api/v1/uploads/")
    assert site.writes == [{"preview_image_url": url}]


@pytest.mark.asyncio
async def test_a_live_site_is_left_to_the_live_capture(monkeypatch):
    """A deployed site's picture belongs to SC-1, which shoots the page visitors can
    actually see. Photographing its markup instead would quietly replace a picture of
    the real thing with a picture of a rehearsal."""
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup("<h1>x</h1>"))
    cf = _FakeCFScreenshot()
    site = _draft(url="https://brew.example.test/")

    assert await screenshot_mod.take_draft_screenshot(site, cloudflare=cf) == ""
    assert cf.calls == []
    assert site.writes == []


@pytest.mark.asyncio
async def test_a_draft_with_no_renderable_markup_is_skipped(monkeypatch):
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup(""))
    cf = _FakeCFScreenshot()
    site = _draft()

    assert await screenshot_mod.take_draft_screenshot(site, cloudflare=cf) == ""
    assert cf.calls == []
    assert site.writes == []


@pytest.mark.asyncio
async def test_a_raising_draft_capture_is_swallowed_by_the_safe_form(monkeypatch):
    """Mutation: remove the try/except in ``safe_take_draft_screenshot`` and this
    raises instead of returning ""."""
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup("<h1>x</h1>"))
    cf = _FakeCFScreenshot(result=RuntimeError("browser rendering quota exceeded"))
    site = _draft()

    assert await screenshot_mod.safe_take_draft_screenshot(site, cloudflare=cf) == ""
    assert site.writes == []


@pytest.mark.asyncio
async def test_a_site_that_went_live_mid_capture_keeps_its_live_picture(monkeypatch, tmp_path):
    """The import flow mints a draft and publishes it seconds later, so the draft
    capture and the live capture that follows it genuinely overlap. Without the
    re-read, the slower draft shot lands on top of the live one and the card shows
    the page as it looked before it was published.

    Mutation: delete the ``_still_a_draft`` check and this test records a write."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup("<h1>x</h1>"))
    monkeypatch.setattr(screenshot_mod, "_still_a_draft", _went_live)
    site = _draft()

    assert await screenshot_mod.take_draft_screenshot(site, cloudflare=_FakeCFScreenshot()) == ""
    assert site.writes == []


async def _went_live(site):
    return False


# --------------------------------------------------------------------------- #
# The wiring — and THE RULE
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_minting_a_draft_schedules_its_capture(beanie_test_db, monkeypatch):
    """The moment a draft becomes a card in the gallery is the moment to try to give
    that card a picture."""
    seen: list = []
    monkeypatch.setattr(sites_service, "_schedule_draft_screenshot", seen.append)

    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_draft", name="Bright Smile"
    )

    assert [s.pocket_id for s in seen] == ["pk_draft"]


@pytest.mark.asyncio
async def test_a_repeat_mint_does_not_re_shoot(beanie_test_db, monkeypatch):
    """``create_draft_site`` is idempotent — a repeat create returns the existing doc
    untouched, and must not spend another render on it. The same early return is what
    stops an already-LIVE site being handed a draft picture."""
    seen: list = []
    monkeypatch.setattr(sites_service, "_schedule_draft_screenshot", seen.append)

    await sites_service.create_draft_site(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    await sites_service.create_draft_site(workspace_id="ws1", user_id="u1", pocket_id="pk1")

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_broken_scheduler_cannot_fail_a_create(beanie_test_db, monkeypatch):
    """THE RULE, at the scheduling layer. ``create_draft_site`` is called from the
    zip/from-url import tail WITHOUT a swallow of its own, so anything escaping here
    fails an import whose files are already safely persisted.

    Mutation: delete the try/except in ``service._schedule_draft_screenshot`` and this
    fails with "scheduler down" instead of returning a draft."""

    def _boom(coro):
        coro.close()
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(screenshot_mod, "_default_screenshot_scheduler", _boom)

    doc = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_boom", name="Bright Smile"
    )

    assert doc.deployed is False


@pytest.mark.asyncio
async def test_a_captured_draft_reaches_the_gallery_list_item(
    beanie_test_db, monkeypatch, tmp_path
):
    """The whole point: a draft that has never been deployed anywhere still carries a
    real image url on its list row, which is what lets its card show the page instead
    of an empty box."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup("<h1>Draft</h1>"))
    monkeypatch.setattr(sites_service, "_cf_client", lambda: _FakeCFScreenshot())

    ran: list = []

    def _inline(coro):
        import asyncio

        ran.append(asyncio.get_running_loop().create_task(coro))

    monkeypatch.setattr(screenshot_mod, "_default_screenshot_scheduler", _inline)

    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_shot", name="Bright Smile"
    )
    for task in ran:
        await task

    rows = await sites_service.list_for_workspace("ws1")
    assert len(rows) == 1
    assert rows[0].deployed is False  # still a draft — nothing was deployed anywhere
    assert (rows[0].preview_image_url or "").startswith("/api/v1/uploads/")


# --------------------------------------------------------------------------- #
# The preview tail — the trigger that makes a ripple/svelte draft's card fill in
# --------------------------------------------------------------------------- #


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


async def _preview(pocket_id: str = "pk_prev"):
    return await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="Bright Smile",
        preview=True,
        _generator=_FakeGenerator(),
        _cloudflare=None,
        _bundle_reader=lambda d: b"x",
        _local_deploy=(lambda site_id, project_dir: f"http://localhost/{site_id}"),
    )


@pytest.mark.asyncio
async def test_a_preview_build_schedules_a_draft_capture(beanie_test_db, monkeypatch):
    """A preview has JUST built the pocket, so the markup is on disk and a capture
    costs a file read rather than the ~16s build the create-time capture declines to
    spend. Without this trigger a ripple/svelte draft never gets art at all under the
    default policy."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    seen: list = []
    monkeypatch.setattr(
        sites_service, "_schedule_draft_screenshot_for_pocket", lambda **kw: seen.append(kw)
    )

    await _preview()

    assert seen == [{"workspace_id": "ws1", "pocket_id": "pk_prev"}]


@pytest.mark.asyncio
async def test_the_preview_capture_goes_by_pocket_not_by_the_transient_doc(
    beanie_test_db, monkeypatch, tmp_path
):
    """A preview's return value is a transient Site-shaped object that is never
    persisted, so recording a picture on it would write to nothing. The capture has to
    resolve the DRAFT doc minted at create — the one the gallery actually lists."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup("<h1>Draft</h1>"))
    monkeypatch.setattr(sites_service, "_cf_client", lambda: _FakeCFScreenshot())

    await sites_service.create_draft_site(workspace_id="ws1", user_id="u1", pocket_id="pk_prev2")
    out = await screenshot_mod.safe_take_draft_screenshot_for_pocket(
        workspace_id="ws1", pocket_id="pk_prev2"
    )

    assert out.startswith("/api/v1/uploads/")
    rows = await sites_service.list_for_workspace("ws1")
    assert (rows[0].preview_image_url or "").startswith("/api/v1/uploads/")


@pytest.mark.asyncio
async def test_previewing_a_live_site_does_not_replace_its_live_picture(
    beanie_test_db, monkeypatch, tmp_path
):
    """Previewing is how an unapproved EDIT is reviewed. Photographing that edit onto
    the gallery card would show visitors' view of the site as something no visitor can
    see. The resolved doc has a url, so the draft capture declines it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup("<h1>Unapproved</h1>"))
    cf = _FakeCFScreenshot()
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)

    doc = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_live"
    )
    await doc.set({"url": "https://brew.example.test/", "deployed": True})

    out = await screenshot_mod.safe_take_draft_screenshot_for_pocket(
        workspace_id="ws1", pocket_id="pk_live"
    )

    assert out == ""
    assert cf.calls == []


@pytest.mark.asyncio
async def test_a_missing_draft_doc_is_not_an_error(beanie_test_db):
    """A pocket with no Site doc (a pre-draft-first row) simply has no card to put a
    picture on."""
    assert (
        await screenshot_mod.safe_take_draft_screenshot_for_pocket(
            workspace_id="ws1", pocket_id="pk_nothing"
        )
        == ""
    )


@pytest.mark.asyncio
async def test_a_broken_scheduler_cannot_fail_a_preview(beanie_test_db, monkeypatch):
    """THE RULE at the preview tail. A preview is the builder's inner loop — it runs on
    every edit — so it is the last thing that may fail over a thumbnail.

    Mutation: delete the try/except in ``service._schedule_draft_screenshot_for_pocket``
    and this fails with "scheduler down"."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")

    def _boom(coro):
        coro.close()
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(screenshot_mod, "_default_screenshot_scheduler", _boom)

    site = await _preview("pk_prev3")

    assert site.deployed is False
