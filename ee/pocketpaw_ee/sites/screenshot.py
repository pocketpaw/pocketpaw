# ee/pocketpaw_ee/sites/screenshot.py — a deployed site's card shows the page,
# not a title and three pills.
#
# Created 2026-08-07 (SC-1). The sites gallery had no capture primitive at all:
# a card was a tinted globe, a name, a script name, and some badges, so ten
# published sites looked like ten rows of the same card. This module is the one
# job that fixes that — screenshot the page a publish just put live, store the
# bytes in the tenant's blob storage, and remember the URL on the Site as
# ``preview_image_url``.
#
# THE RULE THIS MODULE EXISTS TO UPHOLD: a screenshot can never fail, delay, or
# block a publish. Cloudflare Browser Rendering is a paid, quota'd, network
# service that can time out, 400, or be unconfigured entirely; a site that is
# already deployed and serving must not report failure because a picture of it
# could not be taken. So the whole path is fire-and-forget behind
# ``schedule_site_screenshot`` (never blocks, never raises), the work itself is
# wrapped by ``safe_take_site_screenshot`` (swallows everything), and the caller
# in ``sites.service`` wraps even the scheduling call. The worst outcome is a
# card with no image, which is exactly the pre-SC-1 card.
#
# SHAPE — deliberately the same as ``sites.kb_ingest``, the sibling best-effort
# publish tail: a real coroutine, a ``safe_`` wrapper that cannot raise, a
# module-attribute scheduler tests patch to run inline, and a strong-ref task set
# (asyncio only holds a WEAK ref to a bare create_task, so a fire-and-forget task
# can be garbage-collected mid-run).
#
# SOURCE — the site's own live URL. Unlike kb_ingest, which reads the pocket
# precisely to avoid a server-side fetch of a customer hostname, a screenshot has
# no other source: the point is a picture of the page as deployed. The fetch does
# not happen on our network either — Cloudflare's browser does it — so this is
# not the SSRF surface ``url_crawler`` had to be hardened against. A site with no
# url (a WfP deploy with PAW_CF_SITES_DOMAIN unset) is skipped rather than
# guessed at.
#
# PERSISTENCE — ``site.set({...})``, never ``site.save()``. This runs seconds to
# minutes after the publish that scheduled it, holding a Site instance
# snapshotted at that moment, so a whole-document save would silently roll back
# anything written in between (a connected domain, a stamped subscription). Same
# reasoning as ``kb_ingest._record_sync``.
#
# Updated 2026-08-07 (SC-2 — drafts get art too): a second capture path for a site
# that has NEVER been deployed. A draft has no url, so ``take_site_screenshot``
# correctly skips it and its card stayed art-less forever; the Browser Rendering
# endpoint also accepts ``html``, so a draft is photographed from its own MARKUP
# (assembled by ``sites.draft_markup``) with nothing deployed anywhere. Same three
# rules as the live path — never blocks, never raises, never a gate — plus two of
# its own:
#   * it refuses to run for a site that HAS a url. A live site's picture belongs to
#     the live path, which shoots the page visitors actually see.
#   * it re-reads the Site before recording, and drops the write if the site went
#     live while the shutter was open. The import flow mints a draft and publishes
#     it seconds later, so the two captures genuinely race; without this the slower
#     draft shot could land on top of the live one.
# A draft that cannot be captured is the NORMAL case, not the exotic one (a
# never-built ripple pocket is deliberately not built just for a thumbnail — see
# ``draft_markup.build_allowed``), which is why SC-2 also ships the card's themed
# placeholder rather than relying on this landing.

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The stored image's mime. PNG is the endpoint's default, and staying on the
# default is what keeps us away from the ``quality``-vs-``.png`` 400 (see
# ``CloudflareClient.capture_screenshot``): we never send ``quality``, so we
# never have to send ``type`` either.
_MIME = "image/png"

# Upload ceiling for one screenshot. A 1280x800 png of a marketing page is well
# under a megabyte; this is a sanity bound so a pathological render cannot land a
# huge blob in the tenant's storage.
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024

# What the browser sees. A desktop-width viewport because that is the layout a
# marketing page is designed for, and a 16:10 frame because the card crops to a
# banner — a taller shot would just be cropped away.
_VIEWPORT = {"width": 1280, "height": 800}

# ``fullPage`` is deliberately OFF: the card wants the hero, and a full-page
# capture of a long landing page produces a sliver-thin image once it is scaled
# into a card.
_SCREENSHOT_OPTIONS = {"fullPage": False}

# ``networkidle0`` so fonts and hero images have landed before the shutter, and a
# timeout comfortably inside the client's own 30s HTTP timeout so a slow page
# fails as a clean render timeout rather than a severed connection.
_GOTO_OPTIONS = {"waitUntil": "networkidle0", "timeout": 20_000}

# Where the screenshot lands in the owner's Files panel. Its own folder so a
# workspace that republishes often does not bury the owner's real files.
_FOLDER = "/site-previews"


async def _store_screenshot(site: Any, image: bytes) -> str:
    """Put the bytes in the tenant's blob storage, return the stable URL.

    The same pipeline ``deliver_artifact`` uses — a per-call
    :class:`EEUploadService` over the workspace-scoped storage adapter — so a
    screenshot is a first-class Files row with a permanent, auth-gated link
    (``/api/v1/uploads/{id}``) rather than a presign that would expire while the
    card is still rendering it. Imports are local so importing this module never
    drags the upload stack in.
    """
    from fastapi import UploadFile

    from pocketpaw.uploads.config import DEFAULT_ALLOWED_MIMES, UploadSettings
    from pocketpaw.uploads.factory import build_adapter
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    root = Path.home() / ".pocketpaw" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    adapter = build_adapter(root)
    cfg = UploadSettings(
        max_file_bytes=_MAX_SCREENSHOT_BYTES,
        allowed_mimes=frozenset({_MIME, *DEFAULT_ALLOWED_MIMES}),
        local_root=root,
    )
    svc = EEUploadService(adapter=adapter, meta=MongoFileStore(), cfg=cfg)

    upload = UploadFile(
        file=io.BytesIO(image),
        filename=f"site-{getattr(site, 'id', 'preview')}.png",
        headers={"content-type": _MIME},  # type: ignore[arg-type]
    )
    rec = await svc.upload(
        upload,
        owner_id=site.owner,
        chat_id=None,
        workspace=site.workspace,
        folder_path=_FOLDER,
    )
    return f"/api/v1/uploads/{rec.id}"


async def take_site_screenshot(site: Any, *, cloudflare: Any | None = None) -> str:
    """Screenshot the site's live page, store it, record it on the Site.

    Returns the stored image's URL, or "" when there was nothing to shoot (no
    live url yet) or the shot produced no bytes. Raises on a Cloudflare or
    upload failure — callers on the publish path use
    :func:`safe_take_site_screenshot`, which is the form that cannot.

    ``cloudflare`` is the injectable client seam; production resolves the
    configured one through ``sites.service._cf_client`` so screenshots read the
    SAME account / token / configuration check every other Cloudflare call here
    does, and an unconfigured deployment raises a clean "Cloudflare is not
    configured" instead of a KeyError.
    """
    url = (getattr(site, "url", "") or "").strip()
    if not url:
        # A Workers-for-Platforms deploy with PAW_CF_SITES_DOMAIN unset lands
        # here: the worker uploaded fine, it just has no public address yet.
        # There is no page to photograph, and guessing one would photograph
        # somebody else's.
        logger.debug("sites.screenshot: site %s has no url — nothing to capture", site.id)
        return ""

    from pocketpaw_ee.sites.service import _cf_client

    cf = cloudflare or _cf_client()
    image = await cf.capture_screenshot(
        url=url,
        viewport=dict(_VIEWPORT),
        goto_options=dict(_GOTO_OPTIONS),
        screenshot_options=dict(_SCREENSHOT_OPTIONS),
    )
    if not image:
        return ""

    image_url = await _store_screenshot(site, image)
    # Targeted set, not save() — see the module header. This write happens after
    # the publish returned, so it must touch only the field it owns.
    await site.set({"preview_image_url": image_url})
    logger.info("sites.screenshot: captured preview for site %s", getattr(site, "id", "?"))
    return image_url


async def safe_take_site_screenshot(site: Any, *, cloudflare: Any | None = None) -> str:
    """:func:`take_site_screenshot` that never raises — the form the publish path
    uses. A failed screenshot is logged and the site keeps whatever preview it
    already had (or none): the card falls back to its text layout, which is the
    pre-SC-1 card, and the publish that scheduled this is long since successful.
    """
    try:
        return await take_site_screenshot(site, cloudflare=cloudflare)
    except Exception:  # noqa: BLE001 — a screenshot is never a gate on a publish
        logger.warning(
            "sites.screenshot: capture failed for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )
        return ""


# Background-task keepalive: asyncio holds only a WEAK ref to a bare create_task,
# so a fire-and-forget capture can be collected mid-run. Mirrors the schedulers in
# ``sites.kb_ingest`` and ``sites.service``.
_SCREENSHOT_TASKS: set[asyncio.Task[Any]] = set()


def _default_screenshot_scheduler(coro: Any) -> None:
    """Detach the capture onto the running loop and return immediately. With no
    running loop (a sync call site) the coroutine is closed and skipped. Tests
    patch this module attribute to run the coroutine inline instead."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _SCREENSHOT_TASKS.add(task)
    task.add_done_callback(_SCREENSHOT_TASKS.discard)


def schedule_site_screenshot(site: Any) -> None:
    """Fire a background screenshot for a site. Never blocks, never raises.

    A remote browser render takes seconds, so no publish waits on it: the site
    goes live immediately and its card gains a picture a moment later.
    """
    _default_screenshot_scheduler(safe_take_site_screenshot(site))


# --------------------------------------------------------------------------- #
# SC-2 — the DRAFT path: no url, so shoot the markup instead.
# --------------------------------------------------------------------------- #


async def _still_a_draft(site: Any) -> bool:
    """Re-read the Site and report whether it is STILL a draft.

    Called immediately before recording a draft capture. The import flow mints a
    draft Site and publishes it seconds later, so a draft capture and the live
    capture that follows it genuinely overlap; without this re-read the slower draft
    shot could land on top of the live one and the card would show the page as it
    looked before it was published.

    Fails OPEN — an unreadable / non-Beanie site (every unit-test double) reports
    True and the write proceeds. The failure this guards is cosmetic; refusing to
    write on a doubtful read would cost every draft its picture.
    """
    getter = getattr(type(site), "get", None)
    if getter is None:
        return True
    try:
        fresh = await getter(site.id)
    except Exception:  # noqa: BLE001 — a re-read failure must not cost the capture
        return True
    if fresh is None:
        return False  # deleted while the shutter was open — nothing to write to
    return not (getattr(fresh, "url", "") or "").strip()


async def take_draft_screenshot(site: Any, *, cloudflare: Any | None = None) -> str:
    """Screenshot a DRAFT site from its own markup, store it, record it on the Site.

    Returns the stored image's URL, or "" when there was nothing to shoot: the site
    is live (the live path owns that picture), the draft has no renderable markup
    yet, getting markup would cost a Node build this deployment has not opted into,
    or the shot produced no bytes. Raises on a Cloudflare or upload failure —
    :func:`safe_take_draft_screenshot` is the form that cannot.

    The markup goes over as the endpoint's ``html`` body, which renders at
    ``about:blank``: ``draft_markup`` is what makes that document self-contained.
    """
    if (getattr(site, "url", "") or "").strip():
        # A deployed site — ``take_site_screenshot`` photographs the real page.
        return ""

    from pocketpaw_ee.sites.draft_markup import build_draft_markup

    markup = await build_draft_markup(site)
    if not markup:
        return ""

    from pocketpaw_ee.sites.service import _cf_client

    cf = cloudflare or _cf_client()
    image = await cf.capture_screenshot(
        html=markup,
        viewport=dict(_VIEWPORT),
        goto_options=dict(_GOTO_OPTIONS),
        screenshot_options=dict(_SCREENSHOT_OPTIONS),
    )
    if not image:
        return ""

    if not await _still_a_draft(site):
        logger.debug(
            "sites.screenshot: site %s went live while its draft was being captured "
            "— leaving the live picture alone",
            getattr(site, "id", "?"),
        )
        return ""

    image_url = await _store_screenshot(site, image)
    await site.set({"preview_image_url": image_url})
    logger.info("sites.screenshot: captured draft preview for site %s", getattr(site, "id", "?"))
    return image_url


async def safe_take_draft_screenshot(site: Any, *, cloudflare: Any | None = None) -> str:
    """:func:`take_draft_screenshot` that never raises — the form the draft-mint path
    uses. A draft that cannot be photographed keeps the card's themed placeholder,
    and the create/import that scheduled this is long since successful."""
    try:
        return await take_draft_screenshot(site, cloudflare=cloudflare)
    except Exception:  # noqa: BLE001 — a screenshot is never a gate on a create
        logger.warning(
            "sites.screenshot: draft capture failed for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )
        return ""


def schedule_draft_screenshot(site: Any) -> None:
    """Fire a background draft screenshot. Never blocks, never raises.

    Shares ``_default_screenshot_scheduler`` (and its strong-ref task set) with the
    live path, so a test that patches the scheduler to run inline governs both.
    """
    _default_screenshot_scheduler(safe_take_draft_screenshot(site))


async def safe_take_draft_screenshot_for_pocket(*, workspace_id: str, pocket_id: str) -> str:
    """Look the pocket's canonical Site doc up and capture it as a draft. Never raises.

    The by-pocket form exists for the PREVIEW build. A preview returns a TRANSIENT,
    never-persisted Site-shaped object, so there is nothing there to record a picture
    on — but the real draft doc was minted at create and is sitting in Mongo under the
    same stable per-(workspace, pocket) id ``publish`` upserts.

    Why hang a capture off preview at all: a preview has just built the pocket, so the
    markup is already on disk and the capture costs a millisecond (rung 1) instead of
    the 16s build the create-time capture refuses to spend. It is what makes a
    ripple/svelte draft's card fill in at all under the default policy. On an
    already-LIVE site this resolves a doc with a url and ``take_draft_screenshot``
    declines it, so previewing a live site can never replace the picture of the page
    visitors actually see with a picture of an unapproved edit.
    """
    try:
        from pocketpaw_ee.sites.service import _live_object_id
        from pocketpaw_ee.sites.service import _SiteDoc as _Doc

        doc = await _Doc.find_one(
            {"_id": _live_object_id(workspace_id, pocket_id), "workspace": workspace_id}
        )
        if doc is None:
            return ""
        return await take_draft_screenshot(doc)
    except Exception:  # noqa: BLE001 — a screenshot is never a gate on a preview
        logger.warning(
            "sites.screenshot: draft capture failed for pocket %s", pocket_id, exc_info=True
        )
        return ""


def schedule_draft_screenshot_for_pocket(*, workspace_id: str, pocket_id: str) -> None:
    """Fire a background draft capture for a pocket's Site doc. Never blocks, never
    raises."""
    _default_screenshot_scheduler(
        safe_take_draft_screenshot_for_pocket(workspace_id=workspace_id, pocket_id=pocket_id)
    )


__all__ = [
    "safe_take_draft_screenshot",
    "safe_take_draft_screenshot_for_pocket",
    "safe_take_site_screenshot",
    "schedule_draft_screenshot",
    "schedule_draft_screenshot_for_pocket",
    "schedule_site_screenshot",
    "take_draft_screenshot",
    "take_site_screenshot",
]
