"""Enterprise agent chat — SSE endpoint.

``POST /cloud/chat/{scope}/{scope_id}/agent`` streams a typed SSE sequence
to the caller while persisting the user message and (at stream end) the
assistant message. The agent loop itself lives in
:mod:`pocketpaw_ee.cloud.chat.runs.run_core`; this module owns the HTTP
+ SSE plumbing and scope/auth guards.

The agent-loop body, persistence/broadcast helpers, ripple extraction,
specialist-response binding and first-turn auto-titling have all been
moved to ``runs/run_core.py`` (Task 6 of the resumable-runs plan).
``_run_agent_stream`` is re-exported here as a temporary shim so the
existing tests and the POST endpoint keep working until Task 9 rewrites
the endpoint to dispatch through a ``RunExecutor``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from pocketpaw.agents.pool import get_agent_pool as get_agent_pool  # backwards-compat
from pocketpaw_ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from pocketpaw_ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    load_history_for_scope,
    resolve_scope_context,
    session_key_for,
)
from pocketpaw_ee.cloud.chat.agent_service import (
    # Re-exported for legacy patch-by-name tests (Task-6 transitional state).
    build_knowledge_context as build_knowledge_context,
)
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _broadcast_agent_typing as _broadcast_agent_typing,  # backwards-compat
)
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _broadcast_message_new as _broadcast_message_new,  # backwards-compat
)
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _generate_session_title as _generate_session_title,  # backwards-compat
)
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _persist_assistant_message as _persist_assistant_message,  # backwards-compat
)
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _run_agent_stream_shim_for_task6 as _run_agent_stream,
)
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _set_session_title_in_mongo as _set_session_title_in_mongo,  # backwards-compat
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud.shared.errors import CloudError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


# In-process cancel registry keyed by (scope, scope_id, user_id). A new request
# for the same tuple cancels the prior run — mirrors OSS /chat/stream semantics.
# This dict and the /agent/stop endpoint are deleted in Task 9 once the new
# /cloud/chat/runs/{id}/stop endpoint (cross-process via Redis) takes over.
_active_runs: dict[tuple[str, str, str], asyncio.Event] = {}


Scope = Literal["dm", "group", "pocket", "session"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/cloud/chat/{scope}/{scope_id}/agent")
async def post_agent_chat(
    scope: Scope,
    scope_id: str,
    body: CloudAgentChatRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    try:
        ctx = await resolve_scope_context(
            scope=scope, scope_id=scope_id, user_id=user_id, agent_id_hint=body.agent_id
        )
        # Carry the client's intent hint into the system-prompt builder so
        # ``build_context_block`` can swap to the create-pocket guidance
        # when the user is in pocket-creation mode.
        ctx.intent = body.intent
    except InvalidScope:
        raise CloudError(400, "scope.invalid", "Invalid scope") from None
    except CloudError:
        raise

    # Signal any prior in-flight run for the same (scope, scope_id, user_id)
    # to stop. We don't wait on it — each generator cleans its own slot in
    # ``_active_runs`` only when the slot still points to its own event, so
    # the new request's entry is safe from the old generator's ``finally``.
    key = (scope, scope_id, user_id)
    prev = _active_runs.get(key)
    if prev is not None:
        prev.set()

    cancel_event = asyncio.Event()
    _active_runs[key] = cancel_event

    # Load prior turns BEFORE persisting the new user message so ``history``
    # contains only the conversation up to (but not including) this request.
    history = await load_history_for_scope(ctx)

    try:
        user_message_id = await _persist_user_message(ctx, body)
    except CloudError:
        if _active_runs.get(key) is cancel_event:
            _active_runs.pop(key, None)
        raise
    except Exception:
        if _active_runs.get(key) is cancel_event:
            _active_runs.pop(key, None)
        raise

    # Resolve the sidebar Session up-front so ``message.persisted`` and
    # ``stream_start`` carry ``session_id``. Frontend adopts it immediately,
    # which means a mid-stream refresh still finds the thread in the sidebar
    # instead of losing it until ``stream_end``.
    try:
        ctx.session_id = await _ensure_scope_session(ctx)
    except Exception:
        logger.exception("Failed to ensure sidebar session for scope %s", ctx.kind.value)
        ctx.session_id = None

    async def gen() -> AsyncIterator[bytes]:
        try:
            persisted_payload: dict[str, Any] = {
                "user_message_id": user_message_id,
                "client_message_id": body.client_message_id,
            }
            if ctx.session_id:
                persisted_payload["session_id"] = ctx.session_id
            yield _sse("message.persisted", persisted_payload)
            async for name, data in _run_agent_stream(
                ctx, user_message_id, body, cancel_event, history=history
            ):
                yield _sse(name, data)
                if name in ("stream_end", "error"):
                    break
        finally:
            # Only clear the slot if it still belongs to this run — a
            # superseding request will have replaced ``_active_runs[key]``
            # with its own event, and we must not evict that.
            if _active_runs.get(key) is cancel_event:
                _active_runs.pop(key, None)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cloud/chat/{scope}/{scope_id}/agent/stop")
async def post_agent_chat_stop(
    scope: Scope,
    scope_id: str,
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    key = (scope, scope_id, user_id)
    ev = _active_runs.get(key)
    if ev is None:
        from pocketpaw_ee.cloud._core.errors import NotFound

        raise NotFound("active_run", f"{scope}:{scope_id}")
    ev.set()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Collaborators that still belong to the HTTP layer
# ---------------------------------------------------------------------------


async def _ensure_scope_session(ctx: ScopeContext) -> str | None:
    """Find-or-create the :class:`Session` document that the sidebar uses to
    surface this scope+agent pair. Returns the session's ``sessionId`` field
    so the SSE stream can emit it early — frontend :func:`adoptSessionId`
    then upserts the thread into the sidebar *before* the stream completes,
    which lets a mid-stream refresh still find the chat.

    Delegates to :func:`sessions.service.ensure_for_agent_scope` so the
    Session Beanie writes stay inside the sessions entity.
    """
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    return await sessions_service.ensure_for_agent_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        target_agent_id=ctx.target_agent_id,
    )


async def _persist_user_message(ctx: ScopeContext, body: CloudAgentChatRequest) -> str:
    """Persist the caller's message via ``message_service`` and return its id.

    We bypass ``message_service.send_message`` to avoid triggering the
    legacy ``agent_bridge`` auto-response path — the SSE endpoint is the
    sole driver of the reply for this request.
    """
    from pocketpaw_ee.cloud.chat import message_service

    return await message_service.persist_user_message_for_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_key=session_key_for(ctx),
        content=body.content,
        attachments=body.attachments,
        mentions=body.mentions,
        reply_to=body.reply_to,
    )


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]
