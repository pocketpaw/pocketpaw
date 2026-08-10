# ee/pocketpaw_ee/sites/draft_markup.py — ONE self-contained HTML document for a
# site pocket's CURRENT DRAFT content.
#
# Created 2026-08-07 (SC-2 — drafts get art too). SC-1 photographs a site by
# pointing Cloudflare Browser Rendering at its live URL. A DRAFT has no live URL —
# that is what makes it a draft — and the builder's own preview address is a
# 127.0.0.1 bound inside the API process, which Cloudflare's browser cannot reach.
# So a draft is captured from its MARKUP instead: the screenshot endpoint takes an
# ``html`` body as an alternative to ``url``, and this module produces that html.
#
# THE ONE HARD CONSTRAINT ON THE OUTPUT: Browser Rendering renders an ``html`` body
# at ``about:blank``. There is no origin, so NO relative reference resolves — not a
# stylesheet, not an image, not a font. A built page handed over verbatim renders as
# unstyled text. Everything local therefore has to be folded INTO the document
# before it leaves here (``inline_document``), and anything local that cannot be
# folded in is dropped rather than left to 404. Absolute http(s) references are left
# exactly as they are: Cloudflare's browser is on the internet and can fetch them.
#
# WHERE THE MARKUP COMES FROM — a deliberate cost ladder, cheapest rung first,
# because this runs for a picture on a gallery card and must never be worth its
# cost:
#   1. An EXISTING build on disk (``build_home()/<pocket>/<static_output_rel>``).
#      A pocket that has ever been previewed, armed or published already has its
#      built output sitting there (PERF-3 keeps the per-pocket build dir), so this
#      rung costs a few file reads.
#   2. NO BUILD NEEDED (the ``html`` engine — a zip/from-url import). ``engines``
#      says an html site runs no Node build and its served artifact is byte-identical
#      to the authored source, so the pocket's own ``source`` map IS the static tree.
#      Zero subprocesses.
#   3. A REAL BUILD (``ripple`` / ``svelte``, never built before). ``bun install`` +
#      a Vite/SvelteKit build. This rung is gated by
#      ``PAW_SITES_DRAFT_CAPTURE_BUILD`` and is OFF by default — see
#      ``build_allowed`` for the measurement that decided that.
#
# MEASURED on this workspace (2026-08-07, a real paw-sites-gen + bun toolchain, a
# small svelte marketing page, other work running alongside):
#   rung 1  read an existing build and inline it   0.001s
#   rung 2  html source map straight to a document 0.002s
#   rung 3  svelte build, cold (no node_modules)  15.9s
#   rung 3  svelte build, warm (install cached)    6.8s
# Rungs 1 and 2 are free at any scale. Rung 3 is not, which is the whole reason the
# ladder exists.
#
# Nothing here may raise into a caller: ``sites.screenshot`` wraps it, and that
# wrapper is itself wrapped at the call site. A draft that cannot be rendered
# returns "" and the gallery card shows its themed placeholder, which is the whole
# reason the placeholder exists.

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pocketpaw_ee.sites.engines import (
    is_source_engine,
    needs_node_build,
    resolve_static_output_rel,
)

logger = logging.getLogger(__name__)

# Total RAW bytes we are willing to fold into one document as ``data:`` URIs.
# Base64 inflates by ~4/3, so this is roughly a 4MB document ceiling — comfortably
# inside a Browser Rendering request body, and far more than a landing page's CSS +
# hero imagery actually needs. Stylesheets are inlined BEFORE images so the budget,
# when it runs out, runs out on decoration rather than on layout.
_MAX_INLINE_BYTES = 3 * 1024 * 1024

# The document itself is capped too: a page whose markup alone is bigger than this
# is not a marketing page, and posting it would trade a card thumbnail for a
# multi-megabyte upload. Over the cap we send nothing and the card takes the
# placeholder.
_MAX_HTML_BYTES = 6 * 1024 * 1024

# Extension → mime for the ``data:`` URIs. An extension that is not on this list is
# NOT inlined: guessing a mime for an unknown asset is how you end up rendering a
# broken image, which is the one outcome this slice promises never to produce.
_MIME_BY_EXT: dict[str, str] = {
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}

# References that are already resolvable without an origin (or are not fetches at
# all). Everything else is treated as local and must be inlined or dropped.
_NON_LOCAL_PREFIXES = ("http://", "https://", "//", "data:", "blob:", "mailto:", "tel:", "#")


def build_allowed() -> bool:
    """May a draft capture run a full Node build to get markup (ladder rung 3)?

    Default NO, and that default is a measurement rather than a preference. A
    never-built svelte pocket measured 15.9s cold and 6.8s warm here. Three reasons
    that is the wrong thing to spend on a card thumbnail by default:

    * ``GeneratorClient`` serializes builds of the SAME pocket behind a per-pocket
      lock, so a draft-capture build in flight is a build the user's next preview or
      publish QUEUES BEHIND. A picture must not make the primary action slower.
    * it fires at site-CREATE time, which is precisely when the user is still
      chatting with the agent that made the site — the worst moment to take CPU.
    * a created site is often published minutes later, and the live capture then
      re-shoots it, so the render is paid for twice.

    A never-built ripple/svelte draft therefore shows the card's themed placeholder,
    and picks up real art the moment anything else builds the pocket — a preview
    schedules a capture of its own, and by then rung 1 costs a millisecond.

    Set ``PAW_SITES_DRAFT_CAPTURE_BUILD=1`` on a deployment that would rather spend
    the build. The html engine never consults this — it needs no build at all, which
    is why an imported draft gets a real picture under the default.
    """
    return (os.environ.get("PAW_SITES_DRAFT_CAPTURE_BUILD", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_local_ref(ref: str) -> bool:
    """True when a reference needs an origin to resolve — i.e. when it would simply
    fail against ``about:blank`` and therefore has to be inlined or dropped."""
    value = ref.strip()
    if not value:
        return False
    return not value.lower().startswith(_NON_LOCAL_PREFIXES)


def _norm_rel(ref: str, *, base: str = "") -> str:
    """Normalize one local reference to a path relative to the static ROOT.

    Strips the query/fragment, resolves ``./`` and ``../`` against ``base`` (the
    directory of the file the reference was written in — a ``url()`` inside
    ``css/app.css`` is relative to ``css/``, not to the root), and treats a leading
    ``/`` as root-relative, which is how a SvelteKit build writes its ``/_app/…``
    hrefs. Returns "" for anything that climbs out of the root.
    """
    value = ref.strip().split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    if not value:
        return ""
    if value.startswith("/"):
        parts = value.lstrip("/").split("/")
    else:
        parts = [p for p in base.split("/") if p] + value.split("/")
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                return ""  # climbs above the static root — refuse it
            out.pop()
            continue
        out.append(part)
    return "/".join(out)


def _data_uri(rel: str, data: bytes) -> str | None:
    """``data:`` URI for an asset, or None when we do not know its mime."""
    mime = _MIME_BY_EXT.get(Path(rel).suffix.lower())
    if not mime:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class _Budget:
    """The shared inlining allowance. One object so stylesheets, the ``url()``
    references inside them, and images all draw on the SAME cap — otherwise a page
    with fifty images could each pass an individual size check and still assemble a
    document nothing will accept."""

    def __init__(self, limit: int = _MAX_INLINE_BYTES) -> None:
        self.left = limit

    def take(self, size: int) -> bool:
        if size > self.left:
            return False
        self.left -= size
        return True


def _make_disk_reader(root: Path) -> Callable[[str], bytes | None]:
    """Read assets out of a built static tree, contained to that tree.

    The hrefs come from our own generator, but a source-engine site's markup is
    author-controlled, so a ``../`` that survived ``_norm_rel`` still gets refused
    here against the resolved root (symlinks included)."""
    resolved = root.resolve()

    def read(rel: str) -> bytes | None:
        if not rel:
            return None
        try:
            path = (resolved / rel).resolve()
            path.relative_to(resolved)
            if not path.is_file():
                return None
            return path.read_bytes()
        except (OSError, ValueError):
            return None

    return read


def _make_source_reader(
    source: dict[str, Any], disk: Callable[[str], bytes | None] | None = None
) -> Callable[[str], bytes | None]:
    """Read assets out of an html pocket's ``{path: contents}`` source map, falling
    back to ``disk`` for anything the map cannot hold.

    A Pocket stores TEXT only — an imported zip's binary files ride a separate
    ``assets`` sideband straight to the generator and are never persisted on the
    pocket — so images and fonts are exactly the things the source map is missing.
    When a previous publish left a built tree on disk, that tree has them.
    """
    by_path = {_norm_rel(k): v for k, v in source.items() if isinstance(v, str)}

    def read(rel: str) -> bytes | None:
        text = by_path.get(rel)
        if isinstance(text, str):
            return text.encode("utf-8")
        return disk(rel) if disk is not None else None

    return read


_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*[\"'][^\"']+[\"'][^>]*>\s*</script\s*>|"
    r"<script\b[^>]*\bsrc\s*=\s*[\"'][^\"']+[\"'][^>]*/?>",
    re.IGNORECASE,
)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SRC_RE = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SRCSET_RE = re.compile(r"""\ssrcset\s*=\s*["'][^"']*["']""", re.IGNORECASE)
_STYLESHEET_REL_RE = re.compile(r"""rel\s*=\s*["']?stylesheet""", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"""url\(\s*(["']?)([^"')]+)\1\s*\)""", re.IGNORECASE)
_STYLE_BLOCK_RE = re.compile(r"<style([^>]*)>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)


def _inline_css_urls(
    css: str, *, base: str, read: Callable[[str], bytes | None], budget: _Budget
) -> str:
    """Fold a stylesheet's own local ``url(...)`` references in as data URIs.

    This is where a hero background and the site's webfonts live, so a screenshot
    that skips it looks like a different site. A reference we cannot resolve, cannot
    type, or cannot afford is left alone: it will simply not load, which is a missing
    background rather than a broken document.
    """

    def repl(m: re.Match[str]) -> str:
        ref = m.group(2)
        if not _is_local_ref(ref):
            return m.group(0)
        rel = _norm_rel(ref, base=base)
        data = read(rel) if rel else None
        if not data or not budget.take(len(data)):
            return m.group(0)
        uri = _data_uri(rel, data)
        return f"url({uri})" if uri else m.group(0)

    return _CSS_URL_RE.sub(repl, css)


def inline_document(
    html: str, read: Callable[[str], bytes | None], *, budget: _Budget | None = None
) -> str:
    """Fold every local reference into ``html`` so it renders with no origin.

    * ``<link rel=stylesheet>`` → an inline ``<style>`` (with its own ``url()``
      references folded in). A local stylesheet we cannot read is DROPPED.
    * every other local ``<link>`` (icons, preloads, modulepreloads) → dropped; none
      of them can resolve and none of them change the picture.
    * ``<script src=…>`` → dropped whatever the origin. The capture is of the page's
      RESTING state, the state a prerendered site is designed to look finished in;
      client bundles cannot resolve their relative imports here anyway, and a remote
      script is a tracker we have no business executing to take a thumbnail.
    * an ALREADY-inline ``<style>`` block → kept, but its own ``url()`` references
      folded in. SvelteKit inlines critical CSS, which is exactly the CSS the hero
      needs, so leaving its background and font references dangling would undo the
      point of inlining the rest.
    * ``<img src=local>`` → a data URI, with any ``srcset`` on that tag removed so
      the browser cannot prefer a candidate that will never load.

    Order matters: stylesheets first, images second, one shared budget — so a page
    with more assets than the budget loses pictures, not layout.
    """
    budget = budget or _Budget()

    def link_repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        href_m = _HREF_RE.search(tag)
        if not href_m or not _is_local_ref(href_m.group(1)):
            return tag
        if not _STYLESHEET_REL_RE.search(tag):
            return ""  # a local icon/preload/modulepreload — unresolvable, so gone
        rel = _norm_rel(href_m.group(1))
        data = read(rel) if rel else None
        if not data or not budget.take(len(data)):
            return ""
        css = _inline_css_urls(
            data.decode("utf-8", "replace"),
            # A ``url()`` inside the stylesheet is relative to the STYLESHEET, not to
            # the document — ``_app/…/app.css`` referencing ``./fonts/x.woff2`` means
            # ``_app/…/fonts/x.woff2``.
            base=rel.rsplit("/", 1)[0] if "/" in rel else "",
            read=read,
            budget=budget,
        )
        return f"<style>{css}</style>"

    def img_repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        src_m = _SRC_RE.search(tag)
        if not src_m or not _is_local_ref(src_m.group(1)):
            return tag
        rel = _norm_rel(src_m.group(1))
        data = read(rel) if rel else None
        if not data or not budget.take(len(data)):
            return tag
        uri = _data_uri(rel, data)
        if not uri:
            return tag
        return _SRCSET_RE.sub("", tag.replace(src_m.group(0), f'src="{uri}"'))

    def style_repl(m: re.Match[str]) -> str:
        # An ALREADY-inline block, so its url()s are relative to the DOCUMENT (base
        # ""). Run before the link pass so it only ever sees the page's own blocks,
        # never the ones link_repl is about to create (whose refs are already data:).
        css = _inline_css_urls(m.group(2), base="", read=read, budget=budget)
        return f"<style{m.group(1)}>{css}</style>"

    out = _STYLE_BLOCK_RE.sub(style_repl, html)
    out = _LINK_RE.sub(link_repl, out)
    out = _SCRIPT_SRC_RE.sub("", out)
    return _IMG_RE.sub(img_repl, out)


def _built_root(pocket_id: str, engine: str) -> Path:
    """Where a pocket's servable files land on disk: PERF-3's persistent per-pocket
    build dir (``build_home()/<pocket_id>/``) plus the static-output subdir
    (``.svelte-kit/cloudflare`` for ripple and dynamic svelte, ``build`` for a STATIC
    svelte site, ``.`` for html).

    SL-1 — resolved against the build dir rather than derived from the engine name.
    This is the call site that most needs the probe: it reconstructs a path from a
    pocket id with no generate in scope, so there is no generator result to read a
    reported ``staticDir`` off. The persistent per-pocket build dir IS the project
    dir, which is exactly what the resolver needs."""
    from pocketpaw_ee.sites.generator_client import build_home

    project_dir = build_home() / pocket_id
    return project_dir / resolve_static_output_rel(project_dir, engine)


def _built_static_dir(pocket_id: str, engine: str) -> Path | None:
    """The already-built static output for a pocket, or None (ladder rung 1).

    ``build_home()/<pocket_id>/`` is PERF-3's persistent per-pocket working dir, and
    ``static_output_rel`` says where inside it the servable files land per engine.
    An ``index.html`` there means some earlier preview / arm / publish already paid
    for this build and the capture can just read it.
    """
    try:
        static_dir = _built_root(pocket_id, engine)
        return static_dir if (static_dir / "index.html").is_file() else None
    except (OSError, KeyError):
        return None


async def _build_static_dir(
    *, site: Any, pocket: dict[str, Any], engine: str, generator: Any | None
) -> Path | None:
    """Build the pocket's markup from scratch (ladder rung 3) and return its static
    output dir. Returns None when the build rung is disabled.

    ``smoke=False`` — a draft is not being served to anyone, so the workerd SSR
    fail-gate has nothing to protect here; ``static_build=True`` because the static
    output IS what we came for. ``pocket_id`` is passed so the build lands in the
    persistent per-pocket dir, which means this expensive rung pays for itself: the
    NEXT capture (and the next preview) finds rung 1.
    """
    if not build_allowed():
        logger.debug(
            "sites.draft_markup: pocket %s needs a node build for its draft capture "
            "and PAW_SITES_DRAFT_CAPTURE_BUILD is off — card keeps its placeholder",
            pocket.get("id") or getattr(site, "pocket_id", "?"),
        )
        return None

    from pocketpaw_ee.sites.generator_client import GeneratorClient
    from pocketpaw_ee.sites.service import _capture_base

    ripple_spec = pocket.get("rippleSpec") or {}
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}
    build = await (generator or GeneratorClient()).build(
        # The two tracks are mutually exclusive (design spec §4.2): a svelte site
        # carries ``source`` and no rippleSpec, a ripple site the reverse.
        ripple_spec={} if is_source_engine(engine) else ripple_spec,
        theme=theme,
        site_id=str(getattr(site, "id", "draft")),
        title=(pocket.get("name") or getattr(site, "name", "") or "Untitled site"),
        # The capture config makes no difference to a screenshot, but this build
        # lands in the SHARED per-pocket dir, so it is built with the same capture
        # base and key a publish would use rather than leaving a differently-wired
        # tree on disk for something else to find.
        capture_api_base=_capture_base(),
        capture_signed_key=getattr(site, "signed_key", "") or "",
        engine=engine,
        source=pocket.get("source") or None,
        pocket_id=getattr(site, "pocket_id", None),
        smoke=False,
    )
    static_dir = Path(
        build.project_dir, resolve_static_output_rel(build.project_dir, engine)
    )
    return static_dir if (static_dir / "index.html").is_file() else None


async def build_draft_markup(
    site: Any, *, generator: Any | None = None, pocket: dict[str, Any] | None = None
) -> str:
    """One self-contained HTML document for this site's current draft content, or ""
    when there is nothing renderable (no pocket, no markup, no affordable way to get
    markup, or a document over the size cap).

    Raises on a genuine failure — a missing pocket, a build that blows up. The caller
    on the capture path is ``screenshot.safe_take_draft_screenshot``, which is the
    form that cannot raise.
    """
    pocket_id = getattr(site, "pocket_id", "") or ""
    if not pocket_id:
        return ""

    if pocket is None:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket = await pockets_service.get(pocket_id, getattr(site, "owner", ""))
    engine = (pocket.get("engine") or "ripple").strip()

    # Rung 1 — somebody already paid for this build.
    static_dir = _built_static_dir(pocket_id, engine)
    read: Callable[[str], bytes | None]
    if static_dir is not None:
        index_html = (static_dir / "index.html").read_text(encoding="utf-8", errors="replace")
        read = _make_disk_reader(static_dir)
    elif not needs_node_build(engine):
        # Rung 2 — an html site's served artifact IS its authored source (HE-1), so
        # the pocket's own source map is the static tree. No subprocess at all.
        source = pocket.get("source")
        if not isinstance(source, dict):
            return ""
        index = source.get("index.html") or source.get("/index.html") or source.get("./index.html")
        if not isinstance(index, str) or not index.strip():
            return ""
        index_html = index
        # A Pocket stores TEXT, so an imported zip's BINARY assets are not in the
        # source map — they only ever reached the generator on a separate sideband.
        # A previous publish's build dir is where they survive, so it backs the map.
        read = _make_source_reader(source, _make_disk_reader(_built_root(pocket_id, engine)))
    else:
        # Rung 3 — a real build, if this deployment is willing to spend one.
        built = await _build_static_dir(
            site=site, pocket=pocket, engine=engine, generator=generator
        )
        if built is None:
            return ""
        index_html = (built / "index.html").read_text(encoding="utf-8", errors="replace")
        read = _make_disk_reader(built)

    document = inline_document(index_html, read)
    if len(document.encode("utf-8", "replace")) > _MAX_HTML_BYTES:
        logger.info(
            "sites.draft_markup: pocket %s assembled a document over the %d-byte cap "
            "— skipping the capture",
            pocket_id,
            _MAX_HTML_BYTES,
        )
        return ""
    return document


__all__ = ["build_allowed", "build_draft_markup", "inline_document"]
