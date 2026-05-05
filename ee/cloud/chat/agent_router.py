"""Enterprise agent chat — SSE endpoint.

``POST /cloud/chat/{scope}/{scope_id}/agent`` streams a typed SSE sequence
to the caller while persisting the user message and (at stream end) the
assistant message. Agent run mechanics live in Task 7 — this module owns
the HTTP + SSE plumbing and scope/auth guards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    ScopeKind,
    attach_agent_identity,
    attach_sse_event_sink,
    detach_agent_identity,
    detach_sse_event_sink,
    load_history_for_scope,
    push_sse_event,
    resolve_scope_context,
    session_key_for,
)
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.shared.errors import CloudError
from pocketpaw.agents.pool import get_agent_pool  # re-exported for test patching

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


# In-process cancel registry keyed by (scope, scope_id, user_id). A new request
# for the same tuple cancels the prior run — mirrors OSS /chat/stream semantics.
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
    # The in-process SDK subprocess can't be relied on across backend restarts
    # or pool evictions — Mongo is the source of truth.
    history = await load_history_for_scope(ctx)

    try:
        user_message_id = await _persist_user_message(ctx, body)
    except CloudError:
        # Clean up our slot on failure so we don't leak a cancel event.
        if _active_runs.get(key) is cancel_event:
            _active_runs.pop(key, None)
        raise
    except Exception:
        # Any other failure (Mongo error, Pydantic validation, …) must also
        # clear the slot so subsequent requests for this (scope, scope_id,
        # user_id) don't see a dangling cancel event. Re-raise so FastAPI
        # surfaces the original failure unchanged.
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
        from ee.cloud._core.errors import NotFound

        raise NotFound("active_run", f"{scope}:{scope_id}")
    ev.set()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


RIPPLE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


async def _ensure_scope_session(ctx: ScopeContext) -> str | None:
    """Find-or-create the :class:`Session` document that the sidebar uses to
    surface this scope+agent pair. Returns the session's ``sessionId`` field
    so the SSE stream can emit it early — frontend :func:`adoptSessionId`
    then upserts the thread into the sidebar *before* the stream completes,
    which lets a mid-stream refresh still find the chat.

    Delegates to :func:`sessions.service.ensure_for_agent_scope` so the
    Session Beanie writes stay inside the sessions entity.
    """
    from ee.cloud.sessions import service as sessions_service

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
    from ee.cloud.chat import message_service

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


async def _persist_assistant_message(
    ctx: ScopeContext, content: str, attachments: list[dict[str, Any]]
) -> Any:
    from ee.cloud.chat import message_service

    return await message_service.persist_assistant_message_for_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_key=session_key_for(ctx),
        target_agent_id=ctx.target_agent_id,
        content=content,
        attachments=attachments,
    )


async def _broadcast_message_new(
    ctx: ScopeContext,
    message_id: str,
    content: str,
    attachments: list[dict[str, Any]],
    created_at: datetime,
) -> None:
    """Broadcast the finished assistant message to every other scope member."""
    from ee.cloud.chat.schemas import WsOutbound
    from ee.cloud.chat.ws import manager

    others = [m for m in ctx.members if m != ctx.user_id]
    if not others:
        return
    await manager.broadcast_to_group(
        ctx.scope_id,
        others,
        WsOutbound(
            type="message.new",
            data={
                "id": message_id,
                "group": ctx.scope_id,
                "sender_type": "agent",
                "agent": ctx.target_agent_id,
                "content": content,
                "attachments": attachments,
                "created_at": created_at.isoformat(),
            },
        ),
    )


async def _broadcast_agent_typing(ctx: ScopeContext, active: bool) -> None:
    from ee.cloud.chat.schemas import WsOutbound
    from ee.cloud.chat.ws import manager

    others = [m for m in ctx.members if m != ctx.user_id]
    if not others:
        return
    await manager.broadcast_to_group(
        ctx.scope_id,
        others,
        WsOutbound(
            type="agent.typing",
            data={
                "scope": ctx.kind.value,
                "scope_id": ctx.scope_id,
                "agent_id": ctx.target_agent_id,
                "active": active,
            },
        ),
    )


async def _run_agent_stream(
    ctx: ScopeContext,
    user_message_id: str,
    body: CloudAgentChatRequest,
    cancel_event: asyncio.Event,
    *,
    history: list[dict[str, str]] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Drive AgentPool.run and translate events into SSE tuples."""
    run_id = _new_run_id()
    session_key = session_key_for(ctx)

    pool = get_agent_pool()
    try:
        instance = await pool.get(ctx.target_agent_id)
    except Exception as e:
        logger.exception("Failed to load agent instance %s", ctx.target_agent_id)
        yield ("error", {"code": "agent.load_failed", "message": str(e)})
        return

    # Inject the scope/participants block via knowledge_context — AgentPool.run
    # prepends this to the system prompt, which is the least invasive way to
    # give the agent scope awareness without changing pool.run's signature.
    from ee.cloud.chat.agent_service import build_context_block

    # Surface the backend name so the prompt picks the right pocket-tool
    # path (MCP for claude_agent_sdk, shell CLI for codex_cli + others).
    backend_name: str | None = None
    try:
        backend_name = instance.backend.info().name
    except Exception:
        logger.debug("backend.info() unavailable on agent %s", ctx.target_agent_id, exc_info=True)
    scope_block = build_context_block(ctx, backend_name=backend_name)

    # Stage 3.E: pull KB context with per-request scope priority
    # (pocket > agent > workspace). The per-request resolver lives on
    # ``AgentContextBuilder`` so the OSS / channel paths share the same
    # logic, just with a static ``settings.kb_scopes`` fallback. We
    # prepend the result to the scope block so the agent sees ``###
    # From <scope>`` sections alongside the participants tag.
    try:
        from pocketpaw.bootstrap.context_builder import (
            AgentContextBuilder,
            KbContext,
        )

        kb_block = await AgentContextBuilder._get_kb_context(
            body.content,
            kb_ctx=KbContext(
                pocket_id=ctx.pocket_id,
                agent_id=ctx.target_agent_id,
                workspace_id=ctx.workspace_id,
            ),
        )
    except Exception:
        # KB context is best-effort — a transient kb-go / Mongo blip never
        # blocks the chat reply. Log at debug for the failure trail.
        logger.debug("KB context fetch failed for cloud chat", exc_info=True)
        kb_block = ""

    if kb_block:
        scope_block = f"{scope_block}\n\n{kb_block}"

    await _broadcast_agent_typing(ctx, active=True)

    stream_start_payload: dict[str, Any] = {
        "run_id": run_id,
        "agent_id": ctx.target_agent_id,
        "agent_name": getattr(instance, "agent_name", ""),
        "scope": ctx.kind.value,
        "scope_id": ctx.scope_id,
    }
    if ctx.session_id:
        stream_start_payload["session_id"] = ctx.session_id
    yield ("stream_start", stream_start_payload)

    # Bind a per-stream queue for side-channel emitters to push onto. The
    # MCP pocket-write tools (``update_pocket``, ``add_widget``, …) call
    # ``push_sse_event("pocket_mutation", …)`` after Mongo writes, and the
    # background session-titler pushes ``session_titled``. We drain the
    # queue between SDK events so those SSE frames reach the client in
    # near real time — the canvas / sidebar can update before the agent's
    # text reply finishes.
    side_channel_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    sink_token = attach_sse_event_sink(side_channel_queue)
    # ``ctx.scope_id`` is the session's Mongo ``_id`` when the chat was
    # routed via session scope — that's what ``create_pocket_for_agent``
    # needs to flip ``Session.pocket`` to the freshly-created pocket so
    # the chat that built it shows up in the pocket's session list
    # instead of being orphaned at the workspace level.
    session_mongo_id = ctx.scope_id if ctx.kind is ScopeKind.SESSION else None
    identity_tokens = attach_agent_identity(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        session_mongo_id=session_mongo_id,
    )

    # First-turn auto-titling. The OSS bus path runs this from
    # ``AgentLoop._generate_and_emit_title``; the cloud path bypasses
    # ``AgentLoop`` entirely, so without this hook cloud sessions stuck
    # at "New Chat" forever. Spawn AFTER ``attach_sse_event_sink`` so the
    # task inherits the contextvar binding and can ``push_sse_event`` the
    # ``session_titled`` frame onto this stream when generation finishes.
    if not history and ctx.session_id:
        asyncio.create_task(_generate_session_title(ctx, body.content))

    def _drain_side_channel() -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        while True:
            try:
                events.append(side_channel_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    full_text = ""
    cancelled = False
    try:
        async for event in pool.run(
            ctx.target_agent_id,
            body.content,
            session_key,
            history=history,
            knowledge_context=scope_block,
        ):
            for ev in _drain_side_channel():
                yield ev
            if cancel_event.is_set():
                cancelled = True
                break
            etype = getattr(event, "type", None)
            econtent = getattr(event, "content", "")
            if etype == "message":
                full_text += econtent if isinstance(econtent, str) else ""
                yield ("chunk", {"content": econtent, "type": "text"})
            elif etype == "thinking":
                yield ("thinking", {"content": econtent if isinstance(econtent, str) else ""})
            elif etype == "tool_use":
                name = ""
                if isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                elif isinstance(econtent, str):
                    name = econtent
                yield (
                    "tool_start",
                    {"tool": name, "input": econtent if isinstance(econtent, dict) else {}},
                )
            elif etype == "tool_result":
                name = ""
                output: Any = econtent
                if isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                    output = econtent.get("result", econtent)
                yield ("tool_result", {"tool": name, "output": output})
            elif etype == "done":
                break
        # Flush anything the agent emitted right before ``done`` / break.
        for ev in _drain_side_channel():
            yield ev
    except Exception as e:
        logger.exception("Cloud agent run failed for agent=%s", ctx.target_agent_id)
        yield ("error", {"code": "agent.run_failed", "message": str(e)})
        await _broadcast_agent_typing(ctx, active=False)
        return
    finally:
        try:
            detach_sse_event_sink(sink_token)
        except Exception:
            pass
        try:
            detach_agent_identity(identity_tokens)
        except Exception:
            pass

    # Extract ripple block from the accumulated text (same regex as agent_bridge).
    attachments: list[dict[str, Any]] = []
    match = RIPPLE_JSON_RE.search(full_text)
    if match:
        try:
            candidate = json.loads(match.group(1))
        except Exception:
            candidate = None
            logger.debug("Ripple parse failed", exc_info=True)
        if isinstance(candidate, dict) and ("lifecycle" in candidate or "widgets" in candidate):
            spec: dict[str, Any] = candidate
            try:
                from ee.cloud.ripple_normalizer import normalize_ripple_spec

                normalized = normalize_ripple_spec(candidate)
                if normalized:
                    spec = normalized
            except Exception:
                logger.debug("Ripple normalize failed", exc_info=True)
            attachments.append({"type": "ripple", "meta": spec})
            full_text = (full_text[: match.start()] + full_text[match.end() :]).strip()
            yield ("ripple", {"spec": spec})

    if cancelled or not full_text.strip():
        yield ("stream_end", {"assistant_message_id": None, "usage": {}, "cancelled": cancelled})
        await _broadcast_agent_typing(ctx, active=False)
        return

    assistant_msg = await _persist_assistant_message(ctx, full_text, attachments)
    assistant_id = str(assistant_msg.id)
    await _broadcast_message_new(
        ctx, assistant_id, full_text, attachments, created_at=assistant_msg.createdAt
    )
    await _broadcast_agent_typing(ctx, active=False)

    # Per-agent soul observation — routed to the target agent's SoulManager
    # via AgentPool. Never touches the global default PocketPaw soul.
    try:
        await pool.observe(ctx.target_agent_id, body.content, full_text)
    except Exception:
        logger.warning(
            "pool.observe failed for agent %s — per-agent soul not updated",
            ctx.target_agent_id,
            exc_info=True,
        )

    yield (
        "stream_end",
        {"assistant_message_id": assistant_id, "usage": {}, "cancelled": False},
    )


# ---------------------------------------------------------------------------
# First-turn auto-titling
# ---------------------------------------------------------------------------


_DEFAULT_TITLES = ("", "New Chat", "Chat")
_TITLE_PLACEHOLDER_LIMIT = 60


def _truncate_for_title(message: str) -> str:
    """One-line, ~tweet-sized preview of the user's first message.

    Mirrors the frontend ``deriveTitleFromFirstMessage`` heuristic so the
    cloud-server-generated placeholder matches what the desktop client
    shows for sessions it adopts locally.
    """
    raw = (message or "").strip().replace("\n", " ").replace("\r", " ")
    one_line = " ".join(raw.split())
    if len(one_line) > _TITLE_PLACEHOLDER_LIMIT:
        return one_line[:_TITLE_PLACEHOLDER_LIMIT].rstrip() + "…"
    return one_line


async def _set_session_title_in_mongo(session_id: str, title: str) -> bool:
    """Persist a title via :func:`sessions.service.set_title`.

    Best-effort — failures inside the service log and return ``False`` so
    the caller can continue with the SSE-only path.
    """
    from ee.cloud.sessions import service as sessions_service

    return await sessions_service.set_title(session_id, title)


async def _generate_session_title(ctx: ScopeContext, first_message: str) -> None:
    """Set an immediate placeholder title from the user's first message,
    then upgrade it with a Haiku-generated title in the background.

    Two-stage to keep the sidebar from sticking on "New Chat" while the
    titler call is in flight (or if it fails entirely):

    1. **Placeholder**: a truncated, one-line version of ``first_message``
       written to Mongo (only if the current title is still a default
       placeholder) and pushed onto the SSE side-channel so the open
       stream gets an instant ``session_titled`` event.
    2. **Haiku**: ``pocketpaw.memory.titler.generate_title`` produces a
       short, well-formed title which overwrites the placeholder. Writes
       go through the same path so the listener guard doesn't block them.

    Best-effort: any failure logs and returns. Runs as a background task
    spawned from ``_run_agent_stream`` so it overlaps the agent reply
    instead of adding latency to the SSE first-byte.
    """
    if not ctx.session_id:
        return

    # Stage 1 — instant placeholder from the user's first message. Only
    # runs on the first turn (caller-gated via ``history`` empty), so the
    # current title is always a system default ("New Chat" / "Chat") and
    # the write is safe without an extra round-trip to read it.
    placeholder = _truncate_for_title(first_message)
    if placeholder:
        if await _set_session_title_in_mongo(ctx.session_id, placeholder):
            push_sse_event(
                "session_titled",
                {"session_id": ctx.session_id, "title": placeholder},
            )

    # Stage 2 — Haiku-generated title that overwrites the placeholder.
    try:
        from pocketpaw.config import Settings
        from pocketpaw.memory.titler import generate_title

        settings = Settings.load()
        title = await generate_title(
            first_message,
            model=settings.chat_title_model,
            api_key=settings.anthropic_api_key or None,
        )
    except Exception:
        logger.warning(
            "cloud Haiku title generation failed for %s", ctx.session_id, exc_info=True
        )
        return

    if not title or title == placeholder:
        return

    if await _set_session_title_in_mongo(ctx.session_id, title):
        push_sse_event(
            "session_titled",
            {"session_id": ctx.session_id, "title": title},
        )


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]
