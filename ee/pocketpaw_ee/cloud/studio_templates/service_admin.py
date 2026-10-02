# Studio templates — cross-tenant reads and writes for the Discover index.
#
# Created 2026-10-02 (feat/studio-templates): Discover (``cloud.discover``)
# mirrors every public studio template as one listing and reads templates only
# through these functions. They are the only template reads that skip the
# workspace filter, and every one carries ``# admin-cross-tenant: <reason>``.
#
# Rows come back already in Discover listing shape (``title``, ``kind``,
# ``preview_image_url``, ``live_url`` = None, ``media_kind``, ``media_url``) plus
# ``id`` / ``public`` / ``hidden``, with the REAL ``workspace`` and ``owner``
# (Discover stores both and keeps them off its public wire). Media URLs are made
# absolute with ``POCKETPAW_PUBLIC_BASE_URL`` (read per call): a logged-out
# Discover viewer is not on the backend origin. ``recipe_for_discover`` serves
# ``use``: the recipe of a public, unhidden template, with no write anywhere.
#
# Updated 2026-10-02 (feat/studio-templates): the base URL comes from
# ``_core.public_url.public_base_url`` (the dup-ratchet's one accessor) instead of
# a direct env read.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.public_url import public_base_url
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import StudioTemplateUpdated
from pocketpaw_ee.cloud.models.studio_template import StudioTemplate
from pocketpaw_ee.cloud.studio_templates.service import _audit, _event_data

#: Template kind -> the media kind a Discover card renders.
_MEDIA_KINDS = {"image": "image", "video": "video", "music": "audio"}


def _absolute(url: str | None) -> str | None:
    if not url or url.startswith(("http://", "https://")):
        return url or None
    return f"{public_base_url()}/{url.lstrip('/')}"


def _discover_row(doc: StudioTemplate) -> dict[str, Any]:
    cover = doc.cover or {}
    preview = {"image": cover.get("url"), "video": cover.get("poster_url")}.get(doc.kind)
    return {
        "id": str(doc.id),
        "public": doc.visibility == "public",
        "hidden": doc.hidden,
        "workspace": doc.workspace,
        "owner": doc.owner,
        "kind": doc.kind,
        "title": doc.title,
        "description": doc.description,
        "audiences": list(doc.audiences),
        "preview_image_url": _absolute(preview),
        "live_url": None,
        "media_kind": _MEDIA_KINDS[doc.kind],
        "media_url": _absolute(cover.get("url")),
    }


async def _get(template_id: str) -> StudioTemplate | None:
    try:
        oid = PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        return None
    return await StudioTemplate.get(oid)


async def get_for_discover(template_id: str) -> dict[str, Any] | None:
    """One template's Discover row by id, from any workspace, or ``None`` when it
    does not exist."""
    # admin-cross-tenant: the Discover sync reacts to template events from every
    # workspace; the caller only lists rows whose ``public`` is True.
    doc = await _get(template_id)
    return _discover_row(doc) if doc is not None else None


async def iter_public_for_discover() -> list[dict[str, Any]]:
    """Every public template's Discover row (hidden ones included, so their
    listings stay hidden rather than vanish), from every workspace."""
    # admin-cross-tenant: the Discover reindex spans every workspace's public
    # templates.
    # ponytail: one full read; page by _id if public templates reach the thousands.
    docs = await StudioTemplate.find(StudioTemplate.visibility == "public").to_list()
    return [_discover_row(doc) for doc in docs]


async def recipe_for_discover(template_id: str) -> dict[str, Any] | None:
    """``{recipe, uses_input_images}`` of a public, unhidden template, else
    ``None``. Reads only: a remix creates nothing."""
    # admin-cross-tenant: a user in any workspace remixes another's public template.
    doc = await _get(template_id)
    if doc is None or doc.visibility != "public" or doc.hidden:
        return None
    return {"recipe": doc.recipe, "uses_input_images": doc.uses_input_images}


async def set_hidden_from_discover(template_id: str, hidden: bool) -> None:
    """Mirror a Discover hide / unhide onto the template, so making it private
    and public again can't launder a hide. Unhiding clears its reports. A
    missing template is a no-op."""
    # admin-cross-tenant: Discover moderation acts on any workspace's template.
    try:
        oid = PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        return
    changes: dict[str, Any] = {"hidden": hidden, "updatedAt": datetime.now(UTC)}
    if not hidden:
        changes["reports"] = []
    res = await StudioTemplate.get_pymongo_collection().update_one(
        {"_id": oid, "hidden": {"$ne": hidden}}, {"$set": changes}
    )
    if not res.modified_count:
        # no-event: missing, or already in that state.
        return
    doc = await StudioTemplate.get(oid)
    # Re-enters Discover's listener, which re-syncs the listing idempotently.
    await emit(StudioTemplateUpdated(data=_event_data(doc)))
    await _audit(
        doc.workspace,
        "system",
        "studio_template.hidden" if hidden else "studio_template.unhidden",
        template_id,
        via="discover",
    )


__all__ = [
    "get_for_discover",
    "iter_public_for_discover",
    "recipe_for_discover",
    "set_hidden_from_discover",
]
