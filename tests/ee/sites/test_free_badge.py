# tests/ee/sites/test_free_badge.py — a free site cannot ship without its badge.
# Created 2026-08-13 (feat/sites-free-badge). What this pins shut: the badge is the
# ONLY difference between the free tier and the paid per-site tier, so every way it
# could quietly not happen is a way to get the paid product for nothing. Four
# layers, matching where it can break:
#   * Pure injection (no I/O): the badge lands before </body>, a page with no
#     </body> still gets one, a second pass is a no-op, every page in a tree gets
#     one.
#   * The lock: it is server-rendered markup rather than a script, and every
#     property an author stylesheet would use to hide it is inline + !important.
#     A badge that a customer's own CSS can hide is not an enforcement mechanism.
#   * FAILURE-CLOSED on every page that ships: an unreadable/undecodable page and
#     an unwritable page each RAISE, and one bad page aborts the whole publish.
#     This is the inversion from ``_embed_concierge_bar`` (which logs and
#     continues) and it is the heart of the feature — if injection could fail
#     soft, the exploit is to make it fail. An EMPTY root is the one survivable
#     case, because the deploy resolves its upload source from that same root, so
#     nothing ships from it either; those two tests pin the reasoning so nobody
#     "restores" a raise that would protect no site.
#   * The gate: basic is badged, pro/business are not, and an unknown or missing
#     tier is BADGED (fail-closed on the gate too, so a typo can't buy a free
#     removal).

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.billing import site_plans as site_plan_catalog
from pocketpaw_ee.sites import badge
from pocketpaw_ee.sites import service as sites_service

# --------------------------------------------------------------------------- #
# Layer 1 — injection (pure)
# --------------------------------------------------------------------------- #


def test_the_badge_lands_before_the_closing_body():
    page = "<!doctype html><html><body><h1>Atlas AC</h1></body></html>"

    out = badge.inject_into_html(page, badge.build_badge_html())

    assert out is not None
    assert out.index("<a ") < out.index("</body>")
    assert out.endswith("</body></html>")


def test_the_last_closing_body_wins():
    """A page can mention the string in a code sample; the real closing tag is the
    final one. Injecting at the FIRST match buries the badge inside the sample,
    where a <code> block renders it as text rather than a badge.

    The assertion has to be "after the first </body>", not "before the last one" —
    the latter is true even when the badge lands in the sample, which is how the
    ``rfind``->``find`` mutation escaped this test's first version."""
    page = "<html><body><code></body></code><p>real content</p></body></html>"

    out = badge.inject_into_html(page, badge.build_badge_html())

    assert out is not None
    # Count ANCHORS, not markers — the marker is also the look block's selector,
    # so it legitimately appears several times in one injection.
    assert out.count(f"<a {badge.BADGE_MARKER}") == 1
    assert out.index(f"<a {badge.BADGE_MARKER}") > out.lower().index("</body>")


def test_a_page_without_a_body_tag_still_gets_a_badge():
    """Fragments and hand-written partials have no </body>. Appending beats
    skipping — an unbadged page is the one outcome this module does not accept."""
    out = badge.inject_into_html("<h1>Atlas AC</h1>", badge.build_badge_html())

    assert out is not None
    assert badge.BADGE_MARKER in out


def test_rebadging_with_the_SAME_badge_is_a_no_op():
    """The steady state on every re-publish. Returning None (not the unchanged
    page) is what lets the caller skip the write."""
    once = badge.inject_into_html("<body>hi</body>", badge.build_badge_html())
    assert once is not None

    assert badge.inject_into_html(once, badge.build_badge_html()) is None


def test_an_OUTDATED_badge_is_replaced_not_kept():
    """The stale-badge bug, reported live: a site republished after the badge was
    restyled kept the badge it was first given.

    Skip-if-present is correct for the concierge bar, whose snippet never
    changes. The badge is markup we iterate on, and PERF-3 builds into a STABLE
    per-pocket working dir, so an already-injected page comes back around on the
    next publish. Skipping it pins the first badge a site ever got, forever."""
    old = (
        '<a data-paw-badge="1" href="https://pocketpaw.dev" '
        'style="background:#111;">Built with PocketPaw</a>'
    )
    page = f"<html><body><h1>site</h1>{old}</body></html>"

    out = badge.inject_into_html(page, badge.build_badge_html())

    assert out is not None, "an outdated badge must be replaced, not skipped"
    assert "background:#111" not in out
    assert out.count(f"<a {badge.BADGE_MARKER}") == 1
    assert "backdrop-filter" in out


def test_replacing_leaves_the_page_otherwise_intact():
    page = "<html><body><h1>site</h1><p>copy</p></body></html>"
    first = badge.inject_into_html(page, badge.build_badge_html())
    assert first is not None

    # A second pass with a DIFFERENT badge must swap only the badge.
    swapped = badge.inject_into_html(first, "<a data-paw-badge='1'>v2</a>")

    assert swapped is not None
    assert "<h1>site</h1><p>copy</p>" in swapped
    assert swapped.count("<h1>") == 1
    assert "backdrop-filter" not in swapped


def test_a_stale_badge_is_replaced_across_a_whole_tree(tmp_path):
    """The republish path, not just one page."""
    old = '<a data-paw-badge="1" style="background:#111;">Built with PocketPaw</a>'
    for name in ("index.html", "about.html"):
        (tmp_path / name).write_text(f"<body>x{old}</body>", encoding="utf-8")

    changed = badge.inject_into_tree(tmp_path)

    assert len(changed) == 2
    for name in ("index.html", "about.html"):
        page = (tmp_path / name).read_text(encoding="utf-8")
        assert "background:#111" not in page
        assert page.count(f"<a {badge.BADGE_MARKER}") == 1


def test_every_page_in_the_tree_gets_a_badge(tmp_path):
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")
    (tmp_path / "about.html").write_text("<body>about</body>", encoding="utf-8")
    nested = tmp_path / "blog"
    nested.mkdir()
    (nested / "post.html").write_text("<body>post</body>", encoding="utf-8")
    (tmp_path / "styles.css").write_text("body{color:red}", encoding="utf-8")

    changed = badge.inject_into_tree(tmp_path)

    assert len(changed) == 3
    for name in ("index.html", "about.html", "blog/post.html"):
        assert badge.BADGE_MARKER in (tmp_path / name).read_text(encoding="utf-8")
    assert badge.BADGE_MARKER not in (tmp_path / "styles.css").read_text(encoding="utf-8")


def test_a_second_publish_rewrites_nothing_but_still_succeeds(tmp_path):
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")
    badge.inject_into_tree(tmp_path)

    assert badge.inject_into_tree(tmp_path) == []


# --------------------------------------------------------------------------- #
# Layer 2 — the lock (server-rendered, not CSS-hideable)
# --------------------------------------------------------------------------- #


def test_the_badge_is_markup_not_a_script():
    """Server-rendered is the spec's word. A JS-blocked visitor still sees it, and
    there is no loader that can 404 the enforcement away."""
    out = badge.build_badge_html()

    assert "<script" not in out.lower()
    assert "<a " in out


def test_nothing_in_the_badge_is_fetched():
    """A strict CSP or a blocked host must not leave a hole where the brand goes,
    so the mark is an inline SVG and there is no src/href to an asset."""
    out = badge.build_badge_html()

    assert "<svg" in out
    assert "<img" not in out.lower()
    assert 'src="' not in out
    assert "@import" not in out
    # The only URL in the snippet is the badge's own link target.
    assert out.count("http") == 1


@pytest.mark.parametrize(
    "prop",
    [
        "display",
        "visibility",
        "opacity",
        "position",
        "z-index",
        "width",
        "height",
        "transform",
        "filter",
        "clip-path",
        "pointer-events",
    ],
)
def test_every_hiding_vector_is_locked_important(prop):
    """The realistic attack is not editing our artifact — customers never touch it.
    It is a rule in the customer's OWN stylesheet, which we build from. A
    style-attribute !important is the strongest author-origin declaration, so it
    outranks even #id .class {display:none !important}.

    Asserted against the ANCHOR, not the whole snippet: the look block carries
    unlocked declarations by design, and scanning the snippet finds whichever copy
    comes first — which passes while the lock is gone."""
    anchor = badge.build_badge_anchor()

    assert f"{prop}:" in anchor
    start = anchor.index(f"{prop}:")
    assert "!important" in anchor[start : start + 40]


def test_the_lock_lives_on_the_element_not_the_stylesheet():
    """The <style> block is beatable by a later author rule; the style attribute is
    not. If a locked property ever migrates into the block, enforcement silently
    becomes a suggestion.

    The GUARD rule is the one sanctioned exception — a pseudo-element has no
    element to carry a style attribute, so it can only be defended from the
    stylesheet."""
    assert "!important" not in badge._LOOK_CSS
    assert "!important" in badge._GUARD_CSS


# --------------------------------------------------------------------------- #
# Layer 2b — the CHILD lock (the bypass this module shipped with)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "prop",
    ["display", "visibility", "opacity", "font-size", "color", "position", "transform"],
)
def test_the_text_is_locked_too(prop):
    """Two ordinary lines used to defeat the whole feature:

        a[data-paw-badge] span { display:none }

    The anchor stayed fixed, opaque and at max z-index — an empty pill carrying
    nothing. A child with no inline style is styleable by ANY author rule, without
    even needing !important, so locking only the container locked nothing."""
    span = badge.build_badge_anchor().split("<span")[1]

    assert f"{prop}:" in span
    start = span.index(f"{prop}:")
    assert "!important" in span[start : start + 40]


@pytest.mark.parametrize(
    "prop", ["display", "visibility", "opacity", "width", "height", "max-width"]
)
def test_the_mark_is_locked_too(prop):
    """``max-width`` earns its place: a locked ``width:17px`` is still beaten by
    ``max-width:0``, so locking one without the other locks nothing."""
    svg = badge.build_badge_anchor().split("<svg")[1].split(">")[0]

    assert f"{prop}:" in svg
    start = svg.index(f"{prop}:")
    assert "!important" in svg[start : start + 40]


def test_no_element_in_the_badge_is_left_unlocked():
    """The general form of the bug, so a future child element cannot repeat it."""
    anchor = badge.build_badge_anchor()

    for tag in re.finditer(r"<(a|svg|span)\b([^>]*)>", anchor):
        assert "style=" in tag.group(2), f"<{tag.group(1)}> ships with no inline lock"
        assert "!important" in tag.group(2)


def test_a_pseudo_element_overlay_is_neutralised():
    """The one vector with no element to lock — ::after painted over the badge.

    Asserted against the EMITTED snippet, not against ``_GUARD_CSS``: a constant
    can be perfectly correct and simply not reach the page, which is exactly what
    the "guard is dropped" mutation does."""
    out = badge.build_badge_html()

    assert "::before" in out
    assert "::after" in out
    assert "content:none!important" in out


def test_the_hover_moves_colour_not_position():
    """``transform`` is locked, so a hover LIFT cannot work — a locked transform
    can't be re-enabled for one state. The lock is worth more than the animation;
    this pins the trade rather than leaving a dead rule that looks like a bug."""
    out = badge.build_badge_html()
    hover = out.split(":hover{")[1].split("}")[0]

    assert "transform" not in hover
    assert "background" in hover


def test_motion_is_optional():
    assert "prefers-reduced-motion" in badge.build_badge_html()


def test_the_badge_links_out_and_names_itself():
    out = badge.build_badge_html()

    assert badge.BADGE_HREF in out
    assert badge.BADGE_TEXT in out
    assert 'rel="noopener noreferrer nofollow"' in out
    assert "aria-label=" in out
    # The mark is decorative — the accessible name comes from the label + text,
    # so a screen reader announces the badge once, not once per shape in the paw.
    assert 'aria-hidden="true"' in out


# --------------------------------------------------------------------------- #
# Layer 3 — failure-closed (the inversion from the concierge bar)
# --------------------------------------------------------------------------- #


def test_a_missing_build_tree_is_survivable(tmp_path):
    """The deliberate exception to fail-closed, and the reasoning is load-bearing:
    this root is the SAME path the deploy resolves its upload source from, so no
    tree means nothing ships from it either. Raising here would protect no site
    and would turn every stubbed-build publish test into a failure."""
    assert badge.inject_into_tree(tmp_path / "nope") == []


def test_a_tree_with_no_html_is_survivable(tmp_path):
    """Same argument: no HTML at the root the deploy uploads from means no page
    goes live, badged or otherwise."""
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")

    assert badge.inject_into_tree(tmp_path) == []


def test_the_badge_is_pure_ascii():
    """The invariant the latin-1 fallback rests on. If the badge ever gains a
    non-ASCII character — a curly quote in the wording, an en dash in the CSS —
    writing it back into a latin-1 page corrupts that page. This test is what
    stops that, so it is not cosmetic."""
    badge.build_badge_html().encode("ascii")  # raises if anything is non-ASCII


def test_a_non_utf8_page_is_badged_byte_preserving(tmp_path):
    """This used to RAISE, which made an imported windows-1252 site permanently
    unpublishable with no operator override — worse than what it prevented.
    sites/import crawls real sites, so non-UTF-8 pages are ordinary, not hostile."""
    original = "<html><body><p>caf\xe9 r\xe9sum\xe9</p></body></html>".encode("latin-1")
    (tmp_path / "index.html").write_bytes(original)

    changed = badge.inject_into_tree(tmp_path)

    assert len(changed) == 1
    out = (tmp_path / "index.html").read_bytes()
    # the badge landed...
    assert badge.BADGE_MARKER.encode() in out
    # ...and every original byte survived it
    assert b"caf\xe9 r\xe9sum\xe9" in out
    assert out.decode("latin-1").startswith("<html><body><p>")


def test_an_unreadable_page_still_raises(tmp_path, monkeypatch):
    """Undecodable is survivable; UNREADABLE is not. A page we cannot open at all
    would ship unbadged, which is the thing this module exists to prevent."""
    (tmp_path / "index.html").write_text("<body>x</body>", encoding="utf-8")

    def _boom(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    with pytest.raises(badge.BadgeInjectionError):
        badge.inject_into_tree(tmp_path)


def test_an_unwritable_page_raises_rather_than_being_skipped(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")

    def _boom(self, *a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_bytes", _boom)

    with pytest.raises(badge.BadgeInjectionError):
        badge.inject_into_tree(tmp_path)


def test_one_bad_page_stops_the_whole_publish(tmp_path, monkeypatch):
    """Not "the good pages ship badged and the bad one slips through" — the publish
    aborts, so the site does not go live half-enforced."""
    (tmp_path / "a.html").write_text("<body>a</body>", encoding="utf-8")
    (tmp_path / "b.html").write_text("<body>b</body>", encoding="utf-8")

    real = Path.write_bytes

    def _boom_on_b(self, data, *a, **kw):
        if self.name == "b.html":
            raise OSError("read-only file system")
        return real(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", _boom_on_b)

    with pytest.raises(badge.BadgeInjectionError):
        badge.inject_into_tree(tmp_path)


# --------------------------------------------------------------------------- #
# Layer 4 — the gate (which tiers must carry it)
# --------------------------------------------------------------------------- #


def test_the_base_tier_is_badged():
    """This is what free means, and it is the only thing the paid tier sells."""
    tier = site_plan_catalog.get_site_plan(site_plan_catalog.BASE_SITE_PLAN_KEY)

    assert tier is not None
    assert tier.badge_removal is False
    assert badge.badge_required(badge_removal=tier.badge_removal) is True


@pytest.mark.parametrize("key", ["pro", "business"])
def test_the_paid_tiers_may_drop_the_badge(key):
    tier = site_plan_catalog.get_site_plan(key)

    assert tier is not None
    assert tier.badge_removal is True
    assert badge.badge_required(badge_removal=tier.badge_removal) is False


def test_an_unknown_tier_is_badged():
    """``get_site_plan`` deliberately does not substitute a floor, so the caller's
    fail-closed default is what a typo lands on. A misspelled tier must not buy a
    free badge removal."""
    assert site_plan_catalog.get_site_plan("stduio") is None
    assert badge.badge_required(badge_removal=False) is True


def test_a_site_with_no_tier_at_all_is_badged():
    """A FIRST publish reaches the stamper before its Site doc exists, so "no tier"
    is the single most common case on the path — and it must mean free."""
    assert site_plan_catalog.get_site_plan(None) is None
    assert badge.badge_required(badge_removal=False) is True


# --------------------------------------------------------------------------- #
# Layer 5 — the publish path (gate + injection wired together)
# --------------------------------------------------------------------------- #


def _site_doc_returning(monkeypatch, doc):
    """Point ``_stamp_free_badge``'s tier lookup at ``doc`` without a database."""

    async def _find_one(*_a, **_kw):
        return doc

    monkeypatch.setattr(sites_service._SiteDoc, "find_one", _find_one)


class _Doc:
    """A Site row's billing fields. ``subscription_status`` is required here on
    purpose: a fixture carrying only ``plan_tier`` models a site that never paid,
    and pretending that is a paid site is how the cancelled-site hole hides."""

    def __init__(self, plan_tier, subscription_status="active", concierge_enabled=True):
        self.plan_tier = plan_tier
        self.subscription_status = subscription_status
        self.concierge_enabled = concierge_enabled


@pytest.mark.asyncio
async def test_a_first_publish_badges_before_it_deploys(tmp_path, monkeypatch):
    """The case the whole feature turns on: a brand-new site reaches the stamper
    BEFORE its Site doc is inserted, so the tier lookup finds nothing. That must
    resolve to free-and-badged, not to skip."""
    _site_doc_returning(monkeypatch, None)
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")

    await sites_service._stamp_free_badge(
        workspace_id="w1",
        site_id="6512c1f0e4b0a1b2c3d4e5f6",
        project_dir=str(tmp_path),
        engine="html",
    )

    assert badge.BADGE_MARKER in (tmp_path / "index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_basic_tier_site_is_badged(tmp_path, monkeypatch):
    _site_doc_returning(monkeypatch, _Doc("basic"))
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")

    await sites_service._stamp_free_badge(
        workspace_id="w1",
        site_id="6512c1f0e4b0a1b2c3d4e5f6",
        project_dir=str(tmp_path),
        engine="html",
    )

    assert badge.BADGE_MARKER in (tmp_path / "index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_paid_site_ships_clean(tmp_path, monkeypatch):
    """What the customer actually bought — a paid tier WITH a paying subscription."""
    _site_doc_returning(monkeypatch, _Doc("pro", subscription_status="active"))
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")

    await sites_service._stamp_free_badge(
        workspace_id="w1",
        site_id="6512c1f0e4b0a1b2c3d4e5f6",
        project_dir=str(tmp_path),
        engine="html",
    )

    assert badge.BADGE_MARKER not in (tmp_path / "index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "none", "pending"])
async def test_a_paid_tier_that_stopped_paying_gets_its_badge_back(tmp_path, monkeypatch, status):
    """Cancellation sets subscription_status and LEAVES plan_tier on "pro" —
    nothing resets it — so gating the stamp on the tier alone meant a cancelled
    site never saw a badge again. "none" is the same hole and is the LIVE state
    today, because no Dodo product is configured and a paid publish records its
    tier without taking money."""
    _site_doc_returning(monkeypatch, _Doc("pro", subscription_status=status))
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")

    await sites_service._stamp_free_badge(
        workspace_id="w1",
        site_id="6512c1f0e4b0a1b2c3d4e5f6",
        project_dir=str(tmp_path),
        engine="html",
    )

    assert badge.BADGE_MARKER in (tmp_path / "index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_an_unbadgeable_page_aborts_the_publish(tmp_path, monkeypatch):
    """The enforcement, end to end: the stamper does NOT swallow, so publish_pocket
    raises and the site never reaches the deploy."""
    _site_doc_returning(monkeypatch, None)
    (tmp_path / "index.html").write_text("<body>home</body>", encoding="utf-8")

    def _unwritable(self, *a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_bytes", _unwritable)

    with pytest.raises(badge.BadgeInjectionError):
        await sites_service._stamp_free_badge(
            workspace_id="w1",
            site_id="6512c1f0e4b0a1b2c3d4e5f6",
            project_dir=str(tmp_path),
            engine="html",
        )
