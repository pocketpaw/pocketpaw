# ee/pocketpaw_ee/sites/url_crawler.py — Paw Sites same-site URL crawler (SI-5).
#
# Created 2026-07-23 (feat/sites-import-crawler, stacked on SI-4): the fetch half of
# POST /sites/import/from-url. Fills the ``crawl_site_from_url`` seam SI-4 left open.
# SECURITY-CRITICAL — this module is the SSRF surface of the import path. The
# guards themselves now live in safe_fetch.py (see the 2026-09-18 note below);
# they are listed here because this is where they were written and reviewed:
#   * Seed/hop URL validation: http(s) only, no credentials in the URL, ports limited
#     to 80/443/default, length-capped (``validate_seed_url`` — the endpoint calls it
#     too, so a hostile seed 422s before anything is minted or fetched).
#   * DNS is resolved HERE and the connection is PINNED to the validated IP (the
#     request rides ``scheme://ip/...`` with the original Host header + SNI hostname
#     for https), so a re-resolution between check and fetch (TOCTOU / DNS rebinding)
#     cannot swap in a private address. ALL resolved addresses must pass — a mixed
#     public+private answer is rejected outright.
#   * Forbidden targets (both families): loopback, RFC1918/private, link-local (incl.
#     169.254.169.254 metadata), CGNAT 100.64/10, unspecified/reserved/multicast,
#     IPv6 ULA fc00::/7, fe80::/10, and v4 addresses EMBEDDED in v6 forms
#     (IPv4-mapped, 6to4, Teredo, NAT64 64:ff9b::/96) are re-checked as v4.
#   * Redirects are followed MANUALLY (max 5) and EVERY hop re-runs the full URL +
#     DNS + IP validation; non-http(s) redirect targets are rejected.
#   * Per-fetch timeout, per-response size cap, total crawl byte budget (streamed —
#     the response is aborted the moment a cap is crossed, never buffered past it),
#     no cookies (jar cleared after every response), no auth, no env proxies
#     (trust_env=False), an honest User-Agent.
# Crawl scope: BFS from the seed, EXACT host match only. The seed MAY redirect
# off-host (apex->www is the common case) — the crawl then re-seeds to the final
# host so the whole site imports; every OTHER fetch is scope-locked, so a
# same-site page/asset cannot 30x us into fetching foreign content. Depth <= 3,
# pages <= 50, assets <= 200,
# robots.txt honored (simple RobotFileParser matching for our UA + '*'; a failed
# robots fetch degrades to a polite warning), small politeness delay between fetches.
# Harvest: pages/assets map to safe relative paths (sanitized through import_service's
# ``_safe_entry_path`` — the SAME rule zip entries pass), absolute same-origin URLs in
# HTML/CSS are rewritten root-relative, CSS url()/@import refs are chased same-origin.
# Cross-origin refs are left as-is and counted for the import report.
# Edited 2026-07-23 (SSRF review): redirect scope guard (off-host 30x rejected
# for non-seed fetches) + seed apex->www re-seed; opposite-scheme origin rewrite.
# Edited 2026-09-04 (page/dir collision): the asset loop gained the page loop's
# content-type guard, the other way round. A <link href> that answers text/html
# (rel="canonical" above all) now claims the PAGE path, so /about stops landing at
# the file "about" beside the directory "about/" the same page claims through
# <a href="/about/">. That FileMap cannot exist on a filesystem: the generator
# mkdirs over the file, EEXIST, and the whole URL import reports "failed". On one
# real site 48 of 112 harvested files collided this way. Such a page counts
# against MAX_CRAWL_PAGES, and against the asset loop's fetch ceiling (which is
# now counted in slots, not stored files, so re-routing a fetch out of
# assets_fetched cannot widen it). _LinkScan also stops queueing navigational
# <link rel> values outright (canonical, prefetch, alternate & co) — the
# content-type guard stays as the backstop for the ones it can't know about.
# Scope, SSRF, robots and byte-budget behaviour are unchanged.
# Edited 2026-09-18 (SF-7, feat/sites-single-url-fetch): the SSRF fetch machinery
# — URL-shape validation, the forbidden-IP check, the DNS resolve, the IP-pinned
# streaming GET and the manual redirect loop — MOVED VERBATIM to
# ee/pocketpaw_ee/sites/safe_fetch.py and is imported back. A coming slice has to
# fetch ONE customer-controlled URL (a domain-ownership token at
# /.well-known/paw-verify), and the rule is that this codebase has exactly one
# SSRF surface; burying it inside a BFS crawler left that caller with no honest
# way to reuse it short of a depth-1 crawl. What stayed here is the CRAWL: BFS,
# scope, robots, the byte budget, the HTML/CSS harvest. Behaviour is unchanged
# and the error classes are the same objects under their old names, so
# `except CrawlError` and every existing test keep working.

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
    assets_fetched: int = 0
    bytes_fetched: int = 0
    skipped_by_robots: int = 0
    cross_origin_refs: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "pages_fetched": self.pages_fetched,
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


def _allowed_by_robots(robots: robotparser.RobotFileParser | None, url: str) -> bool:
    if robots is None:
        return True
    try:
        return robots.can_fetch(USER_AGENT, url)
    except Exception:  # noqa: BLE001 — a pathological robots file never blocks the crawl
        return True


async def crawl_site(
    url: str,
    *,
    total_byte_cap: int,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    politeness_delay: float | None = None,
) -> CrawlResult:
    """BFS-crawl ``url``'s site (same exact host only) into a FileMap.

    Raises on the FATAL failure modes — bad/forbidden seed, unreachable seed,
    seed blocked by robots, byte budget exceeded. Per-page/per-asset problems
    degrade to report warnings. ``transport``/``resolver`` are test seams;
    ``politeness_delay`` overrides the module default (tests pass 0)."""
    # Lazy import — import_service imports this module; the safe-path rule is
    # shared with the zip path deliberately (ONE sanitizer for both imports).
    from pocketpaw_ee.sites.import_service import _safe_entry_path

    delay = POLITENESS_DELAY_SEC if politeness_delay is None else politeness_delay
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
    fetcher = SafeFetcher(total_byte_cap=total_byte_cap, transport=transport, resolver=resolver)
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

        while queue and result.stats.pages_fetched < MAX_CRAWL_PAGES:
            page_url, depth = queue.popleft()
            is_seed = page_url == seed_url
            if not _allowed_by_robots(robots, page_url):
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
                if len(seen_pages) >= MAX_CRAWL_PAGES:
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
        while asset_queue and asset_slots < MAX_CRAWL_ASSETS:
            asset_url = asset_queue.popleft()
            if not _allowed_by_robots(robots, asset_url):
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
                if result.stats.pages_fetched >= MAX_CRAWL_PAGES:
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

        if asset_queue:
            result.warnings.append(
                f"asset cap reached ({MAX_CRAWL_ASSETS}) — {len(asset_queue)} assets not fetched"
            )
        if queue or pages_truncated:
            result.warnings.append(
                f"page cap reached ({MAX_CRAWL_PAGES}) — some linked pages were not crawled"
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
