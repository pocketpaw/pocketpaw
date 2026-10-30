# fabric/read_model.py — project journal-written Fabric objects into the
# per-workspace SQLite FabricStore (the read model).
# Created: 2026-10-01 (CN-6 — canonicalization night run). Fabric had two
# object stores: FabricJournalStore (the blessed WRITE path — people/Person,
# later Paw Partners clients) and the per-workspace FabricStore that every
# READER uses (Fabric API router, agents' Fabric MCP, Ripple sources). Journal
# writes never reached the SQLite file, so agents could not see them.
#
# Decision (acting captain, Q1): the journal is the write path, FabricStore is
# the read model. FabricJournalStore calls into this module after each write
# to upsert the object (full current state, idempotent by object id) into the
# store of every ``workspace:<id>`` in the event scope. No reader moves.
#
# NOT the FST-4 shadow hook: ``statement_store`` / ``flush_shadow`` only record
# statements on updates (mode-gated) and never write ``fabric_objects``.
#
# ``default_journal_store()`` is the process-wide journal store wired to the
# per-workspace read model — the one helper callers (people today, partners
# next) must reuse. Never build a second ``FabricJournalStore(get_journal())``:
# that is a second in-memory projection and a second backfill.
#
# Updated: 2026-10-01 (CN-6 review) — when the workspace already owns a type
# with the object's name, the projected row is stamped with THAT type's id
# (no dangling ``type_id``); the backfill is no longer triggered lazily — EE
# runs ``sync_read_model()`` as a startup background task.
#
# Updated: 2026-10-02 (CN-6 race fix) — ``project_object`` takes an optional
# ``is_current`` check, re-read right before the upsert, so a projection whose
# snapshot went stale during its awaits (e.g. a live archive) never writes it.

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING

from pocketpaw.fabric.models import FabricObject

if TYPE_CHECKING:
    from pocketpaw.fabric.journal_store import FabricJournalStore
    from pocketpaw.fabric.store import FabricStore

logger = logging.getLogger(__name__)

ReadModelResolver = Callable[[str], "FabricStore"]

_WORKSPACE_PREFIX = "workspace:"


def workspace_ids_from_scope(scope: list[str]) -> list[str]:
    """``["workspace:ws1", "org:x"]`` -> ``["ws1"]``. Only the id segment right
    after ``workspace:`` counts; a scope with no workspace entry projects
    nowhere (it has no per-workspace store to land in)."""
    ids: list[str] = []
    for entry in scope:
        if entry.startswith(_WORKSPACE_PREFIX):
            ws = entry[len(_WORKSPACE_PREFIX) :].split(":", 1)[0]
            if ws and ws not in ids:
                ids.append(ws)
    return ids


async def _ensure_type(store: FabricStore, obj: FabricObject, workspace_id: str) -> str:
    """Return the type id the object should carry in ``store``, defining the
    type if the workspace has none.

    Our stable id already present -> keep it. A same-named type the workspace
    (or a legacy global) already owns -> use THAT id, so the row joins the
    workspace's own type instead of dangling. Otherwise define the type under
    the stable id; a concurrent define losing the race on the PK / name-unique
    index re-resolves.
    """
    if not obj.type_id or await store.get_type(obj.type_id) is not None:
        return obj.type_id
    if obj.type_name:
        owned = await store.get_type_by_name(obj.type_name, workspace_id=workspace_id)
        if owned is not None:
            return owned.id
    try:
        await store.define_type(
            name=obj.type_name or obj.type_id,
            properties=[],
            workspace_id=workspace_id,
            type_id=obj.type_id,
        )
    except sqlite3.IntegrityError:
        owned = await store.get_type_by_name(
            obj.type_name or obj.type_id, workspace_id=workspace_id
        )
        if owned is not None:
            return owned.id
    return obj.type_id


async def project_object(
    store: FabricStore,
    obj: FabricObject,
    *,
    workspace_id: str,
    is_current: Callable[[], bool] | None = None,
) -> None:
    """Upsert ``obj`` (its full current state) into one workspace's store.

    ``is_current`` is re-checked after the type awaits, right before the
    upsert: if the snapshot is no longer the object's live state (archived or
    rewritten meanwhile), nothing is written — the newer write projects itself.
    """
    type_id = await _ensure_type(store, obj, workspace_id)
    if type_id != obj.type_id:
        obj = obj.model_copy(update={"type_id": type_id})
    if is_current is not None and not is_current():
        return
    if not await store.upsert_object(obj, workspace_id=workspace_id):
        logger.info(
            "fabric read model: %s not projected into %s — row is owned by another"
            " workspace or already newer",
            obj.id,
            workspace_id,
        )


async def unproject_object(store: FabricStore, object_id: str, *, workspace_id: str) -> None:
    """Drop an archived object from one workspace's store (scoped probe first,
    so a foreign-tenant row with the same id is never deleted)."""
    if await store.get_object(object_id, workspace_id=workspace_id) is not None:
        await store.remove_object(object_id)


def _workspace_store(workspace_id: str) -> FabricStore:
    from pocketpaw.stores import get_fabric_store  # lazy: stores imports fabric.store

    return get_fabric_store(workspace_id=workspace_id)


@lru_cache(maxsize=1)
def default_journal_store() -> FabricJournalStore:
    """Process-wide FabricJournalStore over the org journal, projecting every
    write into ``get_fabric_store(workspace_id=<scope ws>)``. Bootstrapped once
    (sync journal replay). Backfill of earlier journal objects is explicit:
    ``await default_journal_store().sync_read_model()``."""
    from pocketpaw.fabric.journal_store import FabricJournalStore
    from pocketpaw.journal_dep import get_journal

    store = FabricJournalStore(get_journal(), read_model=_workspace_store)
    store.bootstrap()
    return store
