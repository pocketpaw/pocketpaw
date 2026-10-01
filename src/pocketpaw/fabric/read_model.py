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
# next) should use instead of building their own.

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


async def _ensure_type(store: FabricStore, obj: FabricObject, workspace_id: str) -> None:
    """Make sure the object's type is listable in ``store`` without colliding
    with a workspace-authored type of the same name.

    Our stable id already present -> done. A same-named type the workspace (or a
    legacy global) already owns -> leave it alone; our objects still carry the
    denormalized ``type_name`` so ``query(type_name=...)`` finds them. Otherwise
    define it under the stable id; a concurrent define loses the race on the
    PK / name-unique index, which is fine.
    """
    if not obj.type_id or await store.get_type(obj.type_id) is not None:
        return
    if obj.type_name and await store.get_type_by_name(obj.type_name, workspace_id=workspace_id):
        return
    try:
        await store.define_type(
            name=obj.type_name or obj.type_id,
            properties=[],
            workspace_id=workspace_id,
            type_id=obj.type_id,
        )
    except sqlite3.IntegrityError:
        pass


async def project_object(store: FabricStore, obj: FabricObject, *, workspace_id: str) -> None:
    """Upsert ``obj`` (its full current state) into one workspace's store."""
    await _ensure_type(store, obj, workspace_id)
    if not await store.upsert_object(obj, workspace_id=workspace_id):
        logger.warning(
            "fabric read model: %s already owned by another workspace — not projected into %s",
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
    write into ``get_fabric_store(workspace_id=<scope ws>)``. Bootstrapped once;
    the first write also backfills objects journaled before CN-6."""
    from pocketpaw.fabric.journal_store import FabricJournalStore
    from pocketpaw.journal_dep import get_journal

    store = FabricJournalStore(get_journal(), read_model=_workspace_store)
    store.bootstrap()
    return store
