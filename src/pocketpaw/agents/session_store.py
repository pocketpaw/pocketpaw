"""Tenancy-keyed in-memory SessionStore — OSS reference impl (SS-2).

Created: 2026-06-30 (feat/session-supervisor SS-2). Implements the Claude Agent
SDK's ``SessionStore`` protocol (``claude_agent_sdk.types.SessionStore``) so a
backend can resume an agent conversation from OUR durable store instead of the
subprocess's local disk. The SDK calls ``load`` once (in the parent, before
spawn) and materializes the returned transcript entries into a temp dir the
fresh subprocess resumes from; it calls ``append`` after each local write to
mirror new transcript lines back.

This module is the OSS, dev-grade reference: durability lives in an in-memory
backing dict that callers can SHARE across instances (the analog of a Mongo
collection two processes both read). The real durable impl is the Mongo-backed
``MongoSessionStore`` in the ee layer (``pocketpaw_ee.cloud.agent_sessions``);
it mirrors these exact semantics. The store flows to ``claude_sdk`` OPAQUELY via
``SessionHandle.session_store`` so OSS never imports the ee class — the
EE→OSS boundary stays clean.

Tenancy keying
--------------
The store is bound to a ``workspace_id`` at construction. Every storage key is
the 4-tuple ``(workspace_id, project_key, session_id, subpath)`` (``subpath``
defaults to ``""`` for a main transcript). A store bound to workspace B builds
keys with ``workspace_id == "B"``, so it can NEVER observe a row written by a
store bound to workspace A even over the same backing dict — that is the core
isolation property (SS-7 tests it hard). Reads return ``None`` / ``[]`` for a
foreign tenant's session exactly as for one that was never written.

The store satisfies the protocol by DUCK TYPING — the SDK never uses
``isinstance`` (see the protocol docstring), so this class need not subclass
``SessionStore``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk.types import (
        SessionKey,
        SessionListSubkeysKey,
        SessionStoreEntry,
        SessionStoreListEntry,
    )

# A backing record: the ordered transcript entries plus the set of uuids already
# seen (idempotency), plus the last-write time in epoch ms (the mtime source).
_StorageKey = tuple[str, str, str, str]  # (workspace_id, project_key, session_id, subpath)


def _now_ms() -> int:
    """Current wall-clock time in Unix epoch milliseconds (the ``mtime`` clock)."""
    return int(time.time() * 1000)


class InMemorySessionStore:
    """Conformant, tenancy-keyed ``SessionStore`` backed by an in-memory dict.

    Construct one per ``workspace_id``. Pass a shared ``backing`` dict to model
    durability across a "restart" (a fresh instance over the same backing dict
    still sees prior writes — durability lives in the store, not the process).
    Omit it for a private, throwaway store.
    """

    def __init__(
        self, workspace_id: str, *, backing: dict[_StorageKey, dict[str, Any]] | None = None
    ):
        if not workspace_id:
            raise ValueError("InMemorySessionStore requires a non-empty workspace_id")
        self._workspace_id = workspace_id
        # Shared-able backing: maps the 4-tuple key -> {"entries", "uuids", "mtime"}.
        self._backing: dict[_StorageKey, dict[str, Any]] = backing if backing is not None else {}

    # -- key helpers --------------------------------------------------------

    def _key(self, project_key: str, session_id: str, subpath: str = "") -> _StorageKey:
        """Namespace a (project_key, session_id, subpath) by the bound workspace."""
        return (self._workspace_id, project_key, session_id, subpath)

    @staticmethod
    def _subpath_of(key: SessionKey) -> str:
        """Read the optional ``subpath`` (``""`` == main transcript)."""
        return key.get("subpath") or ""

    # -- SessionStore protocol ---------------------------------------------

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """Mirror a batch of transcript entries for ``key``.

        Entries carrying a stable ``uuid`` are treated as idempotency keys
        (duplicates ignored); entries without one are always appended. Matches
        the protocol's upsert/ignore-duplicate contract.
        """
        sk = self._key(key["project_key"], key["session_id"], self._subpath_of(key))
        record = self._backing.get(sk)
        if record is None:
            record = {"entries": [], "uuids": set(), "mtime": 0}
            self._backing[sk] = record
        seen: set[str] = record["uuids"]
        for entry in entries:
            uuid = entry.get("uuid")
            if uuid is not None:
                if uuid in seen:
                    continue
                seen.add(uuid)
            record["entries"].append(entry)
        record["mtime"] = _now_ms()

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """Return the full transcript for ``key``, or ``None`` if never written.

        A store bound to a different workspace returns ``None`` — the namespaced
        key simply doesn't exist for that tenant.
        """
        sk = self._key(key["project_key"], key["session_id"], self._subpath_of(key))
        record = self._backing.get(sk)
        if record is None:
            return None
        # Return a shallow copy so a caller mutating the list can't corrupt the
        # backing store; entries themselves are opaque pass-through blobs.
        return list(record["entries"])

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        """List MAIN-transcript sessions for ``project_key`` in this workspace.

        Only main transcripts (no ``subpath``) are returned; subagent transcripts
        are discovered via :meth:`list_subkeys`. Order is unspecified — the SDK
        sorts by ``mtime`` descending.
        """
        out: list[SessionStoreListEntry] = []
        for (ws, pk, sid, sub), record in self._backing.items():
            if ws != self._workspace_id or pk != project_key or sub != "":
                continue
            out.append({"session_id": sid, "mtime": record["mtime"]})
        return out

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        """List the subpaths (e.g. subagent transcripts) under a session."""
        target_pk = key["project_key"]
        target_sid = key["session_id"]
        out: list[str] = []
        for (ws, pk, sid, sub), _record in self._backing.items():
            if ws != self._workspace_id or pk != target_pk or sid != target_sid:
                continue
            if sub:
                out.append(sub)
        return out

    async def delete(self, key: SessionKey) -> None:
        """Delete a session entry.

        Deleting a main transcript (no ``subpath``) cascades to every subkey
        under that session; a keyed ``subpath`` deletes only that one entry.
        """
        pk = key["project_key"]
        sid = key["session_id"]
        sub = self._subpath_of(key)
        if sub:
            self._backing.pop(self._key(pk, sid, sub), None)
            return
        # Main transcript: cascade-delete the main row + all subkeys.
        doomed = [
            stored_key
            for stored_key in self._backing
            if stored_key[0] == self._workspace_id and stored_key[1] == pk and stored_key[2] == sid
        ]
        for stored_key in doomed:
            self._backing.pop(stored_key, None)
