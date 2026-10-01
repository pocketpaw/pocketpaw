# Discover — the source registry.
#
# Created 2026-10-01 (feat/discover-index). A source is a product that publishes
# into Discover (site templates first). It names the ``kinds`` its listings may
# carry and a ``use(workspace_id, user_id, source_id, name)`` that makes the
# caller their own copy and returns the source's result dict. Registration is
# by name and idempotent: registering a name again replaces it.
# ``register_builtin_sources`` runs from ``mount_cloud``.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.site_templates import service as site_templates_service


@dataclass(frozen=True)
class DiscoverSource:
    name: str
    kinds: frozenset[str]
    use: Callable[[str, str, str, str | None], Awaitable[dict]]


_SOURCES: dict[str, DiscoverSource] = {}


def register_source(src: DiscoverSource) -> None:
    _SOURCES[src.name] = src


def get_source(name: str) -> DiscoverSource:
    try:
        return _SOURCES[name]
    except KeyError:
        raise NotFound("discover_source", name) from None


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
        )
    )


__all__ = [
    "DiscoverSource",
    "get_source",
    "register_builtin_sources",
    "register_source",
    "registered_sources",
]
