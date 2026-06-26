"""Process-wide store factories for the local SQLite-backed runtime stores.

Instinct, Fabric and Paw Print keep their state in SQLite under
``~/.pocketpaw/``. These factories return a lazily-created handle so agent
tools, automations and routers share one instance per process (or, for Fabric,
one instance per WORKSPACE — see below).

ISO-1 (2026-06-26 — physical per-workspace isolation): Fabric is no longer a
single process-wide singleton on a SHARED ``~/.pocketpaw/fabric.db``. On a
shared cloud box every tenant used to read and write the same file, with only
the W4a in-row ``workspace_id`` WHERE-filter standing between them. ISO-1 adds
PHYSICAL isolation: each workspace gets its OWN file at
``~/.pocketpaw/workspaces/<workspace_id>/fabric.db`` — mirroring how KB
(``~/.knowledge-base/{scope}/``) and Soul (one ``.soul`` per entity) already
isolate by directory. The W4a WHERE-filter STAYS; physical isolation is
ADDITIVE defense-in-depth, not a replacement.

``get_fabric_store`` resolves the target workspace in this order:

    explicit ``workspace_id=`` arg  →  the ``current_workspace`` ContextVar  →  None

When a workspace resolves, a per-workspace ``FabricStore`` is returned (and the
directory is created), cached in a bounded LRU that ``aclose()``s the handle it
evicts. When NO workspace resolves:

  * if ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` is truthy (cloud mode — set by the
    EE cloud bootstrap, mirroring the ``POCKETPAW_MEMORY_BACKEND`` precedent),
    we RAISE ``WorkspaceScopeRequired`` (fail-closed). A missing workspace on a
    cloud path must NEVER silently fall back to a shared store.
  * otherwise (single-tenant OSS) we return the legacy
    ``~/.pocketpaw/fabric.db`` singleton, so a self-hosted install keeps working
    exactly as before.

The ``current_workspace`` ContextVar lives HERE in OSS core (``pocketpaw``) on
purpose: EE imports from OSS, never the reverse. ISO-3 (a later task) wires the
non-router callers — the agent-tool path, the MCP servers — to SET this
ContextVar per request/stream so they too land in the right file; THIS task only
creates and consults it and wires the EE Fabric router (which already resolves
the workspace) to pass it explicitly.

The factory consults the previously-dormant ``pocketpaw.stores`` entry-point
seam (``StoreProvider``): if EE (or a third party) registers a provider, it gets
first refusal on building the store, so a later task can swap in a cloud-backed
implementation without touching core. An OSS-only install finds no provider and
uses the local SQLite default.

Instinct and Paw Print are UNCHANGED here — still process-wide singletons on the
shared file. Per-workspace Instinct isolation is a separate later task (ISO-2);
the workspace-keyed machinery below is written generically so it can be reused.

The factories moved here from ``pocketpaw_ee/api.py`` in the OSS-EE split
(Phase 3); ``pocketpaw_ee.api`` re-exports from this module for the enterprise
routers that still import via that path.
"""

from __future__ import annotations

import logging
import os
import re
from collections import OrderedDict
from contextvars import ContextVar
from pathlib import Path

from pocketpaw._registry import first
from pocketpaw.fabric.store import FabricStore
from pocketpaw.instinct.store import InstinctStore
from pocketpaw.paw_print.store import PawPrintStore

logger = logging.getLogger(__name__)

_DATA_DIR = Path.home() / ".pocketpaw"

# Entry-point group that supplies a `StoreProvider` (see pocketpaw.extensions).
# EE registers a provider to back stores with cloud-native implementations; an
# OSS install registers none and the local SQLite defaults below are used.
_STORE_PROVIDER_GROUP = "pocketpaw.stores"

# Env flag that turns on fail-closed workspace scoping. Set by the EE cloud
# bootstrap (a later task), mirroring the POCKETPAW_MEMORY_BACKEND precedent in
# ee/pocketpaw_ee/cloud/memory/bootstrap.py. When truthy, a Fabric store request
# that resolves to NO workspace raises instead of returning the shared file.
_REQUIRE_WORKSPACE_SCOPE_ENV = "POCKETPAW_REQUIRE_WORKSPACE_SCOPE"

# Bounded cap on the per-workspace FabricStore handle cache. A FabricStore is a
# cheap object (a path string + an `_initialized` bool; it opens/closes a
# connection per call), so 128 live handles is light — but unbounded growth
# across thousands of tenants on a long-lived process would still leak WAL
# sidecars and handle objects, so we cap and evict LRU, aclose()-ing the victim.
_WORKSPACE_STORE_CACHE_CAP = 128

# STRICT allowlist for a workspace id that is about to name a directory under
# ``workspaces/``. A real workspace id is either a 24-hex Mongo ObjectId
# (e.g. ``69e4f93b57ff64b3903868e3``) or a slug (``ws-acme``, ``w_1``) — all of
# which START with an alphanumeric and then carry only ``[A-Za-z0-9_-]``. We
# validate POSITIVELY against that shape and FAIL CLOSED (raise) on anything
# else, rather than trying to sanitize a hostile id into something "safe". An
# allowlist can't be out-thought by a novel traversal encoding the way a
# denylist can.
#
# The FIRST char must be alphanumeric: this rejects a leading ``-`` (which the
# charset would otherwise allow) so a workspace dir name can never be mistaken
# for an argv flag (``-rf``, ``--force``) if the path is ever handed to a
# subprocess / CLI as a bare argument — a defense-in-depth concern even though a
# leading-dash id stays inside ``workspaces/`` and doesn't itself escape.
#
# Security-critical: this is the load-bearing guard that keeps one tenant's id
# from ever resolving to another tenant's (or the OS's) files, and it governs
# the GENERIC factory so ISO-2's Instinct path inherits it.
_WORKSPACE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")


# ---------------------------------------------------------------------------
# Workspace context + fail-closed error
# ---------------------------------------------------------------------------


class WorkspaceScopeRequired(RuntimeError):
    """Raised when a workspace-scoped store is required but none resolved.

    Fail-closed guard for the cloud path: with
    ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` set, a store request that carries no
    explicit ``workspace_id`` and finds no ``current_workspace`` ContextVar
    value raises this rather than silently reading the shared store — which
    would re-open the very cross-tenant leak physical isolation closes.
    """


# The active workspace for the current execution context. OSS core owns this so
# EE (and the agent-tool / MCP callers wired in ISO-3) can SET it without core
# ever importing EE. ``None`` means "no workspace in context" — the resolution
# in get_fabric_store then falls through to the env-flag decision.
current_workspace: ContextVar[str | None] = ContextVar("current_workspace", default=None)


def _resolve_workspace_id(explicit: str | None) -> str | None:
    """Resolve the effective workspace: explicit arg → ContextVar → None.

    A blank / whitespace-only value (from either source) is treated as "no
    workspace" so an empty string can never name a real per-workspace directory
    NOR satisfy the fail-closed required-scope check.
    """
    candidate = explicit if explicit is not None else current_workspace.get()
    if candidate is None:
        return None
    candidate = candidate.strip()
    return candidate or None


def _require_workspace_scope() -> bool:
    """True when the env flag mandates fail-closed workspace scoping."""
    return os.environ.get(_REQUIRE_WORKSPACE_SCOPE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _safe_workspace_dir(workspace_id: str) -> Path:
    """Map a workspace id to its data dir, FAILING CLOSED on a non-token id.

    Security-critical (ISO-1 physically isolates tenants): the id is about to
    become a directory name under ``workspaces/``, so it must be a single, safe
    path segment. We validate it POSITIVELY against ``_WORKSPACE_ID_RE``
    (``[A-Za-z0-9_-]+`` — covers a 24-hex ObjectId and every slug form) and
    RAISE ``ValueError`` on anything else: empty, a ``/`` or ``\\`` separator,
    ``.`` / ``..``, a space, a null byte, a leading dash that argv could read as
    a flag, any non-ASCII. We do NOT sanitize-and-continue — a hostile id is
    refused outright. The resolved-path containment check below is kept as
    defense-in-depth (a second, independent line) but the allowlist is the
    primary guard, because a denylist can be defeated by a novel encoding.
    """
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.match(workspace_id):
        raise ValueError(
            f"unsafe workspace_id for store path: {workspace_id!r} "
            "(must match [A-Za-z0-9][A-Za-z0-9_-]*)"
        )

    root = (_DATA_DIR / "workspaces").resolve()
    target = (root / workspace_id).resolve()
    # Defense-in-depth: even though the allowlist already forbids separators and
    # dots, re-assert that the resolved real path sits DIRECTLY under the
    # workspaces root. A future loosening of the regex, or a symlinked root,
    # cannot silently let an id escape without tripping this check too.
    if target.parent != root:
        raise ValueError(f"workspace_id escapes workspaces dir: {workspace_id!r}")
    return target


# ---------------------------------------------------------------------------
# Instinct / Paw Print — process-wide singletons (UNCHANGED by ISO-1)
# ---------------------------------------------------------------------------

_instinct_store: InstinctStore | None = None
_paw_print_store: PawPrintStore | None = None


def get_instinct_store() -> InstinctStore:
    """Return the global InstinctStore singleton (``~/.pocketpaw/instinct.db``)."""
    global _instinct_store
    if _instinct_store is None:
        _instinct_store = InstinctStore(_DATA_DIR / "instinct.db")
    return _instinct_store


def get_paw_print_store() -> PawPrintStore:
    """Return the global PawPrintStore singleton (``~/.pocketpaw/paw_print.db``)."""
    global _paw_print_store
    if _paw_print_store is None:
        _paw_print_store = PawPrintStore(_DATA_DIR / "paw_print.db")
    return _paw_print_store


# ---------------------------------------------------------------------------
# Fabric — workspace-keyed physical isolation (ISO-1)
# ---------------------------------------------------------------------------

# Legacy shared singleton (single-tenant OSS / unscoped, non-required path).
_fabric_store: FabricStore | None = None

# Per-workspace handle cache: workspace_id -> FabricStore, ordered by recency so
# we can evict the least-recently-used past the cap. Bounded by
# _WORKSPACE_STORE_CACHE_CAP; the evicted handle is aclose()d.
_workspace_fabric_stores: OrderedDict[str, FabricStore] = OrderedDict()


def _provider_fabric_store(workspace_id: str | None) -> FabricStore | None:
    """Ask the StoreProvider seam for a Fabric store, if one is registered.

    Returns the provider's store, or ``None`` when no provider is installed
    (the OSS case) or the provider declines (returns ``None`` / lacks Fabric).
    A provider that raises is isolated: we log and fall back to the local store
    rather than take the factory down — a broken EE plugin must not break core.
    """
    provider = first(_STORE_PROVIDER_GROUP)
    if provider is None:
        return None
    try:
        store = provider.get_store("fabric", workspace_id=workspace_id)
    except TypeError:
        # Back-compat for a provider whose get_store predates the workspace_id
        # keyword: call it the old way. (Core ships no such provider; this just
        # keeps the seam tolerant.)
        try:
            store = provider.get_store("fabric")
        except Exception:  # noqa: BLE001
            logger.warning("StoreProvider.get_store('fabric') failed", exc_info=True)
            return None
    except Exception:  # noqa: BLE001 — isolate plugin failures
        logger.warning("StoreProvider.get_store('fabric', ...) failed", exc_info=True)
        return None
    if store is not None and not isinstance(store, FabricStore):
        logger.warning(
            "StoreProvider returned a %s for 'fabric', expected FabricStore — ignoring",
            type(store).__name__,
        )
        return None
    return store


def _cache_workspace_store(workspace_id: str, store: FabricStore) -> None:
    """Insert ``store`` as most-recently-used, evicting + closing LRU past cap."""
    _workspace_fabric_stores[workspace_id] = store
    _workspace_fabric_stores.move_to_end(workspace_id)
    while len(_workspace_fabric_stores) > _WORKSPACE_STORE_CACHE_CAP:
        _evict_key, evicted = _workspace_fabric_stores.popitem(last=False)
        _schedule_aclose(evicted)


def _schedule_aclose(store: FabricStore) -> None:
    """Best-effort release of an evicted store's on-disk resources.

    The evicted store holds no live connection, so the only cleanup is a WAL
    checkpoint (see ``FabricStore.aclose``). How we run it depends on the caller:

    * A running event loop is present (the common case — eviction fires from an
      async request building another store): fire-and-forget the async
      ``aclose`` as a task on that loop, so we don't block the request and we
      reuse the live loop's aiosqlite worker.
    * No running loop (a sync caller, or test teardown via
      ``reset_store_caches``): do the checkpoint SYNCHRONOUSLY with stdlib
      ``sqlite3`` instead of spinning up a throwaway ``asyncio.run`` loop. A
      short-lived loop would start an aiosqlite worker thread that races process
      teardown and emits noisy "Event loop is closed" warnings; a plain
      ``sqlite3`` checkpoint is faster and side-effect-free.

    Any failure is swallowed — eviction cleanup must never raise into the caller
    building a DIFFERENT store.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        loop.create_task(_safe_aclose(store))
    else:
        _sync_checkpoint(store)


def _sync_checkpoint(store: FabricStore) -> None:
    """Synchronously checkpoint a store's WAL via stdlib sqlite3 (no event loop).

    Resets the store's ``_initialized`` flag to mirror ``aclose`` so a later
    re-fetch of the same path re-runs ``_ensure_schema`` cleanly. Best-effort:
    a missing DB / WAL is fine and is swallowed.
    """
    import sqlite3

    store._initialized = False
    try:
        with sqlite3.connect(store._db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001 — eviction cleanup is best-effort
        logger.debug("evicted FabricStore sync checkpoint skipped", exc_info=True)


async def _safe_aclose(store: FabricStore) -> None:
    try:
        await store.aclose()
    except Exception:  # noqa: BLE001
        logger.debug("evicted FabricStore aclose failed", exc_info=True)


def get_fabric_store(*, workspace_id: str | None = None) -> FabricStore:
    """Return the FabricStore for the resolved workspace (ISO-1).

    Resolution order: explicit ``workspace_id`` arg → ``current_workspace``
    ContextVar → ``None``. See the module docstring for the full contract.

    * A resolved workspace returns a per-workspace store at
      ``~/.pocketpaw/workspaces/<workspace_id>/fabric.db`` (directory created),
      cached in a bounded LRU.
    * No workspace + ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` truthy → raises
      :class:`WorkspaceScopeRequired` (fail-closed; never a shared read).
    * No workspace + flag unset → the legacy shared ``~/.pocketpaw/fabric.db``
      singleton (single-tenant OSS back-compat).

    A registered ``StoreProvider`` (entry-point group ``pocketpaw.stores``) gets
    first refusal in every branch, so EE can later supply a cloud-backed store.
    """
    resolved = _resolve_workspace_id(workspace_id)

    if resolved is None:
        if _require_workspace_scope():
            # Fail-closed: a cloud deployment must carry a workspace. Never fall
            # back to the shared store — that is the exact cross-tenant leak
            # physical isolation exists to close.
            raise WorkspaceScopeRequired(
                "A workspace-scoped Fabric store is required "
                f"(${_REQUIRE_WORKSPACE_SCOPE_ENV} is set) but no workspace was "
                "resolved from the explicit argument or the current_workspace "
                "context. Refusing to fall back to the shared store."
            )
        # Single-tenant OSS path: legacy shared singleton.
        global _fabric_store
        if _fabric_store is None:
            provided = _provider_fabric_store(None)
            _fabric_store = (
                provided if provided is not None else FabricStore(_DATA_DIR / "fabric.db")
            )
        return _fabric_store

    # Workspace resolved: per-workspace physically-isolated store.
    cached = _workspace_fabric_stores.get(resolved)
    if cached is not None:
        _workspace_fabric_stores.move_to_end(resolved)  # mark MRU
        return cached

    provided = _provider_fabric_store(resolved)
    if provided is not None:
        store = provided
    else:
        ws_dir = _safe_workspace_dir(resolved)
        ws_dir.mkdir(parents=True, exist_ok=True)
        store = FabricStore(ws_dir / "fabric.db")
    _cache_workspace_store(resolved, store)
    return store


def reset_store_caches() -> None:
    """Drop every cached store handle (legacy singletons + per-workspace LRU).

    For tests that install/remove providers, swap the data dir, or need a clean
    factory between cases. Evicted per-workspace handles are aclose()d
    best-effort so a checkpoint runs and no WAL sidecar is left behind.
    """
    global _fabric_store, _instinct_store, _paw_print_store
    _fabric_store = None
    _instinct_store = None
    _paw_print_store = None
    while _workspace_fabric_stores:
        _key, store = _workspace_fabric_stores.popitem(last=False)
        _schedule_aclose(store)
