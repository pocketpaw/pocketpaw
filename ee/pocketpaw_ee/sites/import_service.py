# ee/pocketpaw_ee/sites/import_service.py — Paw Sites IMPORT control plane (SI-4).
#
# Edited 2026-09-04 (IR-2a, feat/sites-import-design-brief): added REBUILD mode
# beside the existing byte mirror. ``regenerate_from_url`` validates the same URL
# floors, inserts a queued ``SiteDesignBrief`` and schedules a background capture
# that harvests the page and persists a typed ``DesignBrief``. It mints NO pocket
# and NO Site doc, deliberately — see the section header at the foot of the file.
# The mirror path above is untouched.
#
# Edited 2026-07-23 (SI-FIX review): the zip path now persists the REWIRED source on
# the pocket too (set_imported_source), not just the deployed artifact — a re-publish
# from the builder was reading the raw upload and redeploying un-rewired forms.
#
# Edited 2026-07-23 (SI-FIX — wire the rewire pipeline): both the zip and crawl
# paths now run the unpacked/harvested files through the generator's ``import``
# subcommand (``_plan_import`` -> ``generator_client.run_import``) BEFORE publish, so
# imported ``<form>``s are actually rewired to the capture API and the report carries
# real per-form ``rewired`` verdicts. This replaces the interim Python
# ``derive_import_report`` (which shipped raw source + ``rewired: False``) — the
# rewired source is what we persist on the pocket and deploy. The draft Site doc is
# minted first so its ``signed_key`` can be baked into the forms.
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
from urllib.parse import urljoin, urlparse
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


async def _plan_import(
    *,
    workspace_id: str,
    pocket_id: str,
    source: dict[str, str],
    assets: dict[str, str],
    _run_import: Any | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    """Run the imported files through the generator's import pipeline and return the
    REWIRED ``(source, assets, report)``.

    This is the seam that makes an imported ``<form>`` actually post to the capture
    API. The generator's ``buildImportPlan`` + ``rewireForms`` need the site's
    ``signed_key`` to bake into each form, so the draft Site doc (minted by
    ``_mint_import_pocket``) must already exist — we read its key here and hand it to
    the generator. ``publish`` reuses that same stored key, so the key in the
    deployed forms matches the one the capture endpoint verifies.

    Returns the authoritative report (per-form ``rewired`` verdicts + original
    actions, page titles, asset tallies, warnings) verbatim from the generator; the
    caller merges any path-specific extras (crawl stats, status)."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    oid = sites_service._live_object_id(workspace_id, pocket_id)
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None or not doc.signed_key:
        raise Internal(
            "sites.import_no_draft_key",
            "the import draft site is missing its capture signing key",
        )

    # Merge text (utf-8 -> base64) + binary (already base64) into the single
    # {path: base64} map the generator's ``import`` command ingests.
    files: dict[str, str] = {
        path: base64.b64encode(text.encode("utf-8")).decode("ascii")
        for path, text in source.items()
    }
    files.update(assets)

    from pocketpaw_ee.sites import generator_client

    run = _run_import if _run_import is not None else generator_client.run_import
    try:
        result = await run(
            files,
            site_id=str(oid),
            # The SAME capture base publish passes the generator, so the action baked
            # into the forms matches the site the capture endpoint verifies.
            capture_api_base=sites_service._capture_base(),
            capture_signed_key=doc.signed_key,
        )
    except Exception as exc:  # noqa: BLE001 — fail CLOSED, never fall back to raw source
        # A rewire failure must not silently deploy the un-rewired upload — that
        # would leak the site's leads to its original form backend. Raise a clean
        # error the zip endpoint maps to a 5xx and the crawl path turns into a
        # failed report.
        raise Internal(
            "sites.import_rewire_failed", "the imported site could not be processed"
        ) from exc
    return result["source"], result.get("assets", {}), result["report"]


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
    _run_import: Any | None = None,
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
    # Run the files through the generator's import pipeline: forms get rewired to the
    # capture API, links/titles resolved, and an authoritative report produced. The
    # REWIRED source (not the raw upload) is what we store + deploy.
    source, assets, report = await _plan_import(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        source=source,
        assets=assets,
        _run_import=_run_import,
    )
    report["status"] = "imported"

    # Persist the REWIRED source on the pocket (the durable re-publish source),
    # mirroring the crawl path. The pocket was minted above with the RAW upload to
    # get its signed_key; without this overwrite a later re-publish from the builder
    # would read the raw source and redeploy forms that still post to the origin
    # backend — leaking the site's leads.
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    await pockets_service.set_imported_source(pocket_id, workspace_id=workspace_id, source=source)

    doc = await sites_service.publish(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        ripple_spec=None,
        theme={},
        name=site_name,
        engine="html",
        source=source,
        # ``assets`` is the base64 binary sideband ({path: base64}) the html
        # generator writes verbatim into the static tree.
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
    _run_import: Any | None = None,
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

    try:
        # Same rewire pipeline as the zip path: forms get pointed at the capture
        # API and an authoritative report comes back. The REWIRED source is what we
        # persist + deploy.
        source, assets, report = await _plan_import(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            source=source,
            assets=assets,
            _run_import=_run_import,
        )
    except Exception as exc:  # noqa: BLE001 — a planning failure is a safe failed report
        logger.warning("sites import: planning crawl of %s failed", url, exc_info=True)
        await _mark_import_failed(doc, url=url, message=_safe_crawl_failure(exc))
        return doc
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


# --------------------------------------------------------------------------- #
# Rebuild mode (IR-2a) — capture a DESIGN BRIEF instead of mirroring the bytes
# --------------------------------------------------------------------------- #
# The mirror above re-hosts the source's own files. Rebuild reads the source as a
# DESIGN REFERENCE and hands a typed brief to the generator, which authors a
# native site from it. The two share the URL validation and the crawler, and
# nothing else — in particular this path mints NO pocket and NO Site doc.
#
# WHY IT MINTS NOTHING: the /sites surface routes a run to REFINE whenever a
# pocket_id rides in its meta, engine hint or not. A create flow therefore has to
# run with no pocket, and the agent mints its own through ``create_svelte_site``
# (which stamps engine="svelte" and the right pattern). Pre-minting a pocket here
# to have an id to return would hand the agent the ripple refine toolset pointed
# at an html pocket. It would also stamp IMPORT_PATTERN, which is what
# ``service._refs_must_resolve`` reads to RELAX the publish smoke gate — correct
# for a byte mirror that can reference something the crawl did not harvest, wrong
# for a site we authored ourselves.


class _MetaScan(HTMLParser):
    """Pull the page's own account of itself: title, meta description, favicon
    and og:image.

    A real parser, never a regex: minified markup carries UNQUOTED attributes
    (``<link href=/a.css rel=stylesheet>``), and a quote-assuming pattern returns
    zero matches with no error, which is indistinguishable from a page that
    genuinely declares nothing.
    """

    _ICON_RELS = {"icon", "shortcut icon", "apple-touch-icon", "mask-icon"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.og_title = ""
        self.favicon = ""
        self.og_image = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attr.get("name") or attr.get("property") or "").lower()
            content = attr.get("content", "").strip()
            if not content:
                return
            if key in ("description", "og:description") and not self.description:
                self.description = content
            elif key == "og:title" and not self.og_title:
                self.og_title = content
            elif key == "og:image" and not self.og_image:
                self.og_image = content
        elif tag == "link":
            rels = " ".join((attr.get("rel") or "").lower().split())
            href = attr.get("href", "").strip()
            if href and rels in self._ICON_RELS and not self.favicon:
                self.favicon = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()


def _seed_page_file(url: str, files: dict[str, bytes]) -> tuple[str, bytes]:
    """Find the crawled seed page in the harvest.

    The crawler keys pages by PATH, not by "index.html": ``/landing`` lands at
    ``landing/index.html``. Ask for the seed's own path first, fall back to the
    root, then to the first html file in path order — a seed that redirects to
    another host re-homes the crawl, and that final URL is not returned to us.
    """
    from pocketpaw_ee.sites.url_crawler import _rel_path_for_page

    want = _rel_path_for_page(urlparse(url).path)
    for candidate in (want, "index.html"):
        blob = files.get(candidate)
        if blob:
            return candidate, blob
    for path in sorted(files):
        if path.endswith(".html") and files[path]:
            return path, files[path]
    raise ValidationError(
        "sites.import_no_page", "the crawl of that URL did not yield a readable page"
    )


def _brief_from_crawl(url: str, crawl: Any) -> Any:
    """Turn a harvest into the site-authoring crew's ``DesignBrief``.

    Deliberately the crew's own baton rather than a second brief type: the crew
    threads it Designer → Branding → Frontend, and
    ``surface/handlers/sites.py::_frontend_preamble`` already renders build
    instructions from one and routes to ``create_svelte_site``. IR-2a fills the
    identity layer; the sitemap, design system and asset manifest are each a
    later slice and land empty, which the crew model already tolerates because
    its own stages fill them one at a time too.
    """
    from pocketpaw_ee.sites.design_brief import build_brief_from_source

    warnings = list(crawl.warnings)
    path, blob = _seed_page_file(url, crawl.files)
    scan = _MetaScan()
    try:
        scan.feed(blob.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — a malformed page is a warning, not a failure
        logger.warning("sites rebuild: meta scan of %s failed", path, exc_info=True)
        warnings.append(f"could not read page metadata from {path}")
    title = scan.title or scan.og_title
    if not title:
        warnings.append("the source page declares no title")
    brief = build_brief_from_source(
        source_url=url,
        title=title,
        description=scan.description,
        # Resolved against the SOURCE url so a stored brief carries an absolute
        # address; the crawl's own rewrite made these site-relative, and
        # ``AssetRef`` refuses anything that is not fetchable.
        favicon_url=urljoin(url, scan.favicon) if scan.favicon else None,
        warnings=warnings,
    )

    # IR-4 — the source's own design language, read out of its stylesheets. This
    # is the difference between a site ABOUT the source and one that looks like
    # it: with no design system on the brief the agent has nothing to match and
    # picks a direction of its own, which is exactly what it did.
    from pocketpaw_ee.sites.design_extract import (
        apply_to_brief,
        stylesheets_from_crawl,
    )

    try:
        sheets = stylesheets_from_crawl(crawl.files, blob.decode("utf-8", "replace"))
        brief.open_questions.extend(apply_to_brief(brief, sheets, name=urlparse(url).netloc or url))
    except Exception:  # noqa: BLE001 — a brief without tokens still builds a site
        logger.warning("sites rebuild: token extraction for %s failed", url, exc_info=True)
        brief.open_questions.append(
            "we could not read this site's design tokens; the result will not match its colours"
        )
    return brief


async def _mark_brief_failed(doc: Any, *, message: str) -> None:
    """Stamp a readable failed state on the brief. ``message`` comes from
    ``_safe_crawl_failure``, so it never carries a traceback or upstream text."""
    doc.status = "failed"
    doc.error = message
    await doc.save()


async def capture_design_brief(
    *,
    brief_id: str,
    workspace_id: str,
    url: str,
    _transport: Any | None = None,
    _resolver: Any | None = None,
    _politeness_delay: float | None = None,
) -> Any:
    """Crawl ``url`` and persist a design brief on the queued brief document.

    Runs under the SAME wall-clock cap and the SAME SSRF-pinned crawler as the
    mirror path. Every failure lands as a readable ``failed`` status rather than
    escaping: this runs detached from the request that queued it, so an exception
    here has nobody to return to.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief
    from pocketpaw_ee.sites import url_crawler

    try:
        oid = ObjectId(brief_id)
    except (InvalidId, TypeError):
        raise ValidationError("sites.brief_missing", "Unknown design brief id.") from None
    doc = await SiteDesignBrief.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise ValidationError("sites.brief_missing", "Unknown design brief id.")

    doc.status = "capturing"
    await doc.save()
    try:
        async with asyncio.timeout(MAX_CRAWL_WALL_CLOCK_SEC):
            crawl = await url_crawler.crawl_site(
                url,
                total_byte_cap=MAX_IMPORT_UNCOMPRESSED_BYTES,
                transport=_transport,
                resolver=_resolver,
                politeness_delay=_politeness_delay,
            )
    except Exception as exc:  # noqa: BLE001 — every crawl failure becomes a safe status
        logger.warning("sites rebuild: crawl of %s failed", url, exc_info=True)
        await _mark_brief_failed(doc, message=_safe_crawl_failure(exc))
        return doc

    try:
        brief = _brief_from_crawl(url, crawl)
    except Exception as exc:  # noqa: BLE001 — same rule: a status, never an escape
        logger.warning("sites rebuild: brief build for %s failed", url, exc_info=True)
        await _mark_brief_failed(doc, message=_safe_crawl_failure(exc))
        return doc

    from pocketpaw_ee.sites.design_brief import dump_brief

    doc.brief = dump_brief(brief)
    doc.error = ""
    doc.status = "ready"
    await doc.save()
    _emit_import_journal(
        action="site.design_brief_captured",
        workspace_id=workspace_id,
        user_id=doc.owner,
        pocket_id="",
        site_id="",
        payload={
            "kind": "rebuild",
            "url": url,
            "brief_id": brief_id,
            "open_questions": len(brief.open_questions),
        },
    )
    return doc


async def _run_design_capture(*, brief_id: str, workspace_id: str, url: str) -> None:
    """Background wrapper around ``capture_design_brief`` — it must NEVER let an
    exception escape into the event loop (failures are already stamped on the
    brief inside; this catches only the truly unexpected)."""
    try:
        await capture_design_brief(brief_id=brief_id, workspace_id=workspace_id, url=url)
    except Exception:  # noqa: BLE001 — background task: log, never crash the loop
        logger.exception("sites rebuild: background capture failed for brief %s", brief_id)


async def regenerate_from_url(*, workspace_id: str, user_id: str, url: str) -> dict[str, str]:
    """Queue a REBUILD-mode import: validate the URL (shape + SSRF floors → 422
    before any write), insert a queued design-brief document, schedule the
    background capture, and return ``{brief_id, status: "queued", mode: "rebuild"}``
    immediately (the same 202 contract shape as the mirror path).

    Deliberately mints no pocket and no Site doc — see the section header above.
    """
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief

    await sites_service.require_sites_plan(workspace_id)
    candidate = _validate_import_url(url)
    doc = SiteDesignBrief(
        workspace=workspace_id, owner=user_id, source_url=candidate, status="queued"
    )
    await doc.insert()
    brief_id = str(doc.id)
    _emit_import_journal(
        action="site.rebuild_queued",
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id="",
        site_id="",
        payload={"kind": "from_url_rebuild", "url": candidate, "brief_id": brief_id},
    )
    _default_crawl_scheduler(
        _run_design_capture(brief_id=brief_id, workspace_id=workspace_id, url=candidate)
    )
    return {"brief_id": brief_id, "status": "queued", "mode": "rebuild"}


async def read_design_brief(*, workspace_id: str, brief_id: str) -> Any:
    """Read a captured brief's state for the import panel (IR-2b).

    Returns the four states as themselves rather than collapsing any pair: a
    client that cannot tell ``queued`` from ``failed`` shows a spinner forever on
    a capture that already died, which is the same class of bug the import report
    panel shipped with. ``error`` is already safe text (``_safe_crawl_failure``
    wrote it), so it goes straight to a reader.

    The brief BODY is not returned. Only the goal and the open questions, which
    are the readable half — the baton itself is for the generation run, and
    shipping it to a browser would be sending the whole design system to render
    one status line.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief
    from pocketpaw_ee.sites.design_brief import BriefVersionError, load_brief
    from pocketpaw_ee.sites.dto import ImportBriefStatusResponse

    try:
        oid = ObjectId(brief_id)
    except (InvalidId, TypeError):
        raise ValidationError("sites.brief_missing", "Unknown design brief id.") from None
    doc = await SiteDesignBrief.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise ValidationError("sites.brief_missing", "Unknown design brief id.")

    goal, questions = "", []
    if doc.status == "ready" and doc.brief:
        try:
            brief = load_brief(doc.brief)
            goal, questions = brief.goal, list(brief.open_questions)
        except BriefVersionError as exc:
            # A stored brief this build cannot read is a FAILED capture from the
            # reader's point of view: there is nothing here to generate from, and
            # saying "ready" would start a run against a brief the preamble will
            # also refuse. Recapturing is the fix, so say so.
            logger.warning("sites rebuild: brief %s is unreadable: %s", brief_id, exc)
            return ImportBriefStatusResponse(
                brief_id=brief_id,
                status="failed",
                source_url=doc.source_url,
                error="that capture was saved by a different version — import the URL again",
            )

    return ImportBriefStatusResponse(
        brief_id=brief_id,
        status=doc.status,
        source_url=doc.source_url,
        error=doc.error,
        goal=goal,
        open_questions=questions,
    )
