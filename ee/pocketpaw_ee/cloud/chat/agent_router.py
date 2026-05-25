"""Enterprise agent chat — ``POST /cloud/chat/{scope}/{scope_id}/agent``.

Streams a typed SSE sequence in the response body while persisting the user
message, submitting a ``Run`` to the configured executor, and tailing the
run's Redis Stream so durability sits underneath the wire shape the
frontend already speaks.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from pocketpaw_ee.cloud._core.errors import CloudError
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
from pocketpaw_ee.cloud.surface import resolve_surface_context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


Scope = Literal["dm", "group", "pocket", "session"]


def _sse(event: str, data: dict[str, Any], *, entry_id: str | None = None) -> bytes:
    # ``id:`` powers EventSource Last-Event-Id resume; synthetic frames omit it.
    head = f"id: {entry_id}\n" if entry_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode()


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
        ctx.intent = body.intent
    except InvalidScope:
        raise CloudError(400, "scope.invalid", "Invalid scope") from None

    transport = get_stream_transport()
    # Resolve the surface-aware context preamble AFTER scope is resolved
    # (so we have ``workspace_id`` / ``user_id`` confirmed) and BEFORE any
    # other prompt assembly. The resolver never raises — failures fall
    # back to a GENERIC context with an empty preamble, which
    # ``build_dynamic_context`` then treats as the legacy three-line
    # shape. Older clients that send neither ``surface`` nor
    # ``surface_meta`` land here as ``{surface: None, meta: {}}`` and
    # produce a GENERIC context with a placeholder preamble that the
    # router still attaches; the chat continues to work either way.
    ctx.surface_context = await resolve_surface_context(
        ctx.workspace_id,
        user_id,
        {"surface": body.surface, "meta": body.surface_meta or {}},
    )

    # Supersede any prior in-flight run for this scope. ``request_cancel``
    # writes the cancel flag in Redis so a worker in another process notices.
    prior = await run_service.find_active_run_for_scope(
        workspace_id=workspace_id, context_type=scope, scope_id=scope_id
    )
    if prior is not None:
        await transport.request_cancel(prior.run_id)

    # Load history BEFORE persisting the new user message so it excludes this turn.
    history = await load_history_for_scope(ctx)
    user_message_id = await _persist_user_message(ctx, body)

    # Resolve the sidebar Session up-front so ``message.persisted`` carries
    # ``session_id`` — a mid-stream refresh can still find the thread.
    try:
        ctx.session_id = await _ensure_scope_session(ctx)
    except Exception:
        logger.exception("ensure session failed for scope %s", ctx.kind.value)
        ctx.session_id = None

    client_message_id = body.client_message_id or uuid.uuid4().hex
    spec = RunSpec(
        run_id=uuid.uuid4().hex,
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
    # create_run is idempotent on (workspace, client_message_id) — when a doc
    # already exists, re-use its run_id so the executor + SSE stream both
    # tail the same Redis Stream as the prior request for this client_message_id.
    run = await run_service.create_run(spec)
    if run.run_id != spec.run_id:
        spec = spec.model_copy(update={"run_id": run.run_id})
    run_id = run.run_id
    await get_executor().submit(spec)

    async def gen() -> AsyncIterator[bytes]:
        persisted_payload: dict[str, Any] = {
            "user_message_id": user_message_id,
            "client_message_id": client_message_id,
            "run_id": run_id,
        }
        if ctx.session_id:
            persisted_payload["session_id"] = ctx.session_id
        yield _sse("message.persisted", persisted_payload)

        cursor = "0"
        while True:
            saw_terminal = False
            async for ev in transport.read_events(run_id, after=cursor, block_ms=15000):
                cursor = ev.entry_id
                yield _sse(ev.event, ev.data, entry_id=ev.entry_id)
                if ev.is_terminal:
                    saw_terminal = True
            if saw_terminal:
                return
            yield b": ping\n\n"

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
    user_id: str = Depends(current_user_id),  # noqa: ARG001
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Cancel the active run for this scope. Idempotent — returns ``ok`` even
    when no run is in flight so the frontend's fire-and-forget stop button
    doesn't surface a 404 toast."""
    prior = await run_service.find_active_run_for_scope(
        workspace_id=workspace_id, context_type=scope, scope_id=scope_id
    )
    if prior is not None:
        await get_stream_transport().request_cancel(prior.run_id)
    return {"status": "ok"}


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
    # Bypasses ``send_message`` to skip the legacy ``agent_bridge`` auto-response —
    # the run executor is the sole driver of the reply here.
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
