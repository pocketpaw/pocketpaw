# vendor_keys.py — read an in-tab runtime's vendor key from server config (RR-4).
# Created 2026-07-21 (feat/webcontainer-credentials).
#
# WHY THIS EXISTS: ``browserpod.py`` worked out how to resolve a vendor key on
# this codebase, and every line of that reasoning is about the SHAPE of the
# problem rather than about BrowserPod — these names carry no ``POCKETPAW_``
# prefix, so pydantic-settings never sees them, and whether they reach
# ``os.environ`` at all depends on which entrypoint booted the process. Getting
# that wrong fails SILENTLY: the broker answers ``available: false``, the client
# routes to Daytona, and nothing anywhere reports an error. WebContainers is the
# second runtime with exactly that problem, so the resolution lives here once
# instead of being copied and then drifting.
#
# What deliberately does NOT live here: the per-runtime env var name, the
# logging, and the response shape. Those differ per runtime and belong with the
# runtime. This module resolves a string.
from __future__ import annotations

import os
from collections.abc import Callable


def dotenv_value(name: str) -> str:
    """Read ``name`` from a ``.env`` file without touching the process env.

    Deliberately ``dotenv_values`` and not ``load_dotenv``: this is a READ, and a
    read must not mutate global process state as a side effect. Callers wrap this
    in their own module-level function so their tests have a seam to patch —
    without one, deleting an environment variable in a test would still find the
    developer's real key on disk and the "unconfigured" path could never be
    exercised.
    """
    try:  # pragma: no cover — trivial guard
        from dotenv import dotenv_values
    except ImportError:
        return ""
    return (dotenv_values().get(name) or "").strip()


def read_vendor_key(env_var: str, dotenv_reader: Callable[[], str]) -> str:
    """Return the configured key for ``env_var``, or ``""`` when unset.

    The environment wins over ``.env`` so a real deploy that exports the key
    properly is never shadowed by a stray file. Whitespace-only counts as unset:
    a blank env var handed out as a "key" produces a boot that fails inside the
    vendor's SDK, which is strictly worse than reporting the runtime unavailable
    and letting the caller fall back.
    """
    direct = os.environ.get(env_var, "").strip()
    if direct:
        return direct
    return dotenv_reader()


__all__ = ["dotenv_value", "read_vendor_key"]
