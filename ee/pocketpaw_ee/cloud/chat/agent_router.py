"""Enterprise agent chat — POST endpoint.

``POST /cloud/chat/{scope}/{scope_id}/agent`` accepts a new user message,
persists it, creates a :class:`ChatRunDoc`, and hands the run to a
:class:`RunExecutor`. The endpoint returns JSON immediately —
``{run_id, user_message_id, session_id, client_message_id}`` — and does
NOT stream.

The agent runs detached from the HTTP request. Its events stream through
``GET /cloud/chat/runs/{run_id}/stream`` (see ``runs/router.py``), which
reads the resumable Redis Stream the executor writes to. Cancellation is
``POST /cloud/chat/runs/{run_id}/stop``, also in ``runs/router.py``.

This module owns the HTTP plumbing and scope/auth guards; the agent loop
lives in :mod:`pocketpaw_ee.cloud.chat.runs.run_core` and is reached via
the executor seam (``runs/executor.py``).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from pocketpaw_ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    load_history_for_scope,
    resolve_scope_context,
    session_key_for,
)
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.executor import get_executor
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud.shared.errors import CloudError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


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
) -> dict[str, Any]:
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

    # Cancel any prior in-flight run for this scope (cross-process via Redis).
    # The new ``/cloud/chat/runs/{run_id}/stop`` endpoint and this preempt
    # path together replace the old in-process ``_active_runs`` registry.
    prior = await run_service.find_active_run_for_scope(
        workspace_id=workspace_id, context_type=scope, scope_id=scope_id
    )
    if prior is not None:
        await get_stream_transport().request_cancel(prior.run_id)

    # Load prior turns BEFORE persisting the new user message so ``history``
    # contains only the conversation up to (but not including) this request.
    history = await load_history_for_scope(ctx)
    user_message_id = await _persist_user_message(ctx, body)

    # Resolve the sidebar Session up-front so the run carries ``session_id``
    # from the very first event. A mid-stream refresh can then still find
    # the thread in the sidebar instead of losing it.
    try:
        ctx.session_id = await _ensure_scope_session(ctx)
    except Exception:
        logger.exception("ensure session failed for scope %s", ctx.kind.value)
        ctx.session_id = None

    client_message_id = body.client_message_id or uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    spec = RunSpec(
        run_id=run_id,
        workspace_id=workspace_id,
        context_type=scope,
        scope_id=scope_id,
        session_key=session_key_for(ctx),
        group=scope_id if scope in ("dm", "group") else None,
        user_id=user_id,
        agent_id=ctx.target_agent_id,
        client_message_id=client_message_id,
        user_message_id=user_message_id,
        content=body.content,
        history=history,
        intent=body.intent,
        attachments=body.attachments or [],
        mentions=[],
        reply_to=body.reply_to,
    )
    run = await run_service.create_run(spec)  # idempotent on (workspace, client_message_id)
    await get_executor().submit(spec)

    return {
        "run_id": run.run_id,
        "user_message_id": user_message_id,
        "session_id": ctx.session_id,
        "client_message_id": client_message_id,
    }


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
    legacy ``agent_bridge`` auto-response path — the run executor is the
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
