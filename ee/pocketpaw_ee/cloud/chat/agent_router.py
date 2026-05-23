"""Enterprise agent chat — ``POST /cloud/chat/{scope}/{scope_id}/agent``.

Persists the user message, creates a ``ChatRunDoc``, hands it to a
``RunExecutor``, and returns JSON immediately. The agent's events stream
through ``GET /cloud/chat/runs/{run_id}/stream`` and cancel via
``POST /cloud/chat/runs/{run_id}/stop`` (see ``runs/router.py``).
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
        ctx.intent = body.intent
    except InvalidScope:
        raise CloudError(400, "scope.invalid", "Invalid scope") from None

    # Cancel any prior in-flight run for this scope (cross-process via Redis).
    prior = await run_service.find_active_run_for_scope(
        workspace_id=workspace_id, context_type=scope, scope_id=scope_id
    )
    if prior is not None:
        await get_stream_transport().request_cancel(prior.run_id)

    # Load history BEFORE persisting the new user message so ``history``
    # contains only turns up to (but not including) this request.
    history = await load_history_for_scope(ctx)
    user_message_id = await _persist_user_message(ctx, body)

    # Resolve the sidebar Session up-front so the run carries ``session_id``
    # from the first event — lets a mid-stream refresh find the thread.
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
    run = await run_service.create_run(spec)
    await get_executor().submit(spec)

    return {
        "run_id": run.run_id,
        "user_message_id": user_message_id,
        "session_id": ctx.session_id,
        "client_message_id": client_message_id,
    }


async def _ensure_scope_session(ctx: ScopeContext) -> str | None:
    """Find-or-create the sidebar ``Session`` for this scope+agent pair."""
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    return await sessions_service.ensure_for_agent_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        target_agent_id=ctx.target_agent_id,
    )


async def _persist_user_message(ctx: ScopeContext, body: CloudAgentChatRequest) -> str:
    # Bypasses ``send_message`` to skip the legacy ``agent_bridge`` auto-response
    # path — the run executor is the sole driver of the reply here.
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
