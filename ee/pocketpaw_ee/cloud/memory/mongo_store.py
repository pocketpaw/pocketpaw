"""MongoDB implementation of MemoryStoreProtocol backed by the unified schema.

SESSION entries are stored as pocket-context rows in the `messages` collection,
keyed by ``session_key`` (mirrors the protocol's own key). LONG_TERM and DAILY
entries live in ``memory_facts``.

Session metadata (title stays user-facing / UI-owned) but per-turn upkeep —
``lastActivity`` touch and ``messageCount`` increment — is done here because
this adapter is the sole write path for chat turns. It also auto-creates a
``Session`` doc for a new ``session_key`` so the "start chatting → session
appears in the sidebar" UX works without a prior ``POST /sessions``.

Tenant scope
------------
Every row is stamped with a ``workspace_id`` so multi-tenant ee deployments
can isolate reads. For SESSION rows the adapter resolves it from the linked
Session.workspace at write time. For LONG_TERM / DAILY rows callers populate
``entry.metadata["workspace_id"]``. Reads expose ``workspace_id`` as a
parameter on the adapter-specific helpers (``list_facts_in_workspace``,
``get_session_in_workspace``); the protocol-level methods stay unscoped to
preserve the ``MemoryStoreProtocol`` contract for OSS callers.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw.memory.protocol import MemoryEntry, MemoryType  # type: ignore[import-untyped]
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
    ``entry.metadata["workspace_id"]`` for facts). The protocol-level read
    methods stay tenant-agnostic to keep the ``MemoryStoreProtocol`` contract
    unchanged for OSS callers; ee callers that need strict isolation should
    use the adapter-specific ``*_in_workspace`` helpers, which add an explicit
    ``workspace_id`` filter.
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
            return _message_to_entry(msg)
        fact = await MemoryFactDoc.get(oid)
        if fact:
            return _fact_to_entry(fact)
        return None

    async def delete(self, entry_id: str) -> bool:
        try:
            oid = PydanticObjectId(entry_id)
        except (InvalidId, ValueError):
            return False
        msg = await Message.get(oid)
        if msg and msg.context_type == "pocket":
            from pocketpaw_ee.cloud.chat import message_service

            return await message_service.delete_message_doc_by_id(entry_id)
        fact = await MemoryFactDoc.get(oid)
        if fact:
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
            messages = await Message.find(filters).sort("-createdAt").limit(limit).to_list()
            return [_message_to_entry(m) for m in messages]

        fact_filters: dict = {}
        if memory_type is not None:
            fact_filters["type"] = memory_type.value
        if tags:
            fact_filters["tags"] = {"$in": tags}
        if query:
            fact_filters["content"] = {"$regex": re.escape(query), "$options": "i"}
        facts = await MemoryFactDoc.find(fact_filters).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]

    async def get_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100,
        user_id: str | None = None,
    ) -> list[MemoryEntry]:
        if memory_type == MemoryType.SESSION:
            messages = (
                await Message.find({"context_type": "pocket"})
                .sort("-createdAt")
                .limit(limit)
                .to_list()
            )
            return [_message_to_entry(m) for m in messages]

        filters: dict = {"type": memory_type.value}
        if user_id is not None:
            filters["user_id"] = user_id
        facts = await MemoryFactDoc.find(filters).sort("-createdAt").limit(limit).to_list()
        return [_fact_to_entry(f) for f in facts]

    async def get_session(self, session_key: str) -> list[MemoryEntry]:
        key = _normalize_session_key(session_key)
        messages = (
            await Message.find({"context_type": "pocket", "session_key": key})
            .sort("createdAt")
            .to_list()
        )
        return [_message_to_entry(m) for m in messages]

    async def clear_session(self, session_key: str) -> int:
        from pocketpaw_ee.cloud.chat import message_service

        key = _normalize_session_key(session_key)
        messages = await Message.find({"context_type": "pocket", "session_key": key}).to_list()
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
        """
        return await Session.find_one(Session.sessionId == session_key)

    async def _load_session_index_async(self) -> dict:
        """Build a session-index dict from pocket-context Session docs.

        Shape-compatible with ``FileMemoryStore._load_session_index`` so the
        ``GET /sessions/runtime`` endpoint is backend-agnostic. Returns a mapping
        ``{sessionId: {title, channel, last_activity, message_count}}`` for all
        non-deleted pocket sessions.
        """
        docs = await Session.find({"context_type": "pocket", "deleted_at": None}).to_list()

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
        query = Message.find({"context_type": "pocket", "session_key": key})
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

    md_ws = (entry.metadata or {}).get("workspace_id")
    md_ws = md_ws if isinstance(md_ws, str) and md_ws else None

    session = await sessions_service.find_by_session_id(session_key)
    if session is not None:
        return session, md_ws or session.workspace

    session = await sessions_service.auto_create_pocket_session(session_key, workspace_id=md_ws)
    if session is None:
        return None, md_ws

    return session, session.workspace
