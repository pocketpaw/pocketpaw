"""Sessions service — CRUD + history + activity tracking.

Sole owner of writes to the ``Session`` Beanie document. Module-level
``async def`` API, no class wrapper, no Protocol-based repository.

Public API:
- ``create(ctx, workspace_id, body)`` — create or upsert a session
- ``list_for_owner(ctx, workspace_id)``
- ``list_by_agent(ctx, workspace_id, agent_id)``
- ``list_for_pocket(ctx, pocket_id)``
- ``get(ctx, session_id)``
- ``update(ctx, session_id, body)``
- ``delete(ctx, session_id)`` — soft delete
- ``link_pocket(workspace_id, session_id_str, pocket_id)`` — used by
  ``pockets/service.py`` when a pocket is created with a session_id
- ``get_history(session_id, user_id, limit)`` — kept on Beanie because it
  spans three context types and reads the unified Message collection
- ``touch(session_id)`` — kept on Beanie; called by chat persistence
  bridges on the hot path
- ``legacy_ctx(user_id, workspace_id)`` — helper for routers that haven't
  yet migrated to ``Depends(request_context)``
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import (
    SessionCreated,
    SessionDeleted,
    SessionUpdated,
)
from ee.cloud.models.session import Session as _SessionDoc
from ee.cloud.sessions.dto import (
    CreateSessionRequest,
    UpdateSessionRequest,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

if TYPE_CHECKING:
    from ee.cloud.sessions.domain import Session as DomainSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _to_domain(doc: _SessionDoc) -> DomainSession:
    from ee.cloud.sessions.domain import Session

    return Session(
        id=str(doc.id),
        sessionId=doc.sessionId,
        context_type=doc.context_type or "session",
        workspace=doc.workspace,
        owner=doc.owner,
        title=doc.title,
        pocket=doc.pocket,
        group=doc.group,
        agent=doc.agent,
        message_count=doc.messageCount,
        last_activity=doc.lastActivity,
        created_at=getattr(doc, "createdAt", datetime.now(UTC)),  # type: ignore[arg-type]
        deleted_at=doc.deleted_at,
        surface=getattr(doc, "surface", None),
    )


def legacy_ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    """Build a RequestContext for routers that haven't migrated to
    ``Depends(request_context)`` yet. Synthesizes request_id and scope."""
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="legacy",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Public API — CRUD
# ---------------------------------------------------------------------------


async def create(
    ctx: RequestContext,
    workspace_id: str,
    body: CreateSessionRequest,
) -> DomainSession:
    """Create a session, or update-and-return the existing one when
    ``body.session_id`` matches an existing row."""
    sid = body.session_id or f"websocket_{uuid.uuid4().hex[:12]}"

    if body.session_id:
        existing_doc = await _SessionDoc.find_one(_SessionDoc.sessionId == body.session_id)
        if existing_doc is not None:
            patched: dict = {}
            if body.pocket_id and body.pocket_id != existing_doc.pocket:
                existing_doc.pocket = body.pocket_id
                # Linking a pocket to a session-typed session promotes its
                # context_type so the model validator (which forbids
                # session-typed rows from carrying a pocket) is satisfied.
                if existing_doc.context_type == "session":
                    existing_doc.context_type = "pocket"
                patched["pocket_id"] = body.pocket_id
            if body.title and body.title != "New Chat" and body.title != existing_doc.title:
                existing_doc.title = body.title
                patched["title"] = body.title
            # Stamp ``surface`` only when missing — it tags the *origin* of
            # the session so a re-link from a different surface shouldn't
            # rewrite history.
            if body.surface and getattr(existing_doc, "surface", None) is None:
                existing_doc.surface = body.surface
                patched["surface"] = body.surface
            if patched:
                await existing_doc.save()
                await emit(
                    SessionUpdated(
                        data={
                            "session_id": str(existing_doc.id),
                            "user_id": existing_doc.owner,
                            **patched,
                        }
                    )
                )
            return _to_domain(existing_doc)

    if body.group_id:
        ctype = "group"
    elif body.pocket_id:
        ctype = "pocket"
    else:
        ctype = "session"

    doc = _SessionDoc(
        sessionId=sid,
        context_type=ctype,
        workspace=workspace_id,
        owner=ctx.user_id,
        title=body.title,
        pocket=body.pocket_id,
        group=body.group_id,
        agent=body.agent_id,
        surface=body.surface,
    )
    await doc.insert()
    session = _to_domain(doc)

    await event_bus.emit(
        "session.created",
        {
            "session_id": session.id,
            "session_uuid": session.sessionId,
            "workspace_id": workspace_id,
            "owner_id": ctx.user_id,
            "pocket_id": body.pocket_id,
        },
    )

    created_data: dict = {
        "session_id": session.id,
        "user_id": ctx.user_id,
        "agent_id": body.agent_id,
        "workspace_id": workspace_id,
    }
    if body.pocket_id:
        created_data["pocket_id"] = body.pocket_id
    await emit(SessionCreated(data=created_data))

    return session


async def list_for_owner(
    ctx: RequestContext,
    workspace_id: str,
    *,
    surface: str | None = None,
) -> list[DomainSession]:
    """List the caller's sessions in ``workspace_id``.

    ``surface`` (when provided) restricts the listing to rows stamped with
    that originating surface (``"chat"`` / ``"files"`` / ``"pocket_creation"``).
    Passing ``None`` (the default) preserves legacy behavior — every row
    returns, including pre-fix rows that have ``surface=None``.
    """
    filters: list[Any] = [
        _SessionDoc.workspace == workspace_id,
        _SessionDoc.owner == ctx.user_id,
        _SessionDoc.deleted_at == None,  # noqa: E711
    ]
    if surface is not None:
        filters.append(_SessionDoc.surface == surface)
    docs = (
        await _SessionDoc.find(*filters)
        .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
        .to_list()
    )
    return [_to_domain(d) for d in docs]


async def list_by_agent(
    ctx: RequestContext, workspace_id: str, agent_id: str
) -> list[DomainSession]:
    docs = (
        await _SessionDoc.find(
            _SessionDoc.workspace == workspace_id,
            _SessionDoc.owner == ctx.user_id,
            _SessionDoc.agent == agent_id,
            _SessionDoc.deleted_at == None,  # noqa: E711
        )
        .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
        .to_list()
    )
    return [_to_domain(d) for d in docs]


async def list_for_pocket(ctx: RequestContext, pocket_id: str) -> list[DomainSession]:
    docs = (
        await _SessionDoc.find(
            _SessionDoc.pocket == pocket_id,
            _SessionDoc.owner == ctx.user_id,
            _SessionDoc.deleted_at == None,  # noqa: E711
        )
        .sort(-_SessionDoc.lastActivity)  # type: ignore[arg-type, operator]
        .to_list()
    )
    return [_to_domain(d) for d in docs]


async def _fetch_owned(session_id: str, user_id: str) -> _SessionDoc:
    """Internal: fetch by ObjectId or sessionId; check owner; raise
    NotFound / Forbidden as needed. Used by both ``get`` and the
    history/touch helpers that need the raw doc."""
    doc: _SessionDoc | None = None
    try:
        doc = await _SessionDoc.get(PydanticObjectId(session_id))
    except Exception:
        doc = None
    if doc is None:
        doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
    if doc is None or doc.deleted_at:
        raise NotFound("session", session_id)
    if doc.owner != user_id:
        raise Forbidden("session.not_owner", "Not the session owner")
    return doc


async def get(ctx: RequestContext, session_id: str) -> DomainSession:
    doc = await _fetch_owned(session_id, ctx.user_id)
    return _to_domain(doc)


async def update(
    ctx: RequestContext,
    session_id: str,
    body: UpdateSessionRequest,
) -> DomainSession:
    doc = await _fetch_owned(session_id, ctx.user_id)
    patched = body.model_dump(exclude_unset=True)
    if not patched:
        return _to_domain(doc)
    if body.title is not None:
        doc.title = body.title
    if body.pocket_id is not None:
        doc.pocket = body.pocket_id
        if doc.context_type == "session":
            doc.context_type = "pocket"
    await doc.save()
    await emit(
        SessionUpdated(
            data={
                "session_id": str(doc.id),
                "user_id": ctx.user_id,
                **patched,
            }
        )
    )
    return _to_domain(doc)


async def delete(ctx: RequestContext, session_id: str) -> None:
    doc = await _fetch_owned(session_id, ctx.user_id)
    doc.deleted_at = datetime.now(UTC)
    await doc.save()
    await emit(
        SessionDeleted(
            data={
                "session_id": str(doc.id),
                "user_id": ctx.user_id,
            }
        )
    )


async def link_pocket(workspace_id: str, session_id_str: str, pocket_id: str) -> None:
    """Link a session (looked up by its sessionId string) to a pocket.

    Used by ``pockets/service.create_pocket`` when called with
    ``body.session_id``. No-op if the session doesn't exist or is in a
    different workspace.
    """
    doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id_str)
    if doc is None or doc.workspace != workspace_id:
        return
    doc.pocket = pocket_id
    # Promote the session's context_type so the model validator (which
    # forbids session-typed rows from carrying a pocket) is satisfied.
    if doc.context_type == "session":
        doc.context_type = "pocket"
    await doc.save()


# ---------------------------------------------------------------------------
# History + activity — kept on Beanie (cross-cutting, hot path)
# ---------------------------------------------------------------------------


async def get_history(session_id: str, user_id: str, limit: int = 100) -> dict:
    """Return session chat history from the unified Mongo messages store.

    Spans three context types (session/group/pocket) with shape-specific
    queries. Stays on Beanie because a full extraction would need a
    separate HistoryReader port.
    """
    from ee.cloud.models.message import Message

    session = await _fetch_owned(session_id, user_id)

    if session.context_type == "session":
        # Prefix-match on session_key so reads stay aligned with whatever
        # ``target_agent_id`` the SSE writer used at write time. The writer
        # builds ``cloud:session:{session.id}:{target_agent_id}`` per stream;
        # ``Session.agent`` is backfilled best-effort and may lag (or stay
        # None when the backfill save fails). Matching the literal stored
        # ``session.agent`` would miss those rows entirely — symptom: user
        # sees their optimistic message with no agent reply.
        prefix = f"cloud:session:{session.id}:"
        messages = (
            await Message.find(
                {
                    "context_type": "session",
                    "session_key": {"$regex": f"^{re.escape(prefix)}"},
                }
            )
            .sort("createdAt")
            .limit(limit)
            .to_list()
        )
        return {
            "messages": [
                {
                    "_id": str(m.id),
                    "role": m.role or "user",
                    "content": m.content,
                    "sender": m.sender,
                    "senderType": m.sender_type,
                    "createdAt": iso_utc(m.createdAt),
                    "attachments": [a.model_dump() for a in (m.attachments or [])],
                }
                for m in messages
            ]
        }

    if session.context_type == "group" and session.group:
        messages = (
            await Message.find(
                {
                    "context_type": "group",
                    "group": session.group,
                    "deleted": False,
                }
            )
            .sort("createdAt")
            .limit(limit)
            .to_list()
        )
        return {
            "messages": [
                {
                    "_id": str(m.id),
                    "role": "assistant" if m.sender_type == "agent" else "user",
                    "content": m.content,
                    "sender": m.sender,
                    "senderType": m.sender_type,
                    "createdAt": iso_utc(m.createdAt),
                    "attachments": [a.model_dump() for a in (m.attachments or [])],
                }
                for m in messages
            ]
        }

    # Pocket context — three writer paths land here with different
    # session_key shapes; preserved verbatim from the legacy implementation.
    pocket_candidate_keys = [session.sessionId]
    if session.pocket and session.agent:
        pocket_candidate_keys.append(f"cloud:pocket:{session.pocket}:{session.agent}")
    or_clauses: list[dict[str, Any]] = [
        {"context_type": "pocket", "session_key": {"$in": pocket_candidate_keys}}
    ]
    if session.agent:
        or_clauses.append(
            {
                "context_type": "session",
                "session_key": f"cloud:session:{session.id}:{session.agent}",
            }
        )

    messages = await Message.find({"$or": or_clauses}).sort("createdAt").limit(limit).to_list()
    return {
        "messages": [
            {
                "_id": str(m.id),
                "role": m.role or "user",
                "content": m.content,
                "sender": m.sender,
                "senderType": m.sender_type,
                "createdAt": iso_utc(m.createdAt),
                "attachments": [a.model_dump() for a in (m.attachments or [])],
            }
            for m in messages
        ]
    }


async def ensure_for_agent_scope(
    *,
    kind: str,
    scope_id: str,
    workspace_id: str,
    user_id: str,
    target_agent_id: str | None,
) -> str | None:
    """Find-or-create the ``Session`` row that the sidebar uses to surface a
    ``(scope, agent)`` agent-chat pair. Returns its ``sessionId`` or ``None``.

    Only applies to pocket and agent-DM scopes; plain group chats don't
    show Session rows in the sidebar (the group itself is the entry).
    For ``session`` scope we look up by ``_id`` and backfill ``Session.agent``
    when the session was created without one (e.g. ``createPocketSession``).
    """
    if kind == "pocket":
        existing = (
            await _SessionDoc.find(
                _SessionDoc.pocket == scope_id,
                _SessionDoc.agent == target_agent_id,
                _SessionDoc.owner == user_id,
                _SessionDoc.deleted_at == None,  # noqa: E711
            )
            .sort(-_SessionDoc.lastActivity)
            .limit(1)
            .to_list()
        )
        if existing:
            return existing[0].sessionId
        doc = _SessionDoc(
            sessionId=f"websocket_{uuid.uuid4().hex[:12]}",
            context_type="pocket",
            workspace=workspace_id,
            owner=user_id,
            title="New Chat",
            pocket=scope_id,
            agent=target_agent_id,
        )
        await doc.insert()
        return doc.sessionId

    if kind == "dm":
        # Agent-DM: one Session per (agent, user). Plain human DMs have no
        # agent and surface as rooms, not sessions — skip those.
        if not target_agent_id:
            return None
        existing = (
            await _SessionDoc.find(
                _SessionDoc.agent == target_agent_id,
                _SessionDoc.owner == user_id,
                _SessionDoc.deleted_at == None,  # noqa: E711
            )
            .sort(-_SessionDoc.lastActivity)
            .limit(1)
            .to_list()
        )
        if existing:
            return existing[0].sessionId
        doc = _SessionDoc(
            sessionId=f"websocket_{uuid.uuid4().hex[:12]}",
            context_type="group",
            workspace=workspace_id,
            owner=user_id,
            title="New Chat",
            group=scope_id,
            agent=target_agent_id,
        )
        await doc.insert()
        return doc.sessionId

    if kind == "session":
        try:
            doc = await _SessionDoc.get(PydanticObjectId(scope_id))
        except Exception:
            return None
        if doc is None:
            return None
        # Backfill Session.agent when the session was created without one.
        # The read-side history query no longer keys on ``Session.agent``
        # (it prefix-matches ``cloud:session:{sid}:`` so any writer-supplied
        # agent id matches), but persisting the field still keeps the
        # sidebar's per-agent grouping correct. Log at warning so a recurring
        # save failure shows up in dashboard logs — historically this failure
        # mode caused the "user message with no agent reply" symptom and went
        # undetected for too long.
        if not getattr(doc, "agent", None) and target_agent_id:
            doc.agent = target_agent_id
            try:
                await doc.save()
            except Exception:
                logger.warning(
                    "Session.agent backfill failed for %s — read path uses "
                    "prefix-match so history still resolves, but downstream "
                    "consumers grouping by agent may misroute",
                    doc.sessionId,
                    exc_info=True,
                )
        return doc.sessionId

    return None


async def auto_create_pocket_session(
    session_key: str, *, workspace_id: str | None = None
) -> _SessionDoc | None:
    """Create a pocket ``Session`` doc for ``session_key`` when none exists.

    Picks the first user in the target workspace (or any workspace user
    if ``workspace_id`` is not provided) so the session has an owner.
    Returns ``None`` if no suitable user exists. Used by the cloud
    memory store so its adapter doesn't have to import ``Session`` /
    ``User`` directly.
    """
    from ee.cloud.models.user import User as _UserDoc

    query: dict
    if workspace_id:
        query = {"workspaces.workspace": workspace_id}
    else:
        query = {"workspaces": {"$ne": []}}
    users = await _UserDoc.find(query).limit(1).to_list()
    if not users:
        logger.warning("auto_create_pocket_session: no user with a workspace")
        return None

    user = users[0]
    if workspace_id is None:
        workspace_id = user.workspaces[0].workspace if user.workspaces else None
    if not workspace_id:
        return None

    doc = _SessionDoc(
        sessionId=session_key,
        context_type="pocket",
        workspace=workspace_id,
        owner=str(user.id),
        title="Chat",
    )
    await doc.insert()
    logger.info("auto-created pocket session: sessionId=%s owner=%s", session_key, user.id)
    return doc


async def find_by_session_id(session_id: str) -> _SessionDoc | None:
    """Return the ``Session`` Beanie doc keyed by ``sessionId`` (the safe
    string form, not the Mongo ObjectId), or ``None`` if missing.

    Used by the cloud memory store for its session-resolution path so it
    doesn't import ``Session`` directly.
    """
    return await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)


async def touch_doc(doc: _SessionDoc) -> None:
    """Increment ``messageCount`` and refresh ``lastActivity`` on an
    already-loaded ``Session`` doc. Failures log but don't raise.

    Variant of :func:`touch` for callers that already have the doc in
    hand (the cloud memory store's write path) — saves a refetch.
    """
    try:
        doc.lastActivity = datetime.now(UTC)
        doc.messageCount = (doc.messageCount or 0) + 1
        await doc.save()
    except Exception:
        logger.warning("failed to touch Session %s", doc.sessionId, exc_info=True)


async def attach_pocket_to_session_doc(
    session_mongo_id: str, user_id: str, pocket_id: str
) -> str | None:
    """Link an existing session (by Beanie ``_id``) to a freshly-created
    pocket. The session's ``pocket`` and ``context_type`` are updated and
    persisted. Returns the session's ``_id`` (as str) on success, ``None``
    if the session is missing, owned by someone else, or the save failed.

    Used by the in-process MCP ``create_pocket`` tool so the chat that
    built the pocket shows up in that pocket's session list — without
    this the conversation gets orphaned at the workspace level.
    """
    try:
        doc = await _SessionDoc.get(PydanticObjectId(session_mongo_id))
    except Exception:
        logger.warning(
            "attach_pocket_to_session_doc: invalid session id %s",
            session_mongo_id,
            exc_info=True,
        )
        return None
    if doc is None or doc.owner != user_id:
        return None
    doc.pocket = pocket_id
    doc.context_type = "pocket"
    try:
        await doc.save()
    except Exception:
        logger.warning(
            "attach_pocket_to_session_doc: save failed for %s",
            session_mongo_id,
            exc_info=True,
        )
        return None
    return str(doc.id)


async def set_title_if_default(session_id: str, title: str) -> bool:
    """Persist ``title`` only when the current title is still a system
    default (``"New Chat"`` / ``"Chat"`` / empty). Used by the cloud
    title listener so user-edited titles aren't clobbered when a delayed
    backend titler fires for the same session.

    Returns ``True`` if the title was written, ``False`` if the session
    is missing, already has a user-edited title, or the save failed.
    """
    try:
        doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
    except Exception:
        logger.warning("session lookup failed for %s", session_id, exc_info=True)
        return False
    if doc is None:
        return False
    if doc.title and doc.title not in ("New Chat", "Chat"):
        return False

    doc.title = title
    try:
        await doc.save()
    except Exception:
        logger.warning("session title save failed for %s", session_id, exc_info=True)
        return False

    try:
        await emit(
            SessionUpdated(
                data={
                    "session_id": str(doc.id),
                    "user_id": doc.owner,
                    "title": title,
                }
            )
        )
    except Exception:
        logger.warning("session title emit failed for %s", session_id, exc_info=True)
    return True


async def set_title(session_id: str, title: str) -> bool:
    """Write ``title`` to ``Session.title`` and broadcast ``SessionUpdated``.

    Returns ``True`` on a successful write. Best-effort — Mongo lookup or
    save failures log and return ``False`` so the caller can continue
    with the SSE-only path.
    """
    try:
        doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
    except Exception:
        logger.warning("session lookup failed for %s", session_id, exc_info=True)
        return False
    if doc is None:
        return False

    doc.title = title
    try:
        await doc.save()
    except Exception:
        logger.warning("session title save failed for %s", session_id, exc_info=True)
        return False

    try:
        await emit(
            SessionUpdated(
                data={
                    "session_id": str(doc.id),
                    "user_id": doc.owner,
                    "title": title,
                }
            )
        )
    except Exception:
        logger.debug("SessionUpdated emit failed for %s", session_id, exc_info=True)
    return True


async def touch(session_id: str) -> None:
    """Update lastActivity and increment messageCount.

    Called by chat persistence bridges on the hot path; kept on Beanie.
    """
    doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id)
    if not doc and session_id.startswith("websocket_"):
        doc = await _SessionDoc.find_one(_SessionDoc.sessionId == session_id[10:])
    if not doc:
        return
    doc.lastActivity = datetime.now(UTC)
    doc.messageCount += 1
    await doc.save()

    await emit(
        SessionUpdated(
            data={
                "session_id": str(doc.id),
                "user_id": doc.owner,
                "last_message_at": iso_utc(doc.lastActivity),
            }
        )
    )


__all__ = [
    "attach_pocket_to_session_doc",
    "auto_create_pocket_session",
    "create",
    "delete",
    "ensure_for_agent_scope",
    "find_by_session_id",
    "get",
    "get_history",
    "legacy_ctx",
    "link_pocket",
    "list_by_agent",
    "list_for_owner",
    "list_for_pocket",
    "set_title",
    "set_title_if_default",
    "touch",
    "touch_doc",
    "update",
]
