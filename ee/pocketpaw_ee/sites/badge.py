# ee/pocketpaw_ee/sites/badge.py — the FREE-TIER ATTRIBUTION BADGE: the
# server-rendered "Built with PocketPaw" mark every un-upgraded site carries.
#
# This is the enforcement half of the free tier. Removing the badge is what the
# paid per-site plan sells, so the badge is not decoration — it is the only thing
# separating free from paid, and it is treated as a gate, not a garnish.
#
# SHAPE MIRRORS ``paw_bar/embed.py`` DELIBERATELY: both inject into the built
# static tree between the build and the deploy, so the artifact that lands is
# already correct — no second deploy, no post-publish patch. Read that module
# first; this one is its sibling.
#
# ...AND INVERTS ITS FAILURE POSTURE, WHICH IS THE WHOLE POINT. The concierge bar
# is failure-SOFT on purpose ("a site going live matters more than its bar"). The
# badge is failure-CLOSED on every page that will actually ship: an unwritable
# file or a page that will not decode raises ``BadgeInjectionError`` and the
# publish aborts before deploy. A badge that fails open is not an enforcement
# mechanism — the exploit is simply to make injection fail and walk away with a
# free unbadged site.
#
# The ONE case that is not fatal is an empty/missing output root, and that is
# reasoned rather than lenient: this module resolves its root through the same
# ``resolve_static_output_rel`` that ``sites.local_server`` and the Cloudflare
# deploy resolve their UPLOAD source from. No tree means the deploy ships nothing
# from that root either, so there is no state where the badge finds nothing and a
# real page still goes live. See ``inject_into_tree`` — and do not restore a raise
# there without re-checking that the deploy still reads the same root.
#
# SERVER-RENDERED, NOT CSS-HIDEABLE (the spec's words). Two consequences:
#   * the badge is raw HTML in the document, never a ``<script>`` that draws it —
#     a JS-blocked visitor still sees it, and there is no loader to 404.
#   * every property that could hide it (``display``/``visibility``/``opacity``/
#     ``position``/``z-index``/size) is inline AND ``!important``. The realistic
#     attack is not editing our artifact (customers never touch it) — it is a
#     rule in the customer's OWN stylesheet, which we build from. Inline
#     ``!important`` outranks any author stylesheet, which closes that vector.
#
# THE LOCK / LOOK SPLIT. The snippet is a scoped ``<style>`` block plus the
# anchor. The anchor carries the lock inline; the block carries appearance,
# ``:hover`` and ``prefers-reduced-motion``, none of which a style attribute can
# express. Enforcement therefore lives entirely in the part no stylesheet can
# beat, and the part a customer CAN restyle is the part where restyling is
# harmless. The mark is an inline SVG (from docs/public/favicon.svg) rather than
# an ``<img>``: no second request, so a strict CSP or a blocked host cannot leave
# a broken box where the brand goes.
#
# GATED PER SITE, off ``SitePlanTier.badge_removal`` — the site-plan catalog, not
# the workspace plan. Badge removal is a property of the SITE the customer paid
# for, and ``Site.plan_tier`` is the only per-site billing axis that exists today.
# When per-site entitlements land, ``badge_required`` is the one function that
# changes.
#
# Created 2026-08-13 (feat/sites-free-badge): new module. Implements step 1 of the
# pricing build order in docs/design/drafts/2026-08-13-paw-sites-pricing-spec.md
# (see review finding #3 — the badge was specced as the load-bearing free-tier
# mechanism and had no implementation anywhere).
# Updated 2026-08-13 (feat/sites-free-badge): styled to the floating-pill genre the
#   AI-builder badges established (Lovable, Emergent) — dark translucent pill,
#   backdrop blur, hairline border, brand mark, hover. Our own mark and wording,
#   not their trade dress. Structurally this added the lock/look split above:
#   ``build_badge_anchor`` (locked) is now separate from ``build_badge_html``
#   (look + anchor) so the lock can be asserted against the ELEMENT, not the
#   snippet — scanning the whole snippet for ``display:`` finds the look block's
#   copy first and passes while the lock is gone.

from __future__ import annotations

import html
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The attribute stamped on the injected badge. Presence of this string in a page
# means "already badged" — the idempotence check on every re-publish. Same
# contract as ``paw_bar.embed.EMBED_MARKER``, deliberately a different string so
# the two features can never satisfy each other's check.
BADGE_MARKER = "data-paw-badge"

# Where the badge points. NOTE: this is the PRODUCT domain, which is open
# decision #7 in the pricing spec (the sites' own hostname is still unsettled —
# ``*.workers.dev`` today, ``sites.rohitk06.in`` on the proven custom-domain
# lane). This constant is the single place it changes.
BADGE_HREF = "https://pocketpaw.dev"

BADGE_TEXT = "Built with PocketPaw"

_HTML_SUFFIXES = (".html", ".htm")

# THE LOCK. The properties an author stylesheet would reach for to hide the badge,
# inline and ``!important``. A style-attribute ``!important`` is the strongest
# author-origin declaration in the cascade — it beats ``#id .cls {display:none
# !important}`` in the customer's own stylesheet, which is the realistic attack
# (they never touch our built artifact, but we build FROM their source).
#
# ``transform`` is locked, and that is a deliberate cost: it rules out a hover
# LIFT, because a locked transform cannot be re-enabled for one state. Losing
# ``transform:scale(0)`` as a hiding vector is worth more than the animation, so
# the hover below moves colour and shadow instead of position. Do not trade the
# lock back for the lift.
_LOCKED_STYLE = (
    "position:fixed!important;"
    "right:16px!important;"
    "bottom:16px!important;"
    "z-index:2147483647!important;"
    "display:inline-flex!important;"
    "visibility:visible!important;"
    "opacity:1!important;"
    "width:auto!important;"
    "height:auto!important;"
    "max-width:none!important;"
    "max-height:none!important;"
    "min-width:0!important;"
    "clip-path:none!important;"
    "transform:none!important;"
    "filter:none!important;"
    "pointer-events:auto!important;"
    "font-size:12px!important;"
    "text-indent:0!important;"
)

# THE LOOK. Everything below is presentation and degrades safely: a customer who
# restyles it still has a visible badge, because the lock above is what keeps it
# on the page.
#
# Why a <style> block at all, when the lock is inline: ``:hover`` and
# ``prefers-reduced-motion`` cannot be expressed in a style attribute. Splitting
# them is what buys a badge that looks considered instead of stamped on, without
# weakening the part that enforces.
#
# Scoped to the marker attribute so it cannot touch the customer's own elements,
# and every declaration is a single rule — no resets, no ``*`` selectors.
_LOOK_CSS = (
    "a[data-paw-badge]{"
    "align-items:center;gap:7px;padding:7px 13px 7px 8px;border-radius:999px;"
    "background:rgba(15,17,21,.78);"
    "-webkit-backdrop-filter:blur(14px) saturate(140%);"
    "backdrop-filter:blur(14px) saturate(140%);"
    "border:1px solid rgba(255,255,255,.16);"
    "color:#f4f6f8;"
    "font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;"
    "font-weight:500;line-height:1;letter-spacing:.01em;text-decoration:none;"
    "box-shadow:0 1px 2px rgba(0,0,0,.28),0 8px 24px -6px rgba(0,0,0,.45);"
    "transition:background .18s ease,border-color .18s ease,box-shadow .18s ease;"
    "}"
    "a[data-paw-badge]:hover{"
    "background:rgba(22,25,31,.9);"
    "border-color:rgba(255,255,255,.3);"
    "box-shadow:0 1px 2px rgba(0,0,0,.3),0 12px 32px -6px rgba(0,0,0,.55);"
    "}"
    "a[data-paw-badge]:focus-visible{"
    "outline:2px solid #0A84FF;outline-offset:2px;"
    "}"
    "a[data-paw-badge] svg{display:block;flex:none;}"
    "@media(prefers-reduced-motion:reduce){"
    "a[data-paw-badge]{transition:none;}"
    "}"
)

# The paw mark, inlined from docs/public/favicon.svg. Inline because the page is
# served from the edge with no asset pipeline of ours behind it — an <img src>
# would be a second request to a host this site does not own, and a CSP or an
# offline viewer would leave a broken box where the brand goes.
_MARK_SVG = (
    '<svg width="17" height="17" viewBox="0 0 32 32" aria-hidden="true" focusable="false">'
    '<circle cx="16" cy="16" r="15" fill="#0A84FF"/>'
    '<g transform="translate(4,4)" fill="none" stroke="#fff" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="11" cy="4" r="2"/><circle cx="3" cy="7" r="2"/>'
    '<circle cx="19" cy="7" r="2"/><circle cx="7" cy="19" r="2"/>'
    '<circle cx="15" cy="19" r="2"/>'
    '<path d="M8 14v.5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2V14a4 4 0 0 0-6 0z"/>'
    "</g></svg>"
)


class BadgeInjectionError(RuntimeError):
    """A page that should have been badged was not.

    Raised — never swallowed — so ``publish_pocket`` aborts before deploy. See the
    failure-posture note at the top of this module: silently continuing here hands
    out an unbadged free site, which is the exact outcome the badge exists to
    prevent.
    """


def badge_required(*, badge_removal: bool) -> bool:
    """Whether this site must carry the badge.

    One line today, and deliberately its own function: this is the single
    chokepoint every caller asks, so when per-site entitlements arrive (step 2 of
    the build order) the plumbing changes here and nowhere else.

    ``badge_removal`` comes off the site's ``SitePlanTier``. It defaults to False
    everywhere it is resolved, so an unknown/absent/misspelled tier means BADGED —
    fail-closed on the gate as well as on the injection.
    """
    return not badge_removal


def build_badge_anchor() -> str:
    """Just the anchor — the element that carries the lock.

    Split out from ``build_badge_html`` so the lock can be asserted against the
    ELEMENT rather than the whole snippet. Searching the snippet for
    ``display:`` finds whichever copy comes first, and the look block legitimately
    carries unlocked declarations, so a test scanning the whole string measures
    the wrong one and passes while the lock is gone.
    """
    text = html.escape(BADGE_TEXT)
    return (
        f'<a {BADGE_MARKER}="1" href="{BADGE_HREF}" target="_blank" '
        f'rel="noopener noreferrer nofollow" '
        f'aria-label="{text}" '
        f'style="{_LOCKED_STYLE}">{_MARK_SVG}<span>{text}</span></a>'
    )


def build_badge_html() -> str:
    """The full badge snippet: the scoped look, then the locked anchor.

    No script, and nothing fetched — the mark is an inline SVG, so the badge is
    complete the moment the HTML arrives and survives a blocked CDN, a strict CSP
    and a reader with JavaScript off. Those are the same conditions under which a
    script-injected badge quietly disappears, which is why this one is markup.
    """
    return f"<style>{_LOOK_CSS}</style>{build_badge_anchor()}"


def inject_into_html(page: str, badge: str) -> str | None:
    """Return ``page`` with ``badge`` before the last ``</body>``, or ``None`` if
    it is already badged.

    ``None`` (not the unchanged page) so the caller can tell "nothing to do" from
    "rewritten" and skip the write — the steady state on every re-publish.

    The insertion point is the LAST ``</body>``: a page can mention the string in
    an inline script or a code sample, and the real closing tag is the final one.
    A page with no ``</body>`` at all (a fragment, a hand-written partial) gets
    the badge appended rather than skipped — an unbadged page is the one outcome
    this module does not accept.
    """
    if BADGE_MARKER in page:
        return None
    idx = page.lower().rfind("</body>")
    if idx == -1:
        return f"{page}\n{badge}\n"
    return f"{page[:idx]}{badge}\n{page[idx:]}"


def inject_into_tree(root: Path) -> list[Path]:
    """Badge every HTML page under ``root``; return the pages rewritten.

    FAILURE-CLOSED ON EVERY PAGE THAT WILL SHIP, which is what separates this from
    ``paw_bar.embed.inject_into_tree`` (that one logs and skips, because a bar is
    worth less than a deploy):

      * an unreadable, undecodable or unwritable page RAISES — that page exists,
        the deploy will upload it, and it would go out unbadged. This is the whole
        enforcement, and it is exactly the class of failure a customer's own
        content can provoke (a page that will not decode as UTF-8).

    ...but NOT on an empty root, and the distinction is deliberate rather than a
    softening. ``root`` here is ``project_dir / resolve_static_output_rel(...)`` —
    the SAME path ``sites.local_server`` and the Cloudflare deploy resolve their
    upload source from. So if there is no tree, or a tree with no HTML in it, the
    deploy has nothing to ship from that root either: there is no state where the
    badge finds nothing and a real page still goes live. Raising there would not
    protect a single site; it would only turn every stubbed-build test into a
    publish failure. It logs at WARNING and returns instead.

    Do not "restore" the raise without first checking that the deploy still reads
    this same root — the argument above is the only thing making it safe.

    An already-badged page is skipped SILENTLY and counts as a success: that is
    the steady state on re-publish, and logging it would bury real failures under
    a line per page per deploy.
    """
    if not root.is_dir():
        logger.warning(
            "sites badge: no built static tree at %s — nothing to badge (the deploy "
            "reads this same root, so nothing ships from it either)",
            root,
        )
        return []

    badge = build_badge_html()
    changed: list[Path] = []
    seen_pages = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _HTML_SUFFIXES:
            continue
        seen_pages += 1
        try:
            page = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BadgeInjectionError(f"unreadable page {path} — refusing to deploy") from exc
        updated = inject_into_html(page, badge)
        if updated is None:
            continue
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            raise BadgeInjectionError(f"unwritable page {path} — refusing to deploy") from exc
        changed.append(path)

    if seen_pages == 0:
        logger.warning(
            "sites badge: no HTML pages under %s — nothing to badge (same root the "
            "deploy uploads from, so nothing ships from it either)",
            root,
        )

    return changed


__all__ = [
    "BADGE_HREF",
    "BADGE_MARKER",
    "BADGE_TEXT",
    "BadgeInjectionError",
    "badge_required",
    "build_badge_anchor",
    "build_badge_html",
    "inject_into_html",
    "inject_into_tree",
]
