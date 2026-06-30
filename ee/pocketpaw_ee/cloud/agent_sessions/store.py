# ee/pocketpaw_ee/cloud/agent_sessions/store.py — the Mongo-backed, tenancy-keyed
# ``SessionStore`` (feat/session-supervisor SS-2).
#
# The real, durable counterpart to the OSS reference
# ``pocketpaw.agents.session_store.InMemorySessionStore``. It satisfies the
# Claude Agent SDK's ``SessionStore`` protocol by DUCK TYPING (the SDK never
# uses ``isinstance``) and persists every transcript line as a
# ``SessionTranscriptDoc`` row in the ``session_transcripts`` collection.
#
# The store is bound to a ``workspace_id`` at construction; EVERY query filters
# on ``workspace == self._workspace_id``, so a store bound to tenant B can never
# read tenant A's rows even though both live in the same collection — the core
# isolation property (SS-7 tests it). It mirrors the in-memory ref impl's
# semantics 1:1 (subpath ``""`` == main transcript, uuid-keyed idempotency on
# ``append``, main-transcript ``delete`` cascades to subkeys).
#
# EE→OSS boundary: this concrete class lives in ee and is constructed by the
# cloud controller (SS-5). It reaches the OSS ``claude_sdk`` backend ONLY as an
# opaque object on ``SessionHandle.session_store`` — ``src/pocketpaw`` never
# imports it.
#
# Created 2026-06-30 (feat/session-supervisor SS-2): new entity. Only this module
# imports ``SessionTranscriptDoc``.
#
# Updated 2026-06-30 (fix/session-supervisor-saas-hardening SH-1a): ``append`` is
# now a single ATOMIC aggregation-pipeline ``update_one(..., upsert=True)`` instead
# of find-modify-save, so two concurrent appends to one session both land (no
# read-modify-write lost update). uuid-dedup against the stored rows stays atomic
# (the pipeline appends only entries whose uuid is not already present); within-
# batch duplicate uuids are collapsed locally first.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pocketpaw_ee.cloud.models.session_transcript import SessionTranscriptDoc

if TYPE_CHECKING:
    from claude_agent_sdk.types import (
        SessionKey,
        SessionListSubkeysKey,
        SessionStoreEntry,
        SessionStoreListEntry,
    )

logger = logging.getLogger(__name__)


def _to_ms(dt: Any) -> int:
    """Epoch-milliseconds for a datetime; 0 when absent.

    Mongo strips tzinfo on persistence, so a naive datetime is treated as UTC.
    """
    if dt is None:
        return 0
    return int(dt.timestamp() * 1000)


class MongoSessionStore:
    """Tenancy-keyed ``SessionStore`` persisted in the ``session_transcripts``
    collection. Construct one per ``workspace_id``."""

    def __init__(self, workspace_id: str):
        if not workspace_id:
            raise ValueError("MongoSessionStore requires a non-empty workspace_id")
        self._workspace_id = workspace_id

    @staticmethod
    def _subpath_of(key: SessionKey) -> str:
        """Read the optional ``subpath`` (``""`` == main transcript)."""
        return key.get("subpath") or ""

    async def _find_row(
        self, project_key: str, session_id: str, subpath: str
    ) -> SessionTranscriptDoc | None:
        return await SessionTranscriptDoc.find_one(
            SessionTranscriptDoc.workspace == self._workspace_id,
            SessionTranscriptDoc.project_key == project_key,
            SessionTranscriptDoc.session_id == session_id,
            SessionTranscriptDoc.subpath == subpath,
        )

    # -- SessionStore protocol ---------------------------------------------

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """Mirror a batch of transcript entries for ``key`` with ONE atomic write.

        A single aggregation-pipeline ``update_one(filter, pipeline, upsert=True)``
        replaces the old find-modify-save: it appends to ``entries`` in-place on
        the server, so two concurrent appends to the same session BOTH land (no
        read-modify-write lost update — the old path let one clobber the other).

        uuid-dedup (the protocol's idempotency contract — the SDK mirror batcher
        re-sends a batch verbatim on reconnect, and double-writing it would corrupt
        the resumed transcript) stays correct AND atomic: the pipeline appends only
        the entries whose ``uuid`` is not already present in the stored array.
        Within-batch duplicate uuids are collapsed first in Python (cheap, local,
        no shared state); entries with NO ``uuid`` are always appended (never
        deduped). ``updatedAt`` (the ``mtime`` source ``list_sessions`` reads) is
        refreshed on every call; ``createdAt`` is stamped only on the upsert-insert.

        The pipeline bypasses Beanie's ``before_event`` timestamp hooks (it is a
        raw collection update), so the timestamps are set explicitly here.
        """
        subpath = self._subpath_of(key)

        # Collapse within-batch duplicate uuids (keep first occurrence); no-uuid
        # entries always survive. Purely local — preserves the old per-call dedup
        # without reading the stored row, so no race is reintroduced.
        deduped: list[dict[str, Any]] = []
        seen_in_batch: set[Any] = set()
        for entry in entries:
            uuid = entry.get("uuid")
            if uuid is not None:
                if uuid in seen_in_batch:
                    continue
                seen_in_batch.add(uuid)
            deduped.append(dict(entry))

        now = datetime.now(UTC)
        existing = {"$ifNull": ["$entries", []]}
        # uuids already stored (stored no-uuid entries are excluded — they never
        # participate in dedup and their missing ``uuid`` would corrupt ``$in``).
        existing_uuids = {
            "$map": {
                "input": {
                    "$filter": {
                        "input": existing,
                        "as": "x",
                        "cond": {"$ne": [{"$ifNull": ["$$x.uuid", None]}, None]},
                    }
                },
                "as": "x",
                "in": "$$x.uuid",
            }
        }
        # ``$literal`` keeps the entry blobs as plain data (a stray ``$``-prefixed
        # key would otherwise be evaluated as an aggregation expression).
        new_to_keep = {
            "$filter": {
                "input": {"$literal": deduped},
                "as": "e",
                "cond": {
                    "$or": [
                        # No uuid → always append (not an idempotency key).
                        {"$eq": [{"$ifNull": ["$$e.uuid", None]}, None]},
                        # uuid not already stored → append (dedup re-sends).
                        {"$not": {"$in": ["$$e.uuid", existing_uuids]}},
                    ]
                },
            }
        }
        pipeline = [
            {
                "$set": {
                    "entries": {"$concatArrays": [existing, new_to_keep]},
                    "updatedAt": now,
                    "createdAt": {"$ifNull": ["$createdAt", now]},
                }
            }
        ]
        await SessionTranscriptDoc.get_pymongo_collection().update_one(
            {
                "workspace": self._workspace_id,
                "project_key": key["project_key"],
                "session_id": key["session_id"],
                "subpath": subpath,
            },
            pipeline,
            upsert=True,
        )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """Return the full transcript for ``key``, or ``None`` if never written.

        A store bound to a different workspace returns ``None`` — the
        ``workspace`` filter excludes the other tenant's row.
        """
        doc = await self._find_row(key["project_key"], key["session_id"], self._subpath_of(key))
        if doc is None:
            return None
        return list(doc.entries)  # type: ignore[return-value]

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        """List MAIN-transcript sessions for ``project_key`` in this workspace."""
        docs = await SessionTranscriptDoc.find(
            SessionTranscriptDoc.workspace == self._workspace_id,
            SessionTranscriptDoc.project_key == project_key,
            SessionTranscriptDoc.subpath == "",
        ).to_list()
        return [
            {"session_id": d.session_id, "mtime": _to_ms(getattr(d, "updatedAt", None))}
            for d in docs
        ]

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        """List the subpaths (e.g. subagent transcripts) under a session."""
        docs = await SessionTranscriptDoc.find(
            SessionTranscriptDoc.workspace == self._workspace_id,
            SessionTranscriptDoc.project_key == key["project_key"],
            SessionTranscriptDoc.session_id == key["session_id"],
        ).to_list()
        return [d.subpath for d in docs if d.subpath]

    async def delete(self, key: SessionKey) -> None:
        """Delete a session entry.

        Deleting a main transcript (no ``subpath``) cascades to every subkey
        under that session; a keyed ``subpath`` deletes only that one entry.
        """
        subpath = self._subpath_of(key)
        if subpath:
            doc = await self._find_row(key["project_key"], key["session_id"], subpath)
            if doc is not None:
                await doc.delete()
            return
        # Main transcript: cascade-delete the main row + all subkeys.
        docs = await SessionTranscriptDoc.find(
            SessionTranscriptDoc.workspace == self._workspace_id,
            SessionTranscriptDoc.project_key == key["project_key"],
            SessionTranscriptDoc.session_id == key["session_id"],
        ).to_list()
        for doc in docs:
            await doc.delete()
