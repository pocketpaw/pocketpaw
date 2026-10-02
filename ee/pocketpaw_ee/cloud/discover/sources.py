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

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.site_templates import service as site_templates_service
from pocketpaw_ee.cloud.site_templates import service_admin as site_templates_admin


@dataclass(frozen=True)
class DiscoverSource:
    name: str
    kinds: frozenset[str]
    use: Callable[[str, str, str, str | None], Awaitable[dict]]
    set_hidden: Callable[[str, bool], Awaitable[None]] | None = None


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


def register_builtin_sources() -> None:
    register_source(
        DiscoverSource(
            name="site_template",
            kinds=frozenset({"site", "tool", "game"}),
            use=_use_site_template,
            set_hidden=site_templates_admin.set_hidden_from_discover,
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
