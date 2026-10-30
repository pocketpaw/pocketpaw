"""Process-local caches that other processes must drop too.

The settings cache (``get_settings``) and the skill loader are per process.
When the web tier runs as several processes, a write handled by one of them
(saving settings, installing a skill) leaves the others serving the old value.
The functions here clear the local cache and then call the remote invalidator,
which a deployment installs with ``set_remote_invalidator``. OSS installs none,
so the calls stay local. The enterprise cloud installs one that relays the
cache name over its cross-process broadcast when
``POCKETPAW_REALTIME_BUS=redis-streams``; the receiving processes run the
local half only (``clear_settings_cache(local_only=True)`` etc.), so an
invalidation is never relayed twice.

The remote call is fire-and-forget and must not raise into the write path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

SETTINGS = "settings"
SKILLS = "skills"

#: Called with a cache name after the local clear. ``None`` on OSS.
_REMOTE: Callable[[str], None] | None = None


def set_remote_invalidator(fn: Callable[[str], None] | None) -> None:
    """Install (or clear, with ``None``) the cross-process invalidator."""
    global _REMOTE
    _REMOTE = fn


def _notify(name: str) -> None:
    if _REMOTE is None:
        return
    try:
        _REMOTE(name)
    except Exception:
        logger.warning("remote invalidation of %s failed", name, exc_info=True)


def clear_settings_cache(*, local_only: bool = False) -> None:
    """Drop the cached ``Settings`` here and, unless ``local_only``, everywhere."""
    from pocketpaw.config import get_settings

    get_settings.cache_clear()
    if not local_only:
        _notify(SETTINGS)


def reload_skills(*, local_only: bool = False) -> None:
    """Reload the skill loader here and, unless ``local_only``, everywhere.

    Skills are read from local disk, so a process on another host reloads and
    finds only what is installed on that host.
    """
    from pocketpaw.skills import get_skill_loader

    get_skill_loader().reload()
    if not local_only:
        _notify(SKILLS)
