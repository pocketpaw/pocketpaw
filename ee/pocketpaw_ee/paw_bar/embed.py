# ee/pocketpaw_ee/paw_bar/embed.py — grow the concierge onto a published Paw Site.
#
# Created 2026-07-30 (feat/paw-bar-autoembed): a site we generate, with a
# concierge we auto-provisioned, still shipped with NO concierge on it. The bar
# was embedded ONLY by a snippet the dashboard showed for a human to copy-paste,
# and nothing in the publish path ever wrote it into the built HTML. This module
# is the missing half: the ONE definition of the embed snippet, the ONE rule for
# when a site earns one, and the injection into a built static tree that
# ``sites.service._deploy_site_doc`` runs just before it deploys.
#
# Three things live here and nowhere else:
#
#   * ``EMBED_MARKER`` — the attribute stamped on the injected ``<script>``. It is
#     the idempotence guard: a re-publish that finds the marker already in a page
#     leaves it alone, so republishing a site ten times still yields exactly one
#     script tag. Guarding on the marker (not on the whole snippet) means the guard
#     still holds after the URL or the widget id inside the snippet changes.
#   * ``build_embed_snippet`` — the snippet itself. Every value that reaches markup
#     is HTML-escaped; the key and widget id are server-minted, but a value flowing
#     into an attribute gets escaped on principle, not on provenance.
#   * ``concierge_snippet`` — the FOUR gates a site must pass to earn a bar
#     (concierge on, a real embed key, a paw-bar widget for the pocket, an agent
#     bound to it). The widget lookup reuses ``agent_provisioning.site_widget`` —
#     the same resolution the provisioner runs — rather than forking a second
#     lookup that could drift from it.
#
# The script the snippet loads is the glass-bar LOADER served by
# ``GET /paw-bar/widget.js`` (``router.paw_bar_widget_file``). It reads its config
# off its own tag (``data-site-key`` / ``data-widget-id`` / ``data-endpoint``) and
# mounts the concierge iframe against ``/paw-bar/frame``. The URL is derived from
# the API base the site's own capture endpoint already uses, never a CDN host — a
# locally served site gets a working localhost URL, and there is no second place
# to keep in sync when the deploy moves.

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

logger = logging.getLogger(__name__)

# Stamped on the injected script tag; the idempotence guard for a re-publish.
EMBED_MARKER = "data-paw-bar-embed"

# Path of the loader route, relative to the API base (the paw_bar router is
# mounted under /api/v1, and ``_capture_base()`` already carries that mount, so
# joining the two yields the real URL wherever the API is hosted).
WIDGET_JS_PATH = "/paw-bar/widget.js"

# Page suffixes the injection walks. Deliberately just HTML: the snippet is a
# ``<script>`` tag and belongs in a document, not in a JSON/asset file.
_HTML_SUFFIXES = (".html", ".htm")


def widget_js_url(api_base: str) -> str:
    """The public loader URL for an API base (``<base>/paw-bar/widget.js``)."""
    return f"{api_base.rstrip('/')}{WIDGET_JS_PATH}"


def build_embed_snippet(*, api_base: str, site_key: str, widget_id: str) -> str:
    """The concierge embed snippet for one site.

    ``async`` so the loader never blocks the visitor's page: the tag is written at
    the end of ``<body>``, so ``document.body`` exists by the time it runs whether
    the browser executes it inline or after the parse. ``data-endpoint`` is set
    EXPLICITLY rather than left for the loader to derive from its own ``src``,
    because the loader's fallback assumes the API is mounted at ``/api/v1`` on the
    script's origin — true today, but the caller already knows the real base and
    passing it removes the assumption.
    """
    return (
        "<!-- Paw Bar concierge (embedded at publish) -->\n"
        f'<script src="{escape(widget_js_url(api_base), quote=True)}" '
        f'{EMBED_MARKER}="1" '
        f'data-site-key="{escape(site_key, quote=True)}" '
        f'data-widget-id="{escape(widget_id, quote=True)}" '
        f'data-endpoint="{escape(api_base.rstrip("/"), quote=True)}" '
        "async></script>"
    )


def inject_into_html(page: str, snippet: str) -> str | None:
    """Return ``page`` with ``snippet`` before ``</body>``, or ``None`` if it is
    already embedded.

    ``None`` (rather than the unchanged page) so the caller can tell "nothing to do"
    from "rewritten" and skip the write. The insertion point is the LAST ``</body>``
    — a page can mention the string in an inline script or a code sample, and the
    real closing tag is the final one. A page with no ``</body>`` at all (a
    fragment, a hand-written partial) gets the snippet appended: a script tag at
    the end of a document still runs.
    """
    if EMBED_MARKER in page:
        return None
    idx = page.lower().rfind("</body>")
    if idx == -1:
        return f"{page}\n{snippet}\n"
    return f"{page[:idx]}{snippet}\n{page[idx:]}"


def inject_into_tree(root: Path, snippet: str) -> list[Path]:
    """Inject ``snippet`` into every HTML page under ``root``; return what changed.

    Walks the built static tree (the SvelteKit adapter output for ripple/svelte,
    the project root itself for html — the caller resolves which via
    ``engines.static_output_rel``), so a multi-page site gets a concierge on every
    page rather than only its index. A page that is unreadable, not decodable as
    UTF-8, or unwritable is skipped with a log line instead of aborting the walk:
    one odd file must not cost the other pages their bar. An ALREADY-embedded page
    is skipped SILENTLY — that is the steady state on every re-publish, so logging
    it would bury the real failures under a line per page per deploy.
    """
    changed: list[Path] = []
    if not root.is_dir():
        logger.warning("paw-bar embed: no built static tree at %s — nothing to inject", root)
        return changed
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _HTML_SUFFIXES:
            continue
        try:
            page = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("paw-bar embed: could not read %s — skipping", path, exc_info=True)
            continue
        updated = inject_into_html(page, snippet)
        if updated is None:
            continue
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError:
            logger.warning("paw-bar embed: could not write %s — skipping", path, exc_info=True)
            continue
        changed.append(path)
    return changed


async def concierge_snippet(
    *,
    workspace_id: str,
    pocket_id: str,
    site_key: str,
    api_base: str,
    concierge_enabled: bool,
    concierge_entitled: bool = True,
) -> str:
    """The snippet this site has earned, or ``""`` when it has not.

    Five gates, all of which must pass — a site that fails any of them is published
    exactly as it was before this module existed:

      0. ``concierge_entitled`` — the site's PLAN sells a concierge
         (feat/sites-concierge-entitlement). Same effect as the owner's switch and
         for the same reason: the built page ships with no bar rather than a bar
         that would 403 every visitor at runtime. Defaults True so the only caller
         that must think about billing is the publish path that resolves it; every
         test and internal caller keeps the pre-billing behaviour.
      1. ``concierge_enabled`` — the owner's kill switch. Off means no bar, and a
         re-publish with it off is how an owner takes an embedded bar back off their
         site (the marker is only ever written, never re-written, so the page it was
         injected into is regenerated clean by the next build).
      2. A non-empty ``site_key`` — the embed key IS the credential the loader
         presents; without one the frame endpoint would 401 every visitor.
      3. A paw-bar widget for the site's pocket, resolved through
         ``agent_provisioning.site_widget`` (the provisioner's own lookup).
      4. A non-empty ``agent_id`` on that widget — an unbound widget's chat 409s, so
         a bar for it would render and then refuse to answer, which is worse than no
         bar at all.
    """
    if not concierge_enabled or not concierge_entitled or not site_key or not pocket_id:
        return ""

    from pocketpaw_ee.paw_bar.agent_provisioning import site_widget

    widget = await site_widget(pocket_id, workspace_id)
    if widget is None:
        return ""
    widget_id = str(getattr(widget, "id", "") or "")
    if not widget_id or not (getattr(widget, "agent_id", "") or ""):
        return ""

    return build_embed_snippet(api_base=api_base, site_key=site_key, widget_id=widget_id)


def deployed_host(url: str) -> str:
    """The bare host of a deployed site URL, or ``""``.

    ``origin_allowed`` matches bare, lowercased hosts (it strips scheme, port and
    path off the inbound ``Origin`` before testing membership), so the value stored
    on ``Site.allowed_origins`` must be that same bare shape — see
    ``sites.service._normalize_origin_hosts``, which this feeds.
    """
    host = (url or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split(":", 1)[0]
    return host


__all__ = [
    "EMBED_MARKER",
    "WIDGET_JS_PATH",
    "build_embed_snippet",
    "concierge_snippet",
    "deployed_host",
    "inject_into_html",
    "inject_into_tree",
    "widget_js_url",
]
