# ee/pocketpaw_ee/sites/url_crawler.py — the same-site crawler behind
# POST /sites/import/from-url. It walks a customer's live site into a FileMap
# that the shared import pipeline then treats exactly like an uploaded zip.
#
# Fetching is NOT done here. Every request goes through safe_fetch, the one SSRF
# surface in this codebase; this module supplies only the crawl policy around it.
# Do not add an httpx call to this file — extend safe_fetch instead.
#
# SCOPE IS EXACT-HOST, AND THAT IS A SECURITY BOUNDARY. A same-site page or asset
# must not be able to 30x the crawler into fetching (and then deploying) foreign
# content, so every non-seed fetch passes allowed_host. The SEED is the one
# exception: it may redirect off-host, because apex->www is the common case, and
# the crawl re-seeds `scope` to the final host so the whole site imports instead
# of one page plus a wall of warnings. Cross-origin refs are left as-is, counted.
#
# CAPS, all load-bearing against a hostile site: depth <= MAX_CRAWL_DEPTH,
# MAX_CRAWL_PAGES pages, MAX_CRAWL_ASSETS fetch SLOTS (slots, not stored files,
# so re-routing a fetch cannot widen the ceiling), MAX_TRACKED_URLS on the
# discovery sets so link soup cannot balloon memory, and a total byte budget that
# aborts the whole import when crossed.
#
# CONTENT TYPE DECIDES THE PATH, not the tag that pointed at it — both loops
# check it. A <link href> answering text/html must claim the PAGE path, or /about
# lands as a file beside the directory /about/ that the same page claims through
# <a href="/about/">, and that FileMap cannot exist on a filesystem: the
# generator mkdirs over the file and the import reports "failed". _LinkScan's
# navigational-rel list (canonical, prefetch, alternate) is a DENYLIST, so an
# unfamiliar rel is still fetched and the content-type check is the backstop.
#
# TWO CONSUMERS, ONE POLICY. The import endpoint wants a deployable site, so it
# takes the defaults (every cap above, assets included). Site-KB GROUNDING
# (sites.foreign_grounding) wants prose only, and passes ``max_pages`` +
# ``fetch_assets=False`` to say so: a stylesheet cannot be quoted to a visitor, so
# fetching one is a request against a customer's origin that can only be thrown
# away. Both knobs NARROW ONLY — ``max_pages`` is clamped to MAX_CRAWL_PAGES, so a
# caller can spend less of the budget but never more.
#
# Harvested paths run through import_service._safe_entry_path, the SAME sanitizer
# zip entries pass. Absolute same-origin URLs in HTML/CSS are rewritten
# root-relative; CSS url()/@import refs are chased same-origin. robots.txt is
# honored for our UA and '*'; a FAILED robots fetch degrades to a report warning
# rather than blocking the import.

"""Same-site crawler with SSRF-hardened fetching for Paw Sites URL imports."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.safe_fetch import (
    USER_AGENT,
    FetchBudgetExceeded,
    FetchError,
    FetchResult,
    SafeFetcher,
    validate_fetch_url,
)

logger = logging.getLogger(__name__)

# Compat aliases — the SSRF fetch machinery moved to safe_fetch (SF-7) but this
# module is its historical home, and import_service plus the crawler test suite
# import these names from here. They are the SAME objects, so `except
# CrawlError` and `isinstance` keep working across the move.
CrawlError = FetchError
CrawlBudgetExceeded = FetchBudgetExceeded
validate_seed_url = validate_fetch_url

__all__ = [
    "CrawlBudgetExceeded",
    "CrawlError",
    "CrawlResult",
    "CrawlStats",
    "FetchResult",
    "SafeFetcher",
    "crawl_site",
    "validate_seed_url",
]


MAX_CRAWL_DEPTH = 3
MAX_CRAWL_PAGES = 50
MAX_CRAWL_ASSETS = 200
# Memory floor: a hostile 10MB page can carry millions of DISTINCT refs — the
# discovery sets (assets to fetch, cross-origin URLs to count) stop growing at
# this bound so link soup can't balloon the crawler's memory.
MAX_TRACKED_URLS = 2000
# Politeness delay between consecutive fetches (tests pass 0).
POLITENESS_DELAY_SEC = 0.15


_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"@import\s+['\"]([^'\"]+)['\"]", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# HTML/CSS harvesting
# --------------------------------------------------------------------------- #


# <link rel> values that are NAVIGATIONAL, not asset refs: they address pages
# (or, for the connection hints, a bare host). Queueing them buys a fetch whose
# only possible product is a duplicate of a page the crawl already holds — 48 of
# them on one real site. A DENYLIST, deliberately: an unfamiliar rel is still
# fetched, and the asset loop's content-type guard catches it if it turns out to
# be a page, so a site can hide a page behind any rel it likes and still import.
_NON_ASSET_LINK_RELS = frozenset(
    {
        "canonical",
        "alternate",
        "prefetch",
        "prerender",
        "dns-prefetch",
        "preconnect",
        "next",
        "prev",
        "pingback",
    }
)


class _LinkScan(HTMLParser):
    """One-pass scan of a fetched page: same-site page links (<a href>) and asset
    refs (<link href> minus the navigational rels, <script src>, <img src/srcset>,
    <source src/srcset>, <video/audio src>) — classification against the seed host
    happens later."""

    def __init__(self) -> None:
        super().__init__()
        self.page_links: list[str] = []
        self.asset_refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        if tag == "a" and attr_map.get("href"):
            self.page_links.append(attr_map["href"])
        elif tag == "link" and attr_map.get("href"):
            # Navigational only when EVERY token is: rel="alternate stylesheet"
            # is a stylesheet, and a <link> with no rel at all stays an asset.
            rels = attr_map.get("rel", "").lower().split()
            if not (rels and all(r in _NON_ASSET_LINK_RELS for r in rels)):
                self.asset_refs.append(attr_map["href"])
        elif tag == "script" and attr_map.get("src"):
            self.asset_refs.append(attr_map["src"])
        elif tag in ("img", "source", "video", "audio", "embed"):
            if attr_map.get("src"):
                self.asset_refs.append(attr_map["src"])
            if attr_map.get("srcset"):
                self.asset_refs.extend(_parse_srcset(attr_map["srcset"]))


def _parse_srcset(srcset: str) -> list[str]:
    """Extract the URL of each srcset candidate ("url 2x, url2 480w" → urls)."""
    urls: list[str] = []
    for part in srcset.split(","):
        candidate = part.strip().split(" ")[0].strip()
        if candidate:
            urls.append(candidate)
    return urls


def _css_refs(css_text: str) -> list[str]:
    """url()/@import references out of a stylesheet (data: URIs skipped)."""
    refs = _CSS_URL_RE.findall(css_text) + _CSS_IMPORT_RE.findall(css_text)
    return [r for r in refs if not r.lower().startswith("data:")]


def _rewrite_same_origin(text: str, origins: list[str]) -> str:
    """Rewrite absolute same-origin refs to root-relative so the generator's
    import plan (which handles absolute→relative) works unchanged. Plain string
    rewrite — v1 accepts the (documented) risk of touching prose that contains
    the site's own URL; markup-aware rewriting is a later refinement."""
    for origin in origins:
        text = text.replace(origin + "/", "/")
        text = text.replace(origin, "/")
    return text


def _rel_path_for_page(path: str) -> str:
    """Map a page URL path to its FileMap path: "/" → index.html, a trailing
    slash or an extension-less last segment → <path>/index.html."""
    decoded = unquote(path or "/")
    rel = decoded.lstrip("/")
    if not rel:
        return "index.html"
    if rel.endswith("/"):
        return rel + "index.html"
    last = rel.rsplit("/", 1)[-1]
    if "." not in last:
        return rel + "/index.html"
    return rel


def _rel_path_for_asset(path: str) -> str:
    """Map an asset URL path to its FileMap path (no index.html defaulting)."""
    return unquote(path or "").lstrip("/")


# --------------------------------------------------------------------------- #
# The crawl
# --------------------------------------------------------------------------- #


@dataclass
class CrawlStats:
    """Counters surfaced on the import report."""

    pages_fetched: int = 0
    # Non-seed pages that were queued and did not arrive (fetch error, non-200).
    # Each one is ALSO a warning; the counter exists because a consumer has to
    # decide "is this harvest complete" without parsing warning prose.
    pages_failed: int = 0
    assets_fetched: int = 0
    bytes_fetched: int = 0
    skipped_by_robots: int = 0
    cross_origin_refs: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "assets_fetched": self.assets_fetched,
            "bytes_fetched": self.bytes_fetched,
            "skipped_by_robots": self.skipped_by_robots,
            "cross_origin_refs": self.cross_origin_refs,
        }


@dataclass
class CrawlResult:
    """Everything the import pipeline needs: the harvested FileMap + stats."""

    files: dict[str, bytes] = field(default_factory=dict)
    stats: CrawlStats = field(default_factory=CrawlStats)
    warnings: list[str] = field(default_factory=list)


def _normalize(url: str) -> str:
    """Dedupe key + fetch URL: drop fragment AND query (a static import keys
    pages/assets by path; querystring variants collapse to one — documented v1
    limitation)."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


async def _load_robots(
    fetcher: SafeFetcher, seed: Any
) -> tuple[robotparser.RobotFileParser | None, str | None]:
    """Fetch + parse robots.txt. Missing/failed → (None, warning-or-None): we
    proceed politely, noting the failure on the report when the FETCH errored."""
    robots_url = urlunparse((seed.scheme, seed.netloc, "/robots.txt", "", "", ""))
    try:
        result = await fetcher.fetch(robots_url)
    except CrawlBudgetExceeded:
        raise
    except (CrawlError, ValidationError, httpx.HTTPError):
        return None, "robots.txt could not be fetched — proceeding politely without it"
    if result.status != 200:
        return None, None  # no robots file — everything is allowed, not a warning
    parser = robotparser.RobotFileParser()
    parser.parse(result.body.decode("utf-8", errors="replace").splitlines())
    return parser, None


def _allowed_by_robots(
    robots: robotparser.RobotFileParser | None, url: str, user_agent: str
) -> bool:
    """robots for the UA WE ANNOUNCE, not a hardcoded one. A caller that fetches
    under its own identity must be checked under that identity, or the operator's
    rule for the name in our request header is silently the wrong rule."""
    if robots is None:
        return True
    try:
        return robots.can_fetch(user_agent, url)
    except Exception:  # noqa: BLE001 — a pathological robots file never blocks the crawl
        return True


async def crawl_site(
    url: str,
    *,
    total_byte_cap: int,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    politeness_delay: float | None = None,
    max_pages: int | None = None,
    fetch_assets: bool = True,
    user_agent: str = USER_AGENT,
) -> CrawlResult:
    """BFS-crawl ``url``'s site (same exact host only) into a FileMap.

    Raises on the FATAL failure modes — bad/forbidden seed, unreachable seed,
    seed blocked by robots, byte budget exceeded. Per-page/per-asset problems
    degrade to report warnings. ``transport``/``resolver`` are test seams;
    ``politeness_delay`` overrides the module default (tests pass 0).

    ``max_pages`` and ``fetch_assets`` NARROW the budget for a caller that wants
    less than a deployable copy of the site. ``max_pages`` is clamped into
    [1, MAX_CRAWL_PAGES] so it can only spend less; ``fetch_assets=False`` skips
    the asset pass entirely, which also means a page reachable ONLY through a
    <link rel> is not discovered (that page arrives via the asset loop's
    content-type guard). The grounding caller accepts that: it wants prose, and a
    page nothing links to with <a href> is not part of the site's navigation.

    ``user_agent`` is the identity announced to the customer's server AND the
    identity robots.txt is evaluated for — the two must be the same string or an
    operator's rule about the name in our header is not the rule we obeyed."""
    # Lazy import — import_service imports this module; the safe-path rule is
    # shared with the zip path deliberately (ONE sanitizer for both imports).
    from pocketpaw_ee.sites.import_service import _safe_entry_path

    delay = POLITENESS_DELAY_SEC if politeness_delay is None else politeness_delay
    # Clamped, not trusted: MAX_CRAWL_PAGES stays the ceiling no matter what a
    # caller asks for, so this knob cannot be used to widen the crawl.
    page_budget = MAX_CRAWL_PAGES if max_pages is None else max(1, min(max_pages, MAX_CRAWL_PAGES))
    seed = validate_seed_url(url)
    # Crawl scope is MUTABLE: if the seed redirects to another host (apex->www,
    # the overwhelmingly common case), we re-seed to the final host after the
    # seed fetch so the whole site imports instead of a lone page + a wall of
    # cross-origin warnings. `scope["netloc"]`/`scope["origins"]` are what
    # `_same_site` and the URL rewrite read, so updating them re-homes the crawl.
    scope: dict[str, Any] = {
        "netloc": seed.netloc.lower(),
        # Both schemes + protocol-relative, longest-first (see _origins_for).
        "origins": [
            f"https://{seed.netloc}",
            f"http://{seed.netloc}",
            f"//{seed.netloc}",
        ],
    }

    result = CrawlResult()
    fetcher = SafeFetcher(
        total_byte_cap=total_byte_cap,
        user_agent=user_agent,
        transport=transport,
        resolver=resolver,
    )
    fetched_once = False

    async def _polite_fetch(target: str, *, allowed_host: str | None = None) -> FetchResult:
        nonlocal fetched_once
        if fetched_once and delay > 0:
            await asyncio.sleep(delay)
        fetched_once = True
        return await fetcher.fetch(target, allowed_host=allowed_host)

    def _origins_for(scheme: str, netloc: str) -> list[str]:
        # Longest first so str.replace peels "scheme://host" before "//host".
        # Both schemes are listed: a page served over https routinely hard-codes
        # http://its-own-host refs, and rewriting only the matching scheme would
        # mangle the other to "http:/path".
        return [
            f"https://{netloc}",
            f"http://{netloc}",
            f"//{netloc}",
        ]

    def _reseed(final_url: str) -> None:
        """Re-home the crawl scope to the seed's post-redirect host."""
        final = urlparse(final_url)
        scope["netloc"] = final.netloc.lower()
        scope["origins"] = _origins_for(final.scheme, final.netloc)

    def _same_site(candidate: Any) -> bool:
        return candidate.scheme in ("http", "https") and candidate.netloc.lower() == scope["netloc"]

    def _claim_path(rel: str, source_url: str, *, quiet_duplicate: bool = False) -> str | None:
        """Sanitize + reserve a FileMap path; None (with a warning) when unsafe
        or already taken (first fetch wins). ``quiet_duplicate`` drops the
        already-taken warning for the one collision that is EXPECTED rather than
        lossy: the same page claimed twice, once as a page and once through a
        <link rel> that points at it."""
        try:
            safe = _safe_entry_path(rel)
        except ValidationError:
            result.warnings.append(f"skipped {source_url} — its path is not importable")
            return None
        if safe in result.files:
            if not quiet_duplicate:
                result.warnings.append(f"skipped {source_url} — path {safe!r} already imported")
            return None
        return safe

    try:
        robots, robots_warning = await _load_robots(fetcher, seed)
        if robots_warning:
            result.warnings.append(robots_warning)

        seed_url = _normalize(url)
        queue: deque[tuple[str, int]] = deque([(seed_url, 0)])
        seen_pages = {seed_url}
        asset_queue: deque[str] = deque()
        seen_assets: set[str] = set()
        cross_origin: set[str] = set()
        pages_truncated = False

        def _note_cross_origin(absolute: str) -> None:
            # Bounded (MAX_TRACKED_URLS): link soup can't balloon memory.
            if len(cross_origin) < MAX_TRACKED_URLS:
                cross_origin.add(_normalize(absolute))

        def _note_asset(normalized: str) -> None:
            if normalized not in seen_assets and len(seen_assets) < MAX_TRACKED_URLS:
                seen_assets.add(normalized)
                asset_queue.append(normalized)

        while queue and result.stats.pages_fetched < page_budget:
            page_url, depth = queue.popleft()
            is_seed = page_url == seed_url
            if not _allowed_by_robots(robots, page_url, user_agent):
                result.stats.skipped_by_robots += 1
                if is_seed:
                    raise CrawlError(
                        "the seed page is disallowed by the site's robots.txt",
                        code="sites.import_crawl_blocked_by_robots",
                    )
                continue
            try:
                # The seed may redirect off-host (apex->www): let it, then
                # re-seed. Every OTHER fetch is scope-locked so a same-site
                # resource can't redirect us into importing foreign content.
                fetched = await _polite_fetch(
                    page_url, allowed_host=None if is_seed else scope["netloc"]
                )
            except CrawlBudgetExceeded:
                raise
            except (CrawlError, ValidationError, httpx.HTTPError) as exc:
                if is_seed:
                    raise CrawlError(
                        "the seed URL could not be fetched",
                        code="sites.import_crawl_seed_unreachable",
                    ) from exc
                result.stats.pages_failed += 1
                result.warnings.append(f"skipped {page_url} — fetch failed")
                continue
            if is_seed and urlparse(fetched.url).netloc.lower() != scope["netloc"]:
                _reseed(fetched.url)
            if fetched.status != 200:
                if is_seed:
                    raise CrawlError(
                        f"the seed URL answered HTTP {fetched.status}",
                        code="sites.import_crawl_seed_unreachable",
                    )
                result.stats.pages_failed += 1
                result.warnings.append(f"skipped {page_url} — HTTP {fetched.status}")
                continue

            parsed_final = urlparse(fetched.url)
            if fetched.content_type and fetched.content_type != "text/html":
                # Content-type wins over the link's shape: a "page" link serving
                # a non-HTML body imports as an asset.
                if is_seed:
                    raise CrawlError(
                        "the seed URL did not return an HTML page",
                        code="sites.import_crawl_seed_not_html",
                    )
                rel = _claim_path(_rel_path_for_asset(parsed_final.path), page_url)
                if rel:
                    result.files[rel] = fetched.body
                    result.stats.assets_fetched += 1
                continue

            html_text = fetched.body.decode("utf-8", errors="replace")
            scan = _LinkScan()
            try:
                scan.feed(html_text)
            except Exception:  # noqa: BLE001 — a malformed page still imports, unscanned
                result.warnings.append(f"{page_url} did not parse cleanly — links not followed")
            result.stats.pages_fetched += 1

            for href in scan.page_links:
                absolute = urljoin(fetched.url, href)
                candidate = urlparse(absolute)
                if candidate.scheme not in ("http", "https"):
                    continue  # mailto:, javascript:, tel: …
                if not _same_site(candidate):
                    _note_cross_origin(absolute)
                    continue
                normalized = _normalize(absolute)
                if normalized in seen_pages or depth >= MAX_CRAWL_DEPTH:
                    continue
                if len(seen_pages) >= page_budget:
                    pages_truncated = True
                    continue
                seen_pages.add(normalized)
                queue.append((normalized, depth + 1))

            for ref in scan.asset_refs:
                absolute = urljoin(fetched.url, ref)
                candidate = urlparse(absolute)
                if candidate.scheme not in ("http", "https"):
                    continue
                if not _same_site(candidate):
                    _note_cross_origin(absolute)
                    continue
                _note_asset(_normalize(absolute))

            rel = _claim_path(_rel_path_for_page(parsed_final.path), page_url)
            if rel:
                result.files[rel] = _rewrite_same_origin(html_text, scope["origins"]).encode(
                    "utf-8"
                )

        # ------------------------------------------------------------------- #
        # Assets (CSS refs chase same-origin, so the queue can grow here).
        # ------------------------------------------------------------------- #
        # MAX_CRAWL_ASSETS bounds FETCHES, not files: an entry that answers
        # text/html is stored as a page below, and it burns a slot exactly as it
        # did when it was (mis)stored as an asset. Gating on assets_fetched alone
        # would let this loop drain the whole 2000-entry queue on a link-heavy
        # site — several hundred page fetches where 200 was the ceiling.
        asset_slots = 0
        while fetch_assets and asset_queue and asset_slots < MAX_CRAWL_ASSETS:
            asset_url = asset_queue.popleft()
            if not _allowed_by_robots(robots, asset_url, user_agent):
                result.stats.skipped_by_robots += 1
                continue
            try:
                fetched = await _polite_fetch(asset_url, allowed_host=scope["netloc"])
            except CrawlBudgetExceeded:
                raise
            except (CrawlError, ValidationError, httpx.HTTPError):
                result.warnings.append(f"skipped asset {asset_url} — fetch failed")
                continue
            if fetched.status != 200:
                result.warnings.append(f"skipped asset {asset_url} — HTTP {fetched.status}")
                continue
            parsed_final = urlparse(fetched.url)
            if fetched.content_type == "text/html":
                # The page loop's content-type guard, mirrored: there a "page"
                # serving a non-HTML body imports as an asset; here an "asset"
                # serving HTML imports as a page. _NON_ASSET_LINK_RELS drops the
                # rels we KNOW are navigational, but a site can hang a page off
                # any other rel, so this is the guard that actually holds. Claimed
                # by the asset rule, /about landed at the file "about" while the
                # same page reached through <a href="/about/"> landed at
                # "about/index.html"; a FileMap holding a file AND a directory of
                # one name is unrepresentable, so the generator mkdirs over the
                # file, dies EEXIST, and the whole import reports "failed". Both
                # spellings now claim ONE page path and first-fetch-wins dedupes
                # them. The body gets the page loop's exact treatment (same-origin
                # rewrite, utf-8 text) — raw bytes would leave this one page
                # pointing back at the site we imported FROM. Links are NOT
                # re-scanned: the page loop has ended, so nothing found here could
                # be fetched. Storing counts against MAX_CRAWL_PAGES, so a site
                # cannot smuggle extra pages past the page cap through <link>.
                asset_slots += 1
                rel = _claim_path(
                    _rel_path_for_page(parsed_final.path), asset_url, quiet_duplicate=True
                )
                if rel is None:
                    continue
                if result.stats.pages_fetched >= page_budget:
                    pages_truncated = True
                    continue
                html_text = fetched.body.decode("utf-8", errors="replace")
                rewritten = _rewrite_same_origin(html_text, scope["origins"])
                result.files[rel] = rewritten.encode("utf-8")
                result.stats.pages_fetched += 1
                continue
            rel = _claim_path(_rel_path_for_asset(parsed_final.path), asset_url)
            if rel is None:
                continue
            is_css = fetched.content_type == "text/css" or (
                not fetched.content_type and rel.endswith(".css")
            )
            if is_css:
                css_text = fetched.body.decode("utf-8", errors="replace")
                for ref in _css_refs(css_text):
                    absolute = urljoin(fetched.url, ref)
                    candidate = urlparse(absolute)
                    if candidate.scheme not in ("http", "https"):
                        continue
                    if not _same_site(candidate):
                        _note_cross_origin(absolute)
                        continue
                    _note_asset(_normalize(absolute))
                result.files[rel] = _rewrite_same_origin(css_text, scope["origins"]).encode("utf-8")
            else:
                result.files[rel] = fetched.body
            result.stats.assets_fetched += 1
            asset_slots += 1

        if asset_queue and not fetch_assets:
            result.warnings.append(
                f"{len(asset_queue)} asset(s) not fetched — this crawl harvests pages only"
            )
        elif asset_queue:
            result.warnings.append(
                f"asset cap reached ({MAX_CRAWL_ASSETS}) — {len(asset_queue)} assets not fetched"
            )
        if queue or pages_truncated:
            result.warnings.append(
                f"page cap reached ({page_budget}) — some linked pages were not crawled"
            )
        result.stats.cross_origin_refs = len(cross_origin)
        result.stats.bytes_fetched = fetcher.bytes_fetched
        if cross_origin:
            result.warnings.append(
                f"{len(cross_origin)} cross-origin reference(s) left as-is — external "
                "hosts are never crawled"
            )
        # v1 scope note surfaced honestly: exact-host matching only.
        result.warnings.append(
            "same-site matching is exact-host in v1 — www. and apex variants of the "
            "seed host are treated as external"
        )
        return result
    finally:
        await fetcher.aclose()
