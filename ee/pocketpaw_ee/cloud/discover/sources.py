# Discover — the source registry.
#
# Created 2026-10-01 (feat/discover-index). A source is a product that publishes
# into Discover (site templates first). It names the ``kinds`` its listings may
# carry and a ``use(workspace_id, user_id, source_id, name)`` that makes the
# caller their own copy and returns the source's result dict. Registration is
# by name and idempotent: registering a name again replaces it.
# ``register_builtin_sources`` runs from ``mount_cloud``.
#
# Updated 2026-10-02 (feat/discover-index, hardening): optional
# ``set_hidden(source_id, hidden)`` so a Discover hide / unhide reaches the
# source item (site templates: ``service_admin.set_hidden_from_discover``);
# ``hide_at_source`` calls it when the source has one.
#
# Updated 2026-10-02 (feat/discover-source-contract): optional ``get_public`` /
# ``iter_public`` make the index source-generic. Both return rows already
# mapped to listing fields plus ``id`` / ``public`` / ``hidden`` (see
# ``DiscoverSource``); the site-template mapping (``name`` -> ``title``) and its
# reindex-time ``live_url`` refresh moved here from ``service_admin``.
#
# Updated 2026-10-02 (feat/studio-templates): the ``studio_template`` source
# (kinds image / video / music). Its rows come from
# ``studio_templates.service_admin`` already in listing shape (absolute media
# URLs, ``media_kind`` / ``media_url``). Its ``use`` returns the template's
# ``{recipe, uses_input_images}`` and creates nothing: the caller runs the
# recipe in Studio themselves.

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.site_templates import service as site_templates_service
from pocketpaw_ee.cloud.site_templates import service_admin as site_templates_admin
from pocketpaw_ee.cloud.studio_templates import service_admin as studio_templates_admin


@dataclass(frozen=True)
class DiscoverSource:
    """A product that publishes into Discover.

    ``get_public(source_id)`` / ``iter_public()`` return Discover rows: the
    ``UpsertListingRequest`` fields (workspace, owner, kind, title, description,
    audiences, preview_image_url, live_url) plus ``id`` (the source id),
    ``public`` (belongs in the index) and ``hidden`` (its listing must stay
    hidden). ``get_public`` returns ``None`` for a missing item; ``iter_public``
    yields every public item, hidden ones included. A source without them can't
    be synced or reindexed."""

    name: str
    kinds: frozenset[str]
    use: Callable[[str, str, str, str | None], Awaitable[dict]]
    set_hidden: Callable[[str, bool], Awaitable[None]] | None = None
    get_public: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None
    iter_public: Callable[[], AsyncIterator[dict[str, Any]]] | None = None


_SOURCES: dict[str, DiscoverSource] = {}


def register_source(src: DiscoverSource) -> None:
    _SOURCES[src.name] = src


def get_source(name: str) -> DiscoverSource:
    try:
        return _SOURCES[name]
    except KeyError:
        raise NotFound("discover_source", name) from None


async def hide_at_source(source: str, source_id: str, hidden: bool) -> None:
    """Carry a Discover hide / unhide to the source item, when the source can."""
    # admin-cross-tenant: Discover moderation hides another workspace's item;
    # the source's setter carries its own marker.
    setter = get_source(source).set_hidden
    if setter is not None:
        await setter(source_id, hidden)


def registered_sources() -> tuple[DiscoverSource, ...]:
    return tuple(_SOURCES.values())


async def _use_site_template(
    workspace_id: str, user_id: str, source_id: str, name: str | None
) -> dict:
    """A new private site pocket from the template, in the caller's workspace."""
    body = {"name": name} if name else {}
    return await site_templates_service.use_template(workspace_id, user_id, source_id, body)


def _site_template_row(row: dict[str, Any]) -> dict[str, Any]:
    """A site template's Discover row as a source row (``name`` -> ``title``)."""
    return {
        "id": row["id"],
        "public": row["public"],
        "hidden": row["hidden"],
        "workspace": row["workspace"],
        "owner": row["owner"],
        "kind": row["kind"],
        "title": row["name"],
        "description": row["description"],
        "audiences": row["audiences"],
        "preview_image_url": row["preview_image_url"],
        "live_url": row["live_url"],
    }


async def _get_site_template(template_id: str) -> dict[str, Any] | None:
    # admin-cross-tenant: the Discover sync reacts to template events from every
    # workspace; the caller only lists rows whose ``public`` is True.
    row = await site_templates_admin.get_for_discover(template_id)
    return _site_template_row(row) if row is not None else None


async def _iter_site_templates() -> AsyncIterator[dict[str, Any]]:
    """Every public template, its ``live_url`` re-read from the source site
    (sites emit no rename / unpublish / delete events)."""
    # admin-cross-tenant: the Discover reindex spans every workspace's public
    # templates.
    for row in await site_templates_admin.iter_public_for_discover():
        # ponytail: one site lookup per public template; batch by pocket id if
        # public templates reach the thousands.
        row["live_url"] = await site_templates_admin.refresh_live_url(row["id"])
        yield _site_template_row(row)


async def _use_studio_template(
    workspace_id: str, user_id: str, source_id: str, name: str | None
) -> dict:
    """The template's recipe for the caller to run in Studio; no copy, no write."""
    # admin-cross-tenant: a user in any workspace remixes another's public
    # template; ``recipe_for_discover`` returns only public, unhidden ones.
    result = await studio_templates_admin.recipe_for_discover(source_id)
    if result is None:
        raise NotFound("studio_template", source_id)
    return result


async def _iter_studio_templates() -> AsyncIterator[dict[str, Any]]:
    # admin-cross-tenant: the Discover reindex spans every workspace's public
    # studio templates.
    for row in await studio_templates_admin.iter_public_for_discover():
        yield row


def register_builtin_sources() -> None:
    register_source(
        DiscoverSource(
            name="site_template",
            kinds=frozenset({"site", "tool", "game"}),
            use=_use_site_template,
            set_hidden=site_templates_admin.set_hidden_from_discover,
            get_public=_get_site_template,
            iter_public=_iter_site_templates,
        )
    )
    register_source(
        DiscoverSource(
            name="studio_template",
            kinds=frozenset({"image", "video", "music"}),
            use=_use_studio_template,
            set_hidden=studio_templates_admin.set_hidden_from_discover,
            get_public=studio_templates_admin.get_for_discover,
            iter_public=_iter_studio_templates,
        )
    )


__all__ = [
    "DiscoverSource",
    "get_source",
    "hide_at_source",
    "register_builtin_sources",
    "register_source",
    "registered_sources",
]
