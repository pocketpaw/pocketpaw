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

from __future__ import annotations

import logging
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
        """Mirror a batch of transcript entries for ``key``.

        Find-or-create the row, then extend its ``entries`` array, deduping any
        entry that carries a ``uuid`` already present (the protocol's
        idempotency contract). Read-modify-write — see the concurrency note in
        the module/PR; v1 keeps it simple and correct for the de-risk slice.
        """
        subpath = self._subpath_of(key)
        doc = await self._find_row(key["project_key"], key["session_id"], subpath)
        if doc is None:
            doc = SessionTranscriptDoc(
                workspace=self._workspace_id,
                project_key=key["project_key"],
                session_id=key["session_id"],
                subpath=subpath,
                entries=[],
            )
        seen = {e.get("uuid") for e in doc.entries if isinstance(e, dict) and e.get("uuid")}
        for entry in entries:
            uuid = entry.get("uuid")
            if uuid is not None:
                if uuid in seen:
                    continue
                seen.add(uuid)
            doc.entries.append(dict(entry))
        await doc.save()

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """Return the full transcript for ``key``, or ``None`` if never written.

        A store bound to a different workspace returns ``None`` — the
        ``workspace`` filter excludes the other tenant's row.
        """
        doc = await self._find_row(
            key["project_key"], key["session_id"], self._subpath_of(key)
        )
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
