# ee/pocketpaw_ee/sites/kb_ingest.py — put a site's own content into the pocket KB
# its concierge reads from.
#
# Created 2026-07-26. A dedicated concierge agent used to start KNOWLEDGE-EMPTY: it
# was provisioned with a soul and a persona and a KB scope, and nothing was ever put
# in that scope. Demos only looked grounded because the widget spec happened to
# carry a catalog. Ask a concierge "what are your opening hours" and the answer was
# an honest "I don't know" about a business whose own homepage says 8am.
#
# The concierge's grounding scopes are fixed by
# ``agent_service._kb_scopes_for_context``: a CONCIERGE run reads
# ``pocket:<pocket_id>`` and ``agent:<its own agent id>``, and nothing else (no
# workspace:, no user: — a public caller must never read the whole tenant's KB).
# The second scope is the owner's own attachments on the site's agent; this module
# owns the FIRST one, so "the concierge knows the business without anyone uploading
# anything" reduces to one job: get the site's pages into ``pocket:<pocket_id>``.
# That is all this module does, and it is why a re-publish can never clobber what an
# owner attached to the agent — the two scopes have separate writers.
#
# SOURCE OF TRUTH — the POCKET, not the built artifact and not the live URL:
#   * the pocket is durable, so this works at publish time, at agent-provision time
#     for a site published months ago, and on an owner's manual re-sync — a build
#     directory only exists during a publish;
#   * it needs no network, so there is no SSRF surface. Fetching a site's own
#     ``url`` would mean fetching a customer-controlled hostname server-side, which
#     is exactly the class of request ``url_crawler`` had to be hardened against;
#   * it is what a re-publish would deploy, so the KB never describes a page the
#     site no longer serves.
#
# LANES. Three engines, three shapes, one extractor each (see ``extract_site_documents``):
#   * html   — the ``source`` map is the site's real HTML (this is also what the
#              URL-import crawler writes, so an agency's imported client site is
#              covered by this path);
#   * svelte — the ``source`` map is hand-written components; script/style blocks and
#              template expressions are dropped, the prose survives;
#   * ripple — there is no HTML yet, the copy lives in the rippleSpec, so the spec is
#              walked for its text-bearing values.
#
# NOT COVERED (deliberate, named so it is not mistaken for done): a site minted by
# ``mint_foreign_site`` — a concierge embedded on a site we do not host — has no
# content in its pocket, so it syncs zero documents. Grounding those means crawling
# the customer's own origin, which is the ``url_crawler`` SSRF-hardened path, and is
# its own slice. ``sync_site_knowledge`` reports it as skipped rather than pretending.
#
# IDEMPOTENCE. kb-go derives an article id from ``--source`` and re-ingesting the
# same source bumps that article's version instead of duplicating it, so the page
# sources here are deterministic (``site-<path-slug>``). The Site then remembers the
# ids it produced, and a later sync deletes the ones that stopped being produced —
# that is how a deleted or renamed page leaves the KB. The pocket scope is SHARED
# with owner-uploaded files, so this NEVER clears the scope; it only removes ids it
# is certain it wrote.

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

logger = logging.getLogger(__name__)

# Max pages ingested in one sync. A large site is truncated rather than allowed to
# turn a publish into an unbounded fan-out of LLM compilations.
_MAX_DOCUMENTS = 60

# Max characters kept per page. Long enough for a real content page; past this the
# tail is dropped rather than stored.
_MAX_DOCUMENT_CHARS = 40_000

# Pages shorter than this carry no answerable content (an empty layout, a redirect
# stub) and are skipped so they don't dilute BM25 scoring.
_MIN_DOCUMENT_CHARS = 40

# Tags whose CONTENT is never page copy.
_NON_CONTENT_TAGS = {"script", "style", "noscript", "template", "svg", "head"}

# rippleSpec keys that carry structure rather than copy. Everything else is treated
# as potential copy: over-collecting costs a few noise tokens in a BM25 index,
# under-collecting loses the price or the product name, which is the whole point.
_SPEC_STRUCTURAL_KEYS = {
    "align",
    "as",
    "bg",
    "class",
    "className",
    "color",
    "component",
    "direction",
    "engine",
    "font",
    "gap",
    "height",
    "href",
    "icon",
    "id",
    "justify",
    "key",
    "kind",
    "layout",
    "pattern",
    "pocket_id",
    "position",
    "rel",
    "size",
    "src",
    "style",
    "target",
    "theme",
    "type",
    "url",
    "variant",
    "widget_id",
    "width",
}

# Numeric keys worth rendering into the text — a bare number is noise, but a price
# is the single most asked-about fact on a commerce page.
_SPEC_NUMERIC_KEYS = {"amount", "cost", "price", "price_cents", "qty", "quantity"}

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_URL_RE = re.compile(r"^(?:https?:)?//|^(?:data|mailto|tel|blob):", re.I)
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")
_SVELTE_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_SVELTE_EXPR_RE = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True)
class SiteDocument:
    """One ingestable unit of a site: a page, or the whole spec for a ripple site."""

    path: str
    source: str
    text: str


@dataclass
class SiteKnowledgeReport:
    """What one sync did. Returned to the owner-facing endpoint and logged."""

    ingested: int = 0
    removed: int = 0
    skipped: int = 0
    article_ids: list[str] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #


class _TextHarvester(HTMLParser):
    """Collect visible text from HTML, dropping non-content tags.

    Block-level tags emit a newline so headings and list items don't run into the
    following sentence — BM25 does not care, but a human reading the article (and an
    agent quoting it back to a visitor) does. ``convert_charrefs`` is on, so
    ``&amp;`` arrives already decoded.
    """

    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "footer", "h1",
        "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
        "p", "section", "table", "td", "th", "tr", "ul",
    }  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppress_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        # The document title is the page's own summary of itself ("Brew & Co |
        # Opening Hours") and is worth more per token than most of the body, so it
        # is read even though it lives inside the otherwise-suppressed <head>.
        if tag == "title":
            self._in_title = True
            return
        if tag in _NON_CONTENT_TAGS:
            self._suppress_depth += 1
            return
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")
        # An image's alt text is real content on a visual page.
        if tag == "img":
            for name, value in attrs or []:
                if name == "alt" and value:
                    self._chunks.append(f"{value}\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            self._chunks.append("\n")
            return
        if tag in _NON_CONTENT_TAGS:
            self._suppress_depth = max(0, self._suppress_depth - 1)
            return
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title or self._suppress_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return _tidy("".join(self._chunks))


def _tidy(raw: str) -> str:
    """Collapse runs of spaces and blank lines, strip per-line padding."""
    collapsed = _WS_RE.sub(" ", raw)
    lines = [line.strip() for line in collapsed.split("\n")]
    return _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """Visible text of an HTML document. Malformed markup degrades to whatever the
    parser managed rather than raising — this runs over customer-authored and
    crawler-harvested pages, so it must never be the thing that fails a publish."""
    harvester = _TextHarvester()
    try:
        harvester.feed(html)
        harvester.close()
    except Exception:  # noqa: BLE001 — partial text beats no text
        logger.debug("sites.kb: HTML parse ended early; keeping partial text", exc_info=True)
    return harvester.text()


def svelte_to_text(source: str) -> str:
    """Prose from a Svelte component: script and style blocks removed first (their
    contents are code, not copy), then template expressions like ``{item.name}``,
    then the remaining markup is read as HTML."""
    without_blocks = _SVELTE_BLOCK_RE.sub(" ", source)
    without_exprs = _SVELTE_EXPR_RE.sub(" ", without_blocks)
    return html_to_text(without_exprs)


def _is_copy(value: str) -> bool:
    """True when a spec string looks like something a visitor would read."""
    text = value.strip()
    if not text or len(text) > _MAX_DOCUMENT_CHARS:
        return False
    if _HEX_COLOR_RE.match(text) or _URL_RE.match(text):
        return False
    # Anything with no letter at all is an id, a measurement, or a colour token.
    return bool(_HAS_LETTER_RE.search(text))


def spec_to_text(spec: Any) -> str:
    """Walk a rippleSpec and return its copy, in document order.

    A ripple site has no HTML until it is built, so the spec IS the page. Keys that
    carry structure are skipped (see ``_SPEC_STRUCTURAL_KEYS``); price-ish numbers
    are rendered with their key so "1200" reaches the index as "price 1200" and can
    actually be retrieved.
    """
    out: list[str] = []
    seen: set[int] = set()

    def walk(node: Any, key: str | None) -> None:
        # Specs are trees, but a caller could hand us a self-referencing dict; the
        # id guard makes that terminate instead of recursing forever.
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                return
            seen.add(id(node))
        if isinstance(node, dict):
            for child_key, child in node.items():
                if child_key in _SPEC_STRUCTURAL_KEYS:
                    continue
                walk(child, child_key)
        elif isinstance(node, list):
            for child in node:
                walk(child, key)
        elif isinstance(node, str):
            if _is_copy(node):
                out.append(node.strip())
        elif isinstance(node, bool):
            return  # a flag is never copy (and bool is an int, so check it first)
        elif isinstance(node, (int, float)) and key in _SPEC_NUMERIC_KEYS:
            out.append(f"{key} {node}")

    walk(spec, None)
    return _tidy("\n".join(out))


# --------------------------------------------------------------------------- #
# Document assembly
# --------------------------------------------------------------------------- #


def _path_slug(path: str) -> str:
    """A kb-safe, deterministic article source for a page path.

    kb-go derives an article id from the source string, so this must be stable
    across syncs (that is what makes a re-sync update rather than duplicate) and
    must not collide with an owner-uploaded file in the same pocket scope — hence
    the ``site-`` prefix. kb-go also splits on "/", so slashes become dashes here
    rather than being handed over intact.
    """
    # Drop the file extension first so an html lane's "/about.html" and a svelte
    # lane's "/about" describe the same page under the same name.
    stripped = re.sub(r"\.(html?|svelte|md|svx)$", "", path.strip().lower())
    # SvelteKit route scaffolding is not part of the page's identity, so
    # "src/routes/menu/+page" reads as "menu" like the html lane's "menu.html".
    stripped = re.sub(r"^/?src/routes/", "", stripped)
    stripped = re.sub(r"/?\+(page|layout)$", "", stripped)
    cleaned = _SLUG_RE.sub("-", stripped).strip("-")
    # Index pages of any lane land on one name, so "/", "/index.html" and a
    # SvelteKit root route are one article rather than three copies of the homepage.
    if cleaned in ("", "index", "src-routes-page"):
        cleaned = "home"
    return f"site-{cleaned}"[:120]


def _page_text(path: str, body: str, engine: str) -> str:
    if engine == "html":
        return html_to_text(body)
    if engine == "svelte":
        return svelte_to_text(body)
    return _tidy(body)


def _is_page(path: str, engine: str) -> bool:
    lowered = path.lower()
    if engine == "html":
        return lowered.endswith((".html", ".htm"))
    if engine == "svelte":
        # Route files are pages; components they import are pulled in as text too,
        # since a component often holds the pricing table or the hours block.
        return lowered.endswith((".svelte", ".md", ".svx"))
    return False


def extract_site_documents(
    *,
    engine: str,
    ripple_spec: Any = None,
    source: dict[str, Any] | None = None,
) -> list[SiteDocument]:
    """Turn a pocket's stored content into ingestable documents, one per page.

    Pure — no I/O, no Beanie, no kb subprocess — so the lane rules are unit-testable
    on their own. Returns [] when there is nothing to ingest (a foreign site with an
    empty pocket, a spec with no copy), which the caller reports rather than treats
    as a failure.
    """
    docs: list[SiteDocument] = []

    if engine in ("html", "svelte") and isinstance(source, dict):
        # Sorted so a sync is deterministic and the homepage tends to lead.
        for path in sorted(source):
            if len(docs) >= _MAX_DOCUMENTS:
                break
            body = source.get(path)
            if not isinstance(body, str) or not _is_page(path, engine):
                continue
            text = _page_text(path, body, engine)[:_MAX_DOCUMENT_CHARS]
            if len(text) < _MIN_DOCUMENT_CHARS:
                continue
            docs.append(SiteDocument(path=path, source=_path_slug(path), text=text))
        return docs

    # ripple (and anything unrecognised that still carries a spec): the spec is the
    # whole site, so it becomes one document.
    text = spec_to_text(ripple_spec or {})[:_MAX_DOCUMENT_CHARS]
    if len(text) >= _MIN_DOCUMENT_CHARS:
        docs.append(SiteDocument(path="/", source=_path_slug("/"), text=text))
    return docs


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def kb_scope_for_pocket(pocket_id: str) -> str:
    """The scope the page sync writes, and the first of the two a CONCIERGE run
    reads. Mirrors the POCKET half of ``_kb_scopes_for_context``'s concierge branch
    — if that ever changes, this is the other end of the pair. The other half,
    ``agent:<agent_id>``, has a different writer (the owner, through the agent's
    Knowledge tab), which is why a re-sync can never clobber it."""
    return f"pocket:{pocket_id}"


async def _load_pocket_content(site: Any) -> dict[str, Any] | None:
    """The site's pocket as a wire dict, or None when it can't be read.

    Reads AS THE SITE OWNER (``site.owner``) — the same identity ``publish_pocket``
    uses — so the access check is the real one and this can never read a pocket the
    site's owner could not.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    if not site.pocket_id:
        return None
    try:
        return await pockets_service.get(site.pocket_id, site.owner)
    except Exception:  # noqa: BLE001 — a missing/denied pocket is "nothing to sync"
        logger.warning(
            "sites.kb: could not read pocket %s for site %s",
            site.pocket_id,
            getattr(site, "id", "?"),
            exc_info=True,
        )
        return None


async def sync_site_knowledge(site: Any) -> SiteKnowledgeReport:
    """Ingest a site's own content into its pocket KB and prune what it replaced.

    Idempotent: the same content re-syncs to the same article ids (kb-go versions
    them), and ids this site produced previously but no longer produces are deleted,
    so a removed page stops being quotable. Only ids recorded on THIS Site are ever
    deleted — the pocket scope also holds owner-uploaded files and must survive.

    Records ``kb_article_ids`` / ``kb_synced_at`` / ``kb_sync_error`` on the Site so
    the dashboard can show whether the concierge actually has anything to work with.
    """
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    report = SiteKnowledgeReport()
    scope = kb_scope_for_pocket(site.pocket_id or "")
    previous = list(getattr(site, "kb_article_ids", None) or [])

    pocket = await _load_pocket_content(site)
    if pocket is None:
        report.error = "pocket_unavailable"
        await _record_sync(site, report, previous=previous)
        return report

    docs = extract_site_documents(
        engine=pocket.get("engine") or "ripple",
        ripple_spec=pocket.get("rippleSpec") or {},
        source=pocket.get("source") if isinstance(pocket.get("source"), dict) else None,
    )
    if not docs:
        # Nothing to ingest is a real state, not an error: a foreign site's pocket
        # holds no pages. Previously-ingested articles are left alone rather than
        # purged on what may be a transient empty read.
        report.error = "no_content"
        await _record_sync(site, report, previous=previous)
        return report

    for doc in docs:
        try:
            result = await KnowledgeService.ingest_text_to_scope(scope, doc.text, doc.source)
        except Exception:  # noqa: BLE001 — one bad page must not lose the rest
            report.skipped += 1
            logger.warning(
                "sites.kb: ingest failed for %s on site %s",
                doc.path,
                getattr(site, "id", "?"),
                exc_info=True,
            )
            continue
        article_id = str((result or {}).get("article") or "").strip()
        if not article_id:
            report.skipped += 1
            continue
        report.ingested += 1
        report.article_ids.append(article_id)

    if not report.ingested:
        # The site HAS pages and not one of them made it in — the ingest engine is
        # unreachable or broken. That is a failure, not a clean sync of nothing, and
        # saying so is the difference between a dashboard that reads "nothing
        # learned yet" and one that tells the owner something is wrong.
        report.error = "ingest_failed"
        await _record_sync(site, report, previous=previous)
        logger.warning(
            "sites.kb: every page failed to ingest for site %s (%d skipped)",
            getattr(site, "id", "?"),
            report.skipped,
        )
        return report

    # Prune what this site used to publish and no longer does. Anything not in the
    # fresh set is a page that was renamed or deleted.
    #
    # Reachable ONLY after a successful ingest, deliberately: the fresh set is the
    # thing "no longer produced" is measured against, so an empty one from a failed
    # run would mark EVERY existing article stale and delete the site's whole
    # knowledge base over a transient outage.
    stale = [a for a in previous if a not in set(report.article_ids)]
    for article_id in stale:
        if await KnowledgeService.remove_article(scope, article_id):
            report.removed += 1

    await _record_sync(site, report, previous=previous)
    logger.info(
        "sites.kb: synced site %s into %s (ingested=%d removed=%d skipped=%d)",
        getattr(site, "id", "?"),
        scope,
        report.ingested,
        report.removed,
        report.skipped,
    )
    return report


async def _record_sync(site: Any, report: SiteKnowledgeReport, *, previous: list[str]) -> None:
    """Persist the sync bookkeeping on the Site.

    Writes ONLY the three kb_* fields, via ``$set`` rather than a whole-document
    save. This runs in the background, minutes after the publish that scheduled it,
    holding a Site instance snapshotted at that moment — so a full save would
    silently roll back anything written to the same Site in between (a domain
    connected, a subscription stamped). A targeted set touches nothing it does not
    own.

    A failed or empty sync keeps the PREVIOUS article ids: they are still in the KB,
    so forgetting them would strand them beyond the reach of any future prune. The
    reason is recorded so the dashboard can tell "no knowledge yet" from "syncing is
    broken".
    """
    updates = {
        "kb_article_ids": report.article_ids if report.ingested else previous,
        "kb_synced_at": datetime.now(UTC),
        "kb_sync_error": report.error,
    }
    try:
        await site.set(updates)
    except Exception:  # noqa: BLE001 — bookkeeping must not fail the caller
        logger.warning(
            "sites.kb: could not record sync state on site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


async def safe_sync_site_knowledge(site: Any) -> SiteKnowledgeReport:
    """``sync_site_knowledge`` that never raises — the form background callers use."""
    try:
        return await sync_site_knowledge(site)
    except Exception:  # noqa: BLE001 — a KB sync is never a gate on publish or chat
        logger.warning("sites.kb: sync failed for site %s", getattr(site, "id", "?"), exc_info=True)
        return SiteKnowledgeReport(error="sync_failed")


# Background-task keepalive: asyncio holds only a WEAK ref to a bare create_task, so
# a fire-and-forget sync can be collected mid-run. Mirrors the pre-warm scheduler in
# ``sites.service``.
_SYNC_TASKS: set[asyncio.Task[Any]] = set()


def _default_sync_scheduler(coro: Any) -> None:
    """Detach the sync onto the running loop and return immediately. With no running
    loop (a sync call site) the coroutine is closed and skipped. Tests patch this
    module attribute to run the coroutine inline instead."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _SYNC_TASKS.add(task)
    task.add_done_callback(_SYNC_TASKS.discard)


def schedule_site_knowledge_sync(site: Any) -> None:
    """Fire a background KB sync for a site. Never blocks, never raises.

    Ingest compiles articles, which can be slow, so no caller waits on it: a publish
    returns as soon as the site is live and the concierge gains its knowledge a
    moment later. Callers that need the result (the owner's "sync now") await
    ``sync_site_knowledge`` directly.
    """
    _default_sync_scheduler(safe_sync_site_knowledge(site))


__all__ = [
    "SiteDocument",
    "SiteKnowledgeReport",
    "extract_site_documents",
    "html_to_text",
    "kb_scope_for_pocket",
    "safe_sync_site_knowledge",
    "schedule_site_knowledge_sync",
    "spec_to_text",
    "svelte_to_text",
    "sync_site_knowledge",
]
