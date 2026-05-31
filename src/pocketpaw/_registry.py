"""Runtime discovery of extension implementations registered via entry-points.

Implementations declared under a ``pocketpaw.*`` entry-point group (see
`pocketpaw.extensions` for the group names) are loaded lazily on first
access and cached. An OSS install with no `pocketpaw_ee` package on disk
simply finds no entry-points and pays no import cost for cloud features.

Usage::

    from pocketpaw._registry import first, providers, has

    provider = first("pocketpaw.embeddings")
    embedder = provider.build_embedder(settings) if provider else None
"""

from __future__ import annotations

from functools import cache
from importlib.metadata import entry_points
from typing import Any


@cache
def providers(group: str) -> tuple[Any, ...]:
    """Return instantiated providers for *group*, cached for the process.

    Each entry-point must point at a zero-arg callable (typically the
    provider class). A provider whose module fails to import is skipped
    with a logged warning rather than taking the whole process down — a
    broken EE plugin must not break an otherwise-working core.
    """
    import logging

    logger = logging.getLogger(__name__)
    found: list[Any] = []
    for ep in entry_points(group=group):
        try:
            found.append(ep.load()())
        except Exception as exc:  # noqa: BLE001 — isolate plugin failures
            logger.warning("extension %r in group %r failed to load: %s", ep.name, group, exc)
    return tuple(found)


def first(group: str) -> Any | None:
    """Return the first registered provider for *group*, or ``None``."""
    items = providers(group)
    return items[0] if items else None


def has(group: str) -> bool:
    """True if any provider is registered for *group*."""
    return bool(providers(group))


def clear_cache() -> None:
    """Drop the provider cache. For tests that install/remove entry-points."""
    providers.cache_clear()
