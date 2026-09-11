"""MongoDB implementation of MemoryStoreProtocol backed by the unified schema.

SESSION entries are stored as pocket-context rows in the `messages` collection,
keyed by ``session_key`` (mirrors the protocol's own key). LONG_TERM and DAILY
entries live in ``memory_facts``.

Session metadata (title stays user-facing / UI-owned) but per-turn upkeep —
``lastActivity`` touch and ``messageCount`` increment — is done here because
this adapter is the sole write path for chat turns. It also auto-creates a
``Session`` doc for a new ``session_key`` so the "start chatting → session
appears in the sidebar" UX works without a prior ``POST /sessions``.

Recent change: ``_resolve_or_create_session`` now threads the pocket id out
of ``entry.metadata`` (``pocket_context.id`` or the flat ``pocket_id``
fallback) into ``auto_create_pocket_session`` so OSS-path home/widget chats
get stamped with both the pocket id and the workspace's default agent —
required for them to surface in the PocketPaw DM room.

Tenant scope
------------
Every row is stamped with a ``workspace_id`` so multi-tenant ee deployments
can isolate reads. For SESSION rows the adapter resolves it from the linked
Session.workspace at write time. For LONG_TERM / DAILY rows callers populate
``entry.metadata["workspace_id"]``.

**Changed 2026-09-11.** This module previously said:

    Reads expose ``workspace_id`` as a parameter on the adapter-specific
    helpers (``list_facts_in_workspace``, ``get_session_in_workspace``); the
    protocol-level methods stay unscoped to preserve the
    ``MemoryStoreProtocol`` contract for OSS callers.

That was true, and it was the defect. The protocol-level methods are reachable
from ``recall``, ``clear_session`` and ``delete_session`` — built-in agent tools
on the ``_TENANT_SAFE_TOOLS`` allowlist in ``pocketpaw.agents.pydantic_ai``,
where the grant is annotated "scoped by the caller's own session key". Nothing
in this adapter was scoping them, so one tenant's prompt could read every
workspace's memory facts by regex, and delete another tenant's pocket messages.

Now every protocol-level read and delete resolves the active workspace through
``_scoped`` / ``_row_in_scope`` and refuses rather than answering globally. The
``MemoryStoreProtocol`` signatures are unchanged, so the OSS contract holds; what
changed is that a multi-tenant deployment with no workspace in context gets an
empty result and a loud log line instead of everyone's data. Single-tenant and
local installs are unaffected — ``_tenant_isolation_required()`` is false there,
and the methods behave exactly as before.

``POCKETPAW_MEMORY_ALLOW_GLOBAL_READS=1`` restores the old behaviour for an
operator who needs it, and says so in the log every time it is consulted.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw.memory.protocol import (  # type: ignore[import-untyped]
    DEFAULT_SESSION_HISTORY_LIMIT,
    MemoryEntry,
    MemoryType,
)
from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
from pocketpaw_ee.cloud.models.message import Message
from pocketpaw_ee.cloud.models.session import Session

logger = logging.getLogger(__name__)

# Channels the pocketpaw bus emits as the prefix of `InboundMessage.session_key`
# (``f"{channel.value}:{chat_id}"``). Kept in sync with
# ``pocketpaw.bus.events.Channel`` — when a new adapter is added there, append
# its value here. ``_normalize_session_key`` logs a warning when it sees a
# colon-form prefix it doesn't recognise so the drift is visible.
_KNOWN_BUS_CHANNELS = frozenset({"websocket", "telegram", "discord", "slack", "whatsapp", "cli"})


def _normalize_session_key(key: str) -> str:
    """Translate bus-style session keys to the underscore form used by Session.sessionId.

    The pocketpaw message bus forms session keys as ``"{channel}:{chat_id}"``
    (colon), while ``Session.sessionId`` and the UI use the safe-key form
    ``"{channel}_{chat_id}"`` (underscore). To keep ``messages.session_key``
    joinable with ``sessions.sessionId``, we rewrite the first ``":"`` to
    ``"_"`` on every read/write — but only when the prefix matches a known
    channel so unrelated keys (user-supplied pocket session keys, etc.) are
    left untouched. Unknown colon-prefixed keys log a warning so a missing
    channel is visible rather than silent.
    """
    if ":" not in key:
        return key
    channel, _, rest = key.partition(":")
    if channel in _KNOWN_BUS_CHANNELS:
        return f"{channel}_{rest}"
    logger.warning(
        "session_key %r looks bus-shaped (colon) but channel %r is not in the "
        "known list — left untouched. Update _KNOWN_BUS_CHANNELS if a new bus "
        "adapter was added.",
        key,
        channel,
    )
    return key


_ALLOW_GLOBAL_ENV = "POCKETPAW_MEMORY_ALLOW_GLOBAL_READS"


def _active_workspace_scope() -> str | None:
    """The workspace bound for this execution context, or None.

    Reads the OSS-core ``current_workspace`` ContextVar rather than any ee
    ContextVar, for two reasons: it is the seam OSS core owns for exactly this
    (``pocketpaw.stores``), and ``agent_service.attach_agent_identity`` bridges
    the per-stream workspace onto it (ISO-3), including the second bind inside
    prewarm that exists so in-process tools can read identity at all.
    """
    try:
        from pocketpaw.stores import current_workspace  # type: ignore[import-untyped]

        value = current_workspace.get()
    except Exception:  # noqa: BLE001 — an unreadable ContextVar means "no scope"
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _tenant_isolation_required() -> bool:
    """True when this deployment holds more than one tenant.

    Fails CLOSED. If the signal cannot be read we isolate, because the unsafe
    direction here is answering a query globally — the opposite of the pocket
    router's bypass gate, where the unsafe direction is opening the bypass.
    Both refuse on an unreadable signal; the returned boolean differs because
    the dangerous answer differs.

    ``is_multi_tenant_cloud()`` rather than an env flag, for the reason given in
    PR #2126: a flag defaults to whatever an operator remembers to set, and this
    one would have to be remembered on every deployment that ever gains a second
    tenant.
    """
    if os.environ.get(_ALLOW_GLOBAL_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning(
            "%s is set — memory reads are NOT tenant-scoped. Every workspace's "
            "memory facts and pocket messages are visible to every caller.",
            _ALLOW_GLOBAL_ENV,
        )
        return False
    try:
        from pocketpaw_ee.cloud.shared.db import is_multi_tenant_cloud

        return bool(is_multi_tenant_cloud())
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not determine whether this deployment is multi-tenant; "
            "scoping memory reads to the active workspace (fail-closed)."
        )
        return True


def _scoped(filters: dict, *, field: str = "workspace_id") -> dict | None:
    """Narrow ``filters`` to the active workspace, or None meaning "refuse".

    None is the caller's signal to return an empty result rather than run the
    query: in a multi-tenant deployment a request with no workspace in context
    cannot be attributed, and answering it globally is the defect this closes.

    Rows carrying no ``workspace_id`` (legacy and OSS-path data) fall out of a
    scoped read. That is deliberate and matches ``list_facts_in_workspace``,
    which has always excluded them — an unattributed row cannot be shown to a
    tenant on the assumption that it is theirs.
    """
    if not _tenant_isolation_required():
        return filters
    workspace_id = _active_workspace_scope()
    if not workspace_id:
        return None
    return {**filters, field: workspace_id}


def _refused(operation: str) -> None:
    """Log a refused unscoped access. Loud, because silence is the failure mode."""
    logger.warning(
        "MongoMemoryStore.%s refused: multi-tenant deployment with no workspace "
        "bound in this execution context. Returning an empty result rather than "
        "reading across tenants. Bind identity via attach_agent_identity before "
        "the agent runs, or pass an explicit workspace via the *_in_workspace "
        "helpers.",
        operation,
    )


def _row_in_scope(row_workspace: str | None) -> bool:
    """True when a fetched row may be shown to the current context.

    Used by the id-keyed methods (``get``, ``delete``), where the check has to
    happen after the fetch because the id IS the whole query.
    """
    if not _tenant_isolation_required():
        return True
    workspace_id = _active_workspace_scope()
    if not workspace_id:
        return False
    return row_workspace == workspace_id


def _message_to_entry(msg: Message) -> MemoryEntry:
    """Translate a pocket-context Message to a protocol MemoryEntry."""
    ts = msg.createdAt or datetime.now(UTC)
    metadata: dict = {}
    if msg.workspace_id:
        metadata["workspace_id"] = msg.workspace_id
    return MemoryEntry(
        id=str(msg.id),
        type=MemoryType.SESSION,
        content=msg.content,
        created_at=ts,
        updated_at=ts,
        role=msg.role,
        session_key=msg.session_key,
        metadata=metadata,
    )


def _fact_to_entry(doc: MemoryFactDoc) -> MemoryEntry:
    """Translate a MemoryFactDoc to a protocol MemoryEntry."""
    ts_created = doc.createdAt or datetime.now(UTC)
    ts_updated = doc.updatedAt or ts_created
    metadata = dict(doc.metadata)
    if doc.user_id:
        metadata.setdefault("user_id", doc.user_id)
    if doc.workspace_id:
        metadata.setdefault("workspace_id", doc.workspace_id)
    return MemoryEntry(
        id=str(doc.id),
        type=MemoryType(doc.type),
        content=doc.content,
        created_at=ts_created,
        updated_at=ts_updated,
        tags=list(doc.tags),
        metadata=metadata,
    )


class MongoMemoryStore:
    """Full MemoryStoreProtocol implementation on top of the unified schema.

    - SESSION: reads/writes the `messages` collection (pocket context).
    - LONG_TERM / DAILY: reads/writes the `memory_facts` collection.

    Multi-tenant scoping
    ~~~~~~~~~~~~~~~~~~~~
    Every persisted row carries a ``workspace_id`` (derived from the linked
    ``Session.workspace`` for pocket messages, supplied via
    ``entry.metadata["workspace_id"]`` for facts).

    The protocol-level methods keep their ``MemoryStoreProtocol`` signatures but
    are no longer tenant-agnostic: each resolves the active workspace from the
    ``current_workspace`` ContextVar and, on a multi-tenant deployment with no
    workspace in context, returns an empty result rather than reading across
    tenants. See the module docstring for why that changed.

    The ``*_in_workspace`` helpers remain the right call for any ee caller that
    already holds a request scope — they take the workspace explicitly and do
    not depend on a ContextVar being bound.
    """

    async def save(self, entry: MemoryEntry) -> str:
        if entry.type == MemoryType.SESSION:
            if not entry.session_key:
                raise ValueError("SESSION entry must have session_key set")
            role = entry.role or "user"
            # `sender_type` mirrors the chat-message convention used by the
            # group-chat path: assistant rows land as "agent", everything
            # else (user/system) as "user". Without this both fields would
            # default to "user" and downstream UIs that read `senderType`
            # (instead of `role`) would render every message as the user.
            sender_type = "agent" if role == "assistant" else "user"
            normalized_key = _normalize_session_key(entry.session_key)

            from pocketpaw_ee.cloud.chat import message_service

            # Dedup against a same-turn re-write. The main duplicate source
            # (chat_persistence writing in parallel) is gone — we now own
            # the single write path — but keep this guard so agent-loop
            # retries of identical content don't land twice.
            existing_id = await message_service.find_pocket_dedup_twin_id(
                normalized_key, role, entry.content
            )
            if existing_id is not None:
                return existing_id

            # Attachments ride on the InboundMessage metadata from
            # /chat/stream so we can persist them on the same Message row
            # instead of double-writing. Malformed entries are skipped but
            # don't abort the save — the text content still gets through.
            attachment_dicts: list[dict] = []
            raw_attachments = (entry.metadata or {}).get("attachments") or []
            if isinstance(raw_attachments, list):
                for a in raw_attachments:
                    if isinstance(a, dict):
                        attachment_dicts.append(a)
                    else:
                        logger.warning("skipping malformed attachment on pocket message: %r", a)

            session, workspace_id = await _resolve_or_create_session(normalized_key, entry)
            msg_id = await message_service.persist_pocket_memory_message(
                session_key=normalized_key,
                role=role,
                sender_type=sender_type,
                content=entry.content,
                workspace_id=workspace_id,
                attachments=attachment_dicts or None,
            )

            if session is not None:
                from pocketpaw_ee.cloud.sessions import service as sessions_service

                await sessions_service.touch_doc(session)

            return msg_id

        # LONG_TERM / DAILY → memory_facts
        meta = dict(entry.metadata or {})
        user_id = meta.pop("user_id", None)
        workspace_id = meta.pop("workspace_id", None)
        doc = MemoryFactDoc(
            type=entry.type.value,
            content=entry.content,
            tags=list(entry.tags or []),
            user_id=user_id if isinstance(user_id, str) else None,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
            metadata=meta,
        )
        await doc.insert()
        return str(doc.id)

    async def get(self, entry_id: str) -> MemoryEntry | None:
        try:
            oid = PydanticObjectId(entry_id)
        except (InvalidId, ValueError):
            return None
        msg = await Message.get(oid)
        if msg and msg.context_type == "pocket":
            if not _row_in_scope(msg.workspace_id):
                _refused("get")
                return None
            return _message_to_entry(msg)
        fact = await MemoryFactDoc.get(oid)
        if fact:
            if not _row_in_scope(fact.workspace_id):
                _refused("get")
                return None
            return _fact_to_entry(fact)
        return None

    async def delete(self, entry_id: str) -> bool:
        try:
            oid = PydanticObjectId(entry_id)
        except (InvalidId, ValueError):
            return False
        msg = await Message.get(oid)
        if msg and msg.context_type == "pocket":
            if not _row_in_scope(msg.workspace_id):
                _refused("delete")
                return False
            from pocketpaw_ee.cloud.chat import message_service

            return await message_service.delete_message_doc_by_id(entry_id)
        fact = await MemoryFactDoc.get(oid)
        if fact:
            if not _row_in_scope(fact.workspace_id):
                _refused("delete")
                return False
            await fact.delete()
            return True
        return False

    async def search(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        # Substring-only search (no vectors). Dispatches by type:
        # SESSION → messages; LONG_TERM/DAILY → memory_facts; None → facts
        # across both fact types (mirrors FileMemoryStore's default search).
        if memory_type == MemoryType.SESSION:
            filters: dict = {"context_type": "pocket"}
            if tags:
                raise NotImplementedError("tag search is not supported for SESSION messages in v1")
            if query:
                filters["content"] = {"$regex": re.escape(query), "$options": "i"}
            scoped = _scoped(filters)
            if scoped is None:
                _refused("search")
                return []
            messages = await Message.find(scoped).sort("-createdAt").limit(limit).to_list()
            return [_message_to_entry(m) for m in messages]

        fact_filters: dict = {}
        if memory_type is not None:
            fact_filters["type"] = memory_type.value
        if tags:
            fact_filters["tags"] = {"$in": tags}
        if query:
            fact_filters["content"] = {"$regex": re.escape(query), "$options": "i"}
        scoped_facts = _scoped(fact_filters)
        if scoped_facts is None:
            _refused("search")
            return []
        facts = await MemoryFactDoc.find(scoped_facts).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]

    async def get_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100,
        user_id: str | None = None,
    ) -> list[MemoryEntry]:
        if memory_type == MemoryType.SESSION:
            scoped = _scoped({"context_type": "pocket"})
            if scoped is None:
                _refused("get_by_type")
                return []
            messages = await Message.find(scoped).sort("-createdAt").limit(limit).to_list()
            return [_message_to_entry(m) for m in messages]

        filters: dict = {"type": memory_type.value}
        if user_id is not None:
            filters["user_id"] = user_id
        scoped_facts = _scoped(filters)
        if scoped_facts is None:
            _refused("get_by_type")
            return []
        facts = await MemoryFactDoc.find(scoped_facts).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]

    async def get_session(
        self, session_key: str, limit: int | None = DEFAULT_SESSION_HISTORY_LIMIT
    ) -> list[MemoryEntry]:
        key = _normalize_session_key(session_key)
        scoped = _scoped({"context_type": "pocket", "session_key": key})
        if scoped is None:
            _refused("get_session")
            return []
        query = Message.find(scoped)
        if limit is None:
            messages = await query.sort("createdAt").to_list()
        else:
            # NEWEST `limit`, then reversed back to ascending. Sorting ascending
            # and limiting would return the OLDEST `limit` — the exact defect
            # #2075 fixed in the chat history window, where the agent was
            # rehydrated with the first fifty messages a scope ever had.
            recent = await query.sort("-createdAt").limit(limit).to_list()
            messages = list(reversed(recent))
        return [_message_to_entry(m) for m in messages]

    async def clear_session(self, session_key: str) -> int:
        from pocketpaw_ee.cloud.chat import message_service

        key = _normalize_session_key(session_key)
        scoped = _scoped({"context_type": "pocket", "session_key": key})
        if scoped is None:
            _refused("clear_session")
            return 0
        messages = await Message.find(scoped).to_list()
        count = 0
        for m in messages:
            if await message_service.delete_message_doc_by_id(str(m.id)):
                count += 1
        return count

    # ---- Adapter-specific (not in MemoryStoreProtocol) ----------------

    async def get_session_info(self, session_key: str) -> Session | None:
        """Return the Session metadata row for ``session_key`` if it exists.

        The adapter never auto-creates `sessions` rows — that's the API layer's
        job (`SessionService`). A None return means no user-facing session
        metadata exists, even if messages do.

        ``Session`` names its tenant column ``workspace``, not ``workspace_id``
        like ``Message`` and ``MemoryFactDoc`` — hence the explicit ``field``.
        """
        scoped = _scoped({"sessionId": session_key}, field="workspace")
        if scoped is None:
            _refused("get_session_info")
            return None
        return await Session.find_one(scoped)

    async def _load_session_index_async(self, *, workspace_id: str, owner_id: str) -> dict:
        """Build a session-index dict from one owner's pocket-context Sessions.

        Shape-compatible with ``FileMemoryStore._load_session_index`` so the
        ``GET /sessions/runtime`` endpoint is backend-agnostic. Returns a mapping
        ``{sessionId: {title, channel, last_activity, message_count}}``.

        The scope arguments are REQUIRED (2026-08-01). This index backs a
        "your sessions" listing, so every read of it is per-owner within a
        workspace and a deployment-wide variant has no legitimate caller.
        Mandatory parameters rather than ones defaulting to None mean the
        unscoped form cannot reappear by omission — a caller that forgets the
        scope fails to call at all, rather than silently querying across
        tenants.
        """
        docs = await Session.find(
            {
                "context_type": "pocket",
                "deleted_at": None,
                "workspace": workspace_id,
                "owner": owner_id,
            }
        ).to_list()

        index: dict[str, dict] = {}
        for doc in docs:
            session_id = doc.sessionId
            # Derive channel from the safe_key prefix (websocket_xxx → "websocket").
            channel = session_id.split("_", 1)[0] if "_" in session_id else "unknown"
            # Mongo strips tzinfo on persistence; re-anchor as UTC so the
            # serialized ISO string stays unambiguous for the frontend.
            last_activity = ""
            if doc.lastActivity:
                dt = doc.lastActivity
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                last_activity = dt.isoformat()
            index[session_id] = {
                "title": doc.title or "New Chat",
                "channel": channel,
                "last_activity": last_activity,
                "message_count": doc.messageCount,
            }
        return index

    async def get_session_with_messages(
        self, session_key: str, limit: int | None = None
    ) -> tuple[Session | None, list[MemoryEntry]]:
        """Return session metadata (if any) plus its messages in one call.

        Two queries but a single adapter entry point. When ``limit`` is None
        all messages are returned; otherwise only the most recent ``limit`` in
        ascending order.
        """
        session = await self.get_session_info(session_key)
        key = _normalize_session_key(session_key)
        scoped = _scoped({"context_type": "pocket", "session_key": key})
        if scoped is None:
            _refused("get_session_with_messages")
            return None, []
        query = Message.find(scoped)
        if limit is None:
            messages = await query.sort("createdAt").to_list()
        else:
            recent = await query.sort("-createdAt").limit(limit).to_list()
            messages = list(reversed(recent))
        return session, [_message_to_entry(m) for m in messages]

    # ---- Tenant-scoped reads (ee callers should prefer these) ----------

    async def get_session_in_workspace(
        self, session_key: str, workspace_id: str
    ) -> list[MemoryEntry]:
        """Like ``get_session`` but enforces a workspace boundary.

        Returns an empty list if the session_key exists but belongs to a
        different workspace, so a leaked or guessed key cannot expose a
        tenant's messages.
        """
        key = _normalize_session_key(session_key)
        messages = (
            await Message.find(
                {
                    "context_type": "pocket",
                    "session_key": key,
                    "workspace_id": workspace_id,
                }
            )
            .sort("createdAt")
            .to_list()
        )
        return [_message_to_entry(m) for m in messages]

    async def list_facts_in_workspace(
        self,
        workspace_id: str,
        memory_type: MemoryType | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """List LONG_TERM / DAILY facts scoped to a workspace.

        Rows without a ``workspace_id`` (legacy / OSS data) are excluded so
        cross-tenant leakage is impossible by construction.
        """
        filters: dict = {"workspace_id": workspace_id}
        if memory_type is not None:
            filters["type"] = memory_type.value
        if user_id is not None:
            filters["user_id"] = user_id
        facts = await MemoryFactDoc.find(filters).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]


async def _resolve_or_create_session(
    session_key: str, entry: MemoryEntry
) -> tuple[Session | None, str | None]:
    """Return (session, workspace_id) for a SESSION row at write time.

    Lookup order:
    1. Existing ``Session`` row where ``sessionId == session_key`` — the
       common case (client POSTed ``/sessions`` first or the session was
       auto-created on a previous turn).
    2. Auto-create a pocket ``Session`` (via ``sessions.service``) so the
       "start chatting → session appears in the sidebar" UX works when
       the client skipped the explicit create.

    The workspace_id prefers ``entry.metadata["workspace_id"]`` when the
    caller already knows it (e.g. an HTTP handler with active workspace
    in scope), falling back to ``Session.workspace``.

    Returns ``(None, workspace_id_or_None)`` only when no session could
    be resolved or created. The message row still persists — it's just
    not counted against a session.
    """
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    md = entry.metadata or {}
    md_ws = md.get("workspace_id")
    md_ws = md_ws if isinstance(md_ws, str) and md_ws else None

    # Pocket id lives at metadata["pocket_context"]["id"] for the OSS pocket
    # chat path; fall back to a flat metadata["pocket_id"] when present.
    pocket_ctx = md.get("pocket_context")
    md_pocket = pocket_ctx.get("id") if isinstance(pocket_ctx, dict) else None
    if not md_pocket:
        md_pocket = md.get("pocket_id")
    md_pocket = md_pocket if isinstance(md_pocket, str) and md_pocket else None

    session = await sessions_service.find_by_session_id(session_key)
    if session is not None:
        return session, md_ws or session.workspace

    session = await sessions_service.auto_create_pocket_session(
        session_key, workspace_id=md_ws, pocket_id=md_pocket
    )
    if session is None:
        return None, md_ws

    return session, session.workspace
