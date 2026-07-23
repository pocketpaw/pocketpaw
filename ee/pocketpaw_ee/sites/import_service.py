# ee/pocketpaw_ee/sites/import_service.py — Paw Sites IMPORT control plane (SI-4).
#
# Created 2026-07-22 (feat/sites-import-endpoint): the service half of the two
# import endpoints — POST /sites/import (zip upload) and POST /sites/import/from-url
# (crawler-backed, next slice). Owns:
#   * SAFE zip unpacking, fully in memory: zip-slip guard (absolute paths, drive
#     letters, backslashes, ``..`` traversal all rejected), an entry-count cap and a
#     total-uncompressed-size cap (decompression-bomb guard), junk filtering
#     (__MACOSX/, .DS_Store), and single-root flattening (a zip whose only top-level
#     dir holds index.html imports as if that dir were the root).
#   * Text/binary split by CONTENT sniff (NUL byte or non-UTF-8 → binary), building
#     the generator input: text files ride ``source`` ({path: text}) — the existing
#     html-engine source map — and binary files ride ``assets`` ({path: base64}).
#     CROSS-REPO SEAM: the generator's ``assets`` base64 sideband is being added in a
#     parallel paw-sites slice; this codes to that contract (see generator_client).
#   * The import flow: mint the html POCKET (the durable source of truth, same as
#     every other site) → mint the DRAFT Site doc (create_draft_site — the
#     draft-first pattern) → publish through the EXISTING html/static deploy path →
#     persist an ``import_report`` on the Site doc → best-effort Journal event.
#   * A MINIMAL import report derived from the zip contents (pages + titles, asset
#     count/bytes, forms with their original actions, script refs). ENRICHMENT SEAM:
#     the generator-side import plan (form rewiring verdicts etc.) replaces this
#     derivation once the parallel paw-sites slice lands — ``rewired`` is False until
#     the generator confirms it.
#   * from-url (SI-5, updated 2026-07-23 feat/sites-import-crawler): full seed
#     validation (SSRF shape floors from url_crawler — scheme/port/credentials/
#     literal-IP checks → 422 BEFORE anything is minted) + draft Site mint + a
#     queued report, then the crawl runs as a DETACHED BACKGROUND TASK (the
#     pre-warm scheduler pattern from sites/service.py — patchable module attr,
#     strong-ref task set) under a hard wall-clock cap. ``crawl_site_from_url``
#     is now REAL: crawl (url_crawler — SSRF-pinned fetcher, same-host BFS,
#     robots, caps) → the SAME pipeline as zip (text/binary split → publish with
#     the assets sideband) → import_report with crawl stats; every failure mode
#     lands as a safe ``status:"failed"`` report, never a stack trace.
#     WHY background, not the workspace-jobs (ARQ) machinery: the jobs worker's
#     writeback merges result state into the pocket's rippleSpec — an imported
#     html pocket has no spec, so a successful crawl could be marked failed by
#     the writeback gate. Tradeoff (documented): a web-process restart drops an
#     in-flight crawl and the report stays "queued" — acceptable for v1, the
#     durable-queue upgrade is a follow-up.
# Tenancy: every entry point takes workspace_id/user_id and funnels through the
# tenant-scoped pockets + sites services; the plan gate (require_sites_plan) runs
# before any write.
# Edited 2026-07-23 (security review): crafted/abnormal zip members (bad CRC,
# encrypted, exotic compression) now map to 422 instead of escaping as 500s;
# entry names carrying control characters are rejected as unsafe.

from __future__ import annotations

import asyncio
import base64
import io
import logging
import zipfile
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from pocketpaw_ee.cloud._core.errors import CloudError, Internal, ValidationError
from pocketpaw_ee.sites import service as sites_service

logger = logging.getLogger(__name__)

# Upload cap for the zip itself (enforced at the router while reading the upload,
# re-checked here for direct service callers).
MAX_IMPORT_ZIP_BYTES = 25 * 1024 * 1024
# Decompression-bomb guards: a hostile zip can be tiny on the wire yet explode on
# extract. Cap both the entry count and the TOTAL uncompressed size (checked
# incrementally while reading, so a bomb is rejected before it is fully inflated).
MAX_IMPORT_ENTRIES = 2000
MAX_IMPORT_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
# Per-URL shape cap for from-url imports.
MAX_IMPORT_URL_LENGTH = 2048
# Hard wall-clock cap on one background crawl+import run (SI-5). The crawl's own
# caps (pages/bytes/redirects/per-fetch timeout) bound it well below this in
# practice; the timeout is the backstop against a slow-loris target.
MAX_CRAWL_WALL_CLOCK_SEC = 120

# The pocket authoring pattern stamped on imported sites so the gallery can badge
# them and later slices (crawler, re-import) can find them.
IMPORT_PATTERN = "imported"

# macOS zip junk that should never become site files.
_JUNK_PREFIXES = ("__MACOSX/",)
_JUNK_BASENAMES = {".DS_Store", "Thumbs.db"}


def _safe_entry_path(raw_name: str) -> str:
    """Normalize one zip entry name to a safe, relative POSIX path.

    Zip-slip guard: rejects backslashes (Windows-style traversal), absolute paths,
    drive letters, ``..`` components, and empty/degenerate names. Raises
    ``ValidationError('sites.import_zip_entry_unsafe')`` — the whole import fails
    closed on ONE bad entry (a crafted archive is hostile; do not cherry-pick)."""
    name = raw_name.strip()
    if "\\" in name:
        raise ValidationError(
            "sites.import_zip_entry_unsafe",
            f"Zip entry {raw_name!r} contains a backslash — not a safe archive path.",
        )
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        raise ValidationError(
            "sites.import_zip_entry_unsafe",
            f"Zip entry {raw_name!r} is an absolute path — archives must be relative.",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValidationError(
            "sites.import_zip_entry_unsafe",
            f"Zip entry {raw_name!r} contains control characters — not a safe archive path.",
        )
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        raise ValidationError(
            "sites.import_zip_entry_unsafe",
            f"Zip entry {raw_name!r} normalizes to an empty path.",
        )
    if ".." in parts:
        raise ValidationError(
            "sites.import_zip_entry_unsafe",
            f"Zip entry {raw_name!r} traverses outside the archive root ('..').",
        )
    return "/".join(parts)


def _is_junk(path: str) -> bool:
    """macOS/Windows archive junk (resource forks, Finder metadata)."""
    if any(path.startswith(p) for p in _JUNK_PREFIXES):
        return True
    return path.rsplit("/", 1)[-1] in _JUNK_BASENAMES


def _is_text(data: bytes) -> bool:
    """Content sniff: NUL byte or invalid UTF-8 → binary; else text. Extension is
    deliberately NOT trusted (a .png named .html must still ride the binary
    sideband, or the generator would corrupt it writing it as text)."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _flatten_single_root(entries: dict[str, bytes]) -> dict[str, bytes]:
    """If the archive has NO root index.html but exactly ONE top-level directory
    that contains one (the `zip -r site site/` shape every OS zipper produces),
    strip that directory prefix so the site imports rooted correctly."""
    if "index.html" in entries:
        return entries
    tops = {p.split("/", 1)[0] for p in entries}
    if len(tops) != 1:
        return entries
    top = next(iter(tops))
    prefix = f"{top}/"
    if not all(p.startswith(prefix) for p in entries) or f"{prefix}index.html" not in entries:
        return entries
    return {p[len(prefix) :]: data for p, data in entries.items()}


def unpack_zip(data: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """Unpack an uploaded site zip IN MEMORY into (text source map, base64 asset map).

    Guards (all fail closed with a 4xx-mapped ValidationError):
      * ``sites.import_zip_invalid`` — not a readable zip;
      * ``sites.import_zip_too_large`` — the archive itself over MAX_IMPORT_ZIP_BYTES,
        or the total UNCOMPRESSED size over MAX_IMPORT_UNCOMPRESSED_BYTES
        (decompression bomb; checked incrementally, never fully inflated first);
      * ``sites.import_zip_too_many_entries`` — over MAX_IMPORT_ENTRIES;
      * ``sites.import_zip_entry_unsafe`` — zip-slip (absolute / ``..`` / backslash /
        control characters — NUL or newline in a to-be-written path is generator input
        the other side of the seam should never have to defend against);
      * ``sites.import_no_index`` — no index.html at the (flattened) root, which the
        html deploy path requires (its static smoke gates on it)."""
    if len(data) > MAX_IMPORT_ZIP_BYTES:
        raise ValidationError(
            "sites.import_zip_too_large",
            f"Import zip exceeds the {MAX_IMPORT_ZIP_BYTES // (1024 * 1024)}MB upload cap.",
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ValidationError(
            "sites.import_zip_invalid", "The uploaded file is not a readable zip archive."
        ) from exc

    entries: dict[str, bytes] = {}
    total_uncompressed = 0
    for info in infos:
        if info.is_dir():
            continue
        path = _safe_entry_path(info.filename)
        if _is_junk(path):
            continue
        if len(entries) >= MAX_IMPORT_ENTRIES:
            raise ValidationError(
                "sites.import_zip_too_many_entries",
                f"Import zip has more than {MAX_IMPORT_ENTRIES} files.",
            )
        # Incremental bomb guard: trust neither the header's file_size (it can lie)
        # nor a single up-front sum — count REAL inflated bytes as we read, and stop
        # the moment the running total crosses the cap.
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_IMPORT_UNCOMPRESSED_BYTES:
            raise ValidationError(
                "sites.import_zip_too_large",
                "Import zip inflates past the uncompressed-size cap (decompression bomb?).",
            )
        try:
            with zf.open(info) as fh:
                blob = fh.read(MAX_IMPORT_UNCOMPRESSED_BYTES + 1)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            # Per-entry reads raise outside the ZipFile() try above: bad CRC and
            # lying size headers surface as BadZipFile, password-protected
            # members as RuntimeError, exotic compression methods as
            # NotImplementedError. All are a malformed/hostile-or-unsupported
            # archive, not a server fault — map to the same 422 contract.
            raise ValidationError(
                "sites.import_zip_invalid",
                f"Zip entry {info.filename!r} is unreadable "
                "(corrupt, encrypted, or an unsupported compression method).",
            ) from exc
        total_uncompressed += max(0, len(blob) - info.file_size)  # header lied → true bytes
        if total_uncompressed > MAX_IMPORT_UNCOMPRESSED_BYTES:
            raise ValidationError(
                "sites.import_zip_too_large",
                "Import zip inflates past the uncompressed-size cap (decompression bomb?).",
            )
        entries[path] = blob

    entries = _flatten_single_root(entries)
    if "index.html" not in entries:
        raise ValidationError(
            "sites.import_no_index",
            "The zip has no index.html at its root — an importable site needs one.",
        )

    source: dict[str, str] = {}
    assets: dict[str, str] = {}
    for path, blob in entries.items():
        if _is_text(blob):
            source[path] = blob.decode("utf-8")
        else:
            assets[path] = base64.b64encode(blob).decode("ascii")
    return source, assets


class _PageScan(HTMLParser):
    """One-pass scan of an imported HTML page: <title>, <form action=...>, and
    <script src=...> refs — the raw material for the minimal import report."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self.form_actions: list[str] = []
        self.script_srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "form":
            self.form_actions.append(attr_map.get("action", ""))
        elif tag == "script" and attr_map.get("src"):
            self.script_srcs.append(attr_map["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def derive_import_report(source: dict[str, str], assets: dict[str, str]) -> dict[str, Any]:
    """Derive the MINIMAL import report from the unpacked zip contents.

    Shape: {pages: [{path, title}], asset_count, asset_bytes,
            forms: [{page, original_action, rewired}], scripts: [...], warnings: [...]}.

    ENRICHMENT SEAM (cross-repo): the paw-sites generator's import plan will
    provide the authoritative report — including per-form REWIRING verdicts (the
    generator rewrites <form> actions to the native capture POST) — in a parallel
    slice. Until it lands, this derivation reports what the zip CONTAINS and marks
    every form ``rewired: False`` (nothing is confirmed rewired yet)."""
    pages: list[dict[str, str]] = []
    forms: list[dict[str, Any]] = []
    scripts: list[str] = []
    for path in sorted(source):
        if not path.endswith((".html", ".htm")):
            continue
        scan = _PageScan()
        try:
            scan.feed(source[path])
        except Exception:  # noqa: BLE001 — a malformed page still lists, just untitled
            logger.debug("import report: page %s did not parse cleanly", path)
        pages.append({"path": path, "title": scan.title.strip()})
        forms.extend(
            {"page": path, "original_action": action, "rewired": False}
            for action in scan.form_actions
        )
        scripts.extend(src for src in scan.script_srcs if src not in scripts)

    asset_bytes = sum(len(base64.b64decode(b64)) for b64 in assets.values())
    warnings: list[str] = []
    if forms:
        warnings.append(
            "form rewiring is confirmed by the generator-side import plan (pending "
            "paw-sites slice) — forms are listed but not yet verified as rewired"
        )
    if assets:
        warnings.append(
            "binary assets deploy at import time; re-publishing from the builder will "
            "not carry them until the pocket asset sideband lands"
        )
    return {
        "pages": pages,
        "asset_count": len(assets),
        "asset_bytes": asset_bytes,
        "forms": forms,
        "scripts": scripts,
        "warnings": warnings,
    }


def _emit_import_journal(
    *,
    action: str,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    site_id: str,
    payload: dict[str, Any],
) -> None:
    """Append the import Journal event (best-effort, mirrors versions/service.py's
    ``_emit_version_event``). The Site doc + import_report are the durable record;
    the event is the audit echo, so a journal-less context degrades silently."""
    try:
        from soul_protocol.spec.journal import Actor, EventEntry

        from pocketpaw.journal_dep import get_journal
    except Exception:  # noqa: BLE001 — journal dep unavailable on a fork
        logger.debug("journal dep unavailable — skipping %s event", action)
        return

    scope = [f"pocket:{pocket_id}", f"site:{site_id}", f"workspace:{workspace_id}"]
    event = EventEntry(
        id=uuid4(),
        ts=datetime.now(UTC),
        actor=Actor(kind="user", id=user_id, scope_context=scope),
        action=action,
        scope=scope,
        payload=payload,
    )
    try:
        get_journal().append(event)
    except Exception:  # noqa: BLE001 — audit echo must not break the import
        logger.warning("sites import: failed to append %s journal event", action, exc_info=True)


async def _mint_import_pocket(
    *, workspace_id: str, user_id: str, name: str, source: dict[str, str]
) -> str:
    """Create the html POCKET an import is rooted on (the durable source of truth
    every other site flow — publish/preview/status/versions — already reads), then
    mint the DRAFT Site doc for it (create_draft_site — the draft-first pattern, so
    the site lists in the gallery and publish flips the SAME doc live)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name=name,
        type_="site",
        pattern=IMPORT_PATTERN,
        ripple_spec=None,
        engine="html",
        source=source,
        # The source is the user's own uploaded files, not LLM-drafted ripple JSON —
        # there is no catalog gate to run (same rationale as the svelte create path).
        trusted=True,
    )
    if err is not None or pocket_id is None:
        raise Internal("sites.import_pocket_failed", f"Could not create the site pocket: {err}")
    await sites_service.create_draft_site(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, name=name
    )
    return pocket_id


async def import_zip_site(
    *,
    workspace_id: str,
    user_id: str,
    data: bytes,
    name: str = "",
    _generator: Any | None = None,
    _cloudflare: Any | None = None,
    _local_deploy: Any | None = None,
) -> Any:
    """Import an uploaded site zip end to end: unpack safely → mint pocket + draft
    Site doc → publish through the EXISTING html/static deploy path (with the
    binary ``assets`` sideband) → persist the import_report → Journal event.

    Returns the live Site doc (import_report set). The generator / CF / local-deploy
    seams forward to ``publish`` so tests never shell out to bun/workerd."""
    # Plan gate FIRST — before any unpack work or pocket write — mirroring
    # publish_pocket's gate-before-read ordering.
    await sites_service.require_sites_plan(workspace_id)

    source, assets = unpack_zip(data)
    site_name = name.strip() or "Imported site"
    pocket_id = await _mint_import_pocket(
        workspace_id=workspace_id, user_id=user_id, name=site_name, source=source
    )
    report = derive_import_report(source, assets)

    doc = await sites_service.publish(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        ripple_spec=None,
        theme={},
        name=site_name,
        engine="html",
        source=source,
        # CROSS-REPO SEAM: ``assets`` is the base64 binary sideband the paw-sites
        # generator is gaining in a parallel slice ({path: base64} written verbatim
        # into the static tree). Until that lands, a generator that ignores the key
        # deploys the text tree only — the report's warning covers it.
        assets=assets or None,
        pattern=IMPORT_PATTERN,
        _generator=_generator,
        _cloudflare=_cloudflare,
        _local_deploy=_local_deploy,
    )
    doc.import_report = report
    await doc.save()

    _emit_import_journal(
        action="site.imported",
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        site_id=str(doc.id),
        payload={
            "kind": "zip",
            "pages": len(report["pages"]),
            "asset_count": report["asset_count"],
            "asset_bytes": report["asset_bytes"],
            "forms": len(report["forms"]),
        },
    )
    return doc


def _validate_import_url(url: str) -> str:
    """Validate a from-url import target: shape (http(s), a real host, sane length)
    PLUS the crawler's SSRF shape floors — no credentials in the URL, no ports
    beyond 80/443, and a literal-IP host must be publicly routable (loopback /
    private / link-local / metadata / CGNAT all 422 here, before anything is
    minted). Hostname RESOLUTION is checked later, inside the pinned fetcher."""
    candidate = (url or "").strip()
    if not candidate or len(candidate) > MAX_IMPORT_URL_LENGTH:
        raise ValidationError("sites.import_url_invalid", "A non-empty http(s) URL is required.")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(
            "sites.import_url_invalid",
            "The import URL must be an absolute http(s) URL with a host.",
        )
    from pocketpaw_ee.sites import url_crawler

    url_crawler.validate_seed_url(candidate)
    return candidate


def _safe_crawl_failure(exc: BaseException) -> str:
    """Map a crawl/import failure to a SAFE message for the import report (which
    every site viewer can read): our own errors carry fixed, safe text; anything
    else degrades to a generic message. Full detail stays in the log only."""
    from pocketpaw_ee.sites.url_crawler import CrawlError

    if isinstance(exc, TimeoutError):
        return f"crawl exceeded the {MAX_CRAWL_WALL_CLOCK_SEC}s wall-clock cap"
    if isinstance(exc, (CrawlError, CloudError)):
        return str(exc)
    return "crawl failed"


async def _mark_import_failed(doc: Any, *, url: str, message: str) -> None:
    """Stamp a clear failed-state import report on the draft Site doc — the UI's
    signal that the from-url import ended. Never a traceback, never raw upstream
    text (``message`` comes from ``_safe_crawl_failure``)."""
    doc.import_report = {
        "pages": [],
        "asset_count": 0,
        "asset_bytes": 0,
        "forms": [],
        "scripts": [],
        "warnings": [],
        "status": "failed",
        "error": message,
        "source_url": url,
    }
    await doc.save()


async def crawl_site_from_url(
    *,
    workspace_id: str,
    user_id: str,
    site_id: str,
    url: str,
    _transport: Any | None = None,
    _resolver: Any | None = None,
    _politeness_delay: float | None = None,
    _generator: Any | None = None,
    _cloudflare: Any | None = None,
    _local_deploy: Any | None = None,
) -> Any:
    """SI-5: crawl ``url`` (same-site, SSRF-pinned — see url_crawler) and run the
    harvest through the SAME import pipeline as the zip path: text/binary split →
    persist the source on the pocket (durable re-publish source) → publish with the
    assets sideband → import_report with crawl stats → Journal event.

    Runs under a hard wall-clock cap. EVERY failure mode (unreachable seed, seed
    blocked by robots, byte budget exceeded, deploy failure, timeout) marks the
    draft Site's report ``status:"failed"`` with a safe error message and returns
    the doc — it never raises into the background scheduler and never surfaces a
    stack trace. The ``_transport``/``_resolver`` seams keep tests off the network;
    the generator/CF/local-deploy seams forward to ``publish`` as in the zip path."""
    from bson import ObjectId
    from bson.errors import InvalidId

    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
    from pocketpaw_ee.sites import url_crawler

    try:
        oid = ObjectId(site_id)
    except (InvalidId, TypeError):
        raise ValidationError("sites.import_site_missing", "Unknown import site id.")
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise ValidationError("sites.import_site_missing", "Unknown import site id.")
    pocket_id = doc.pocket_id

    try:
        async with asyncio.timeout(MAX_CRAWL_WALL_CLOCK_SEC):
            crawl = await url_crawler.crawl_site(
                url,
                total_byte_cap=MAX_IMPORT_UNCOMPRESSED_BYTES,
                transport=_transport,
                resolver=_resolver,
                politeness_delay=_politeness_delay,
            )
    except Exception as exc:  # noqa: BLE001 — every crawl failure becomes a safe report
        logger.warning("sites import: crawl of %s failed", url, exc_info=True)
        await _mark_import_failed(doc, url=url, message=_safe_crawl_failure(exc))
        return doc

    source: dict[str, str] = {}
    assets: dict[str, str] = {}
    for path, blob in crawl.files.items():
        if _is_text(blob):
            source[path] = blob.decode("utf-8")
        else:
            assets[path] = base64.b64encode(blob).decode("ascii")
    if "index.html" not in source:
        await _mark_import_failed(
            doc, url=url, message="the crawl did not yield an importable index.html"
        )
        return doc

    report = derive_import_report(source, assets)
    report["warnings"].extend(crawl.warnings)
    report["crawl"] = crawl.stats.as_dict()
    report["status"] = "imported"
    report["source_url"] = url
    site_name = doc.name or f"Import of {urlparse(url).netloc}"

    try:
        # Persist the harvested source on the POCKET (the durable source of truth —
        # re-publish from the builder reads it), then publish exactly like the zip
        # path. Entity isolation: the Pocket Beanie write lives in pockets.service.
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        await pockets_service.set_imported_source(
            pocket_id, workspace_id=workspace_id, source=source
        )
        doc = await sites_service.publish(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            ripple_spec=None,
            theme={},
            name=site_name,
            engine="html",
            source=source,
            assets=assets or None,
            pattern=IMPORT_PATTERN,
            _generator=_generator,
            _cloudflare=_cloudflare,
            _local_deploy=_local_deploy,
        )
    except Exception as exc:  # noqa: BLE001 — deploy failures also become safe reports
        logger.warning("sites import: deploy of crawled %s failed", url, exc_info=True)
        fresh = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
        if fresh is not None:
            await _mark_import_failed(fresh, url=url, message=_safe_crawl_failure(exc))
            return fresh
        return doc

    doc.import_report = report
    await doc.save()

    _emit_import_journal(
        action="site.imported",
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        site_id=str(doc.id),
        payload={
            "kind": "from_url",
            "url": url,
            "pages": len(report["pages"]),
            "asset_count": report["asset_count"],
            "crawl": report["crawl"],
        },
    )
    return doc


# Background-task keepalive — mirrors sites/service.py's pre-warm scheduler:
# asyncio holds only a weak ref to a bare create_task, so hold a strong ref until
# done. Tests patch ``_default_crawl_scheduler`` to capture the coroutine.
_CRAWL_TASKS: set[asyncio.Task[None]] = set()


def _default_crawl_scheduler(coro: Any) -> None:
    """Detach the crawl coroutine as a background task on the running loop and
    return immediately (the endpoint's 202 must not wait on the crawl). With no
    running loop, close the coroutine and skip — the report stays ``queued``."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _CRAWL_TASKS.add(task)
    task.add_done_callback(_CRAWL_TASKS.discard)


async def _run_url_import(*, workspace_id: str, user_id: str, site_id: str, url: str) -> None:
    """The background wrapper around ``crawl_site_from_url`` — it must NEVER let an
    exception escape into the event loop (failures are already stamped on the
    report inside; this catches only the truly unexpected)."""
    try:
        await crawl_site_from_url(
            workspace_id=workspace_id, user_id=user_id, site_id=site_id, url=url
        )
    except Exception:  # noqa: BLE001 — background task: log, never crash the loop
        logger.exception("sites import: background url import failed for site %s", site_id)


async def import_from_url(*, workspace_id: str, user_id: str, url: str) -> dict[str, str]:
    """Queue a from-url import: validate the URL (shape + SSRF floors → 422 before
    any write), mint the pocket + DRAFT Site doc (status stays draft / not
    deployed), stamp a queued import_report, schedule the background crawl
    (SI-5 — ``crawl_site_from_url`` under the wall-clock cap), and return
    {site_id, pocket_id, status: "queued"} immediately (the 202 contract)."""
    await sites_service.require_sites_plan(workspace_id)
    candidate = _validate_import_url(url)
    host = urlparse(candidate).netloc
    site_name = f"Import of {host}"
    pocket_id = await _mint_import_pocket(
        workspace_id=workspace_id, user_id=user_id, name=site_name, source={}
    )
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    oid = sites_service._live_object_id(workspace_id, pocket_id)
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is not None:
        doc.import_report = {
            "pages": [],
            "asset_count": 0,
            "asset_bytes": 0,
            "forms": [],
            "scripts": [],
            "warnings": [
                f"crawler queued — the crawl of {candidate} runs in the background; "
                "refresh the site to see the import report"
            ],
            "status": "queued",
            "source_url": candidate,
        }
        await doc.save()

    _emit_import_journal(
        action="site.import_queued",
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        site_id=str(oid),
        payload={"kind": "from_url", "url": candidate},
    )
    _default_crawl_scheduler(
        _run_url_import(workspace_id=workspace_id, user_id=user_id, site_id=str(oid), url=candidate)
    )
    return {"site_id": str(oid), "pocket_id": pocket_id, "status": "queued"}
