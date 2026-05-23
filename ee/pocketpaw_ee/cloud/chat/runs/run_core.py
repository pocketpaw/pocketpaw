"""Agent-run core — the loop the executor invokes for every chat run.

The single entry point ``execute_run(spec)`` rebuilds a ``ScopeContext``
from the ``RunSpec``, drives the agent via ``_iter_agent_events`` and
writes every event through a :class:`RunStreamTransport`. The
in-process and arq executors invoke this same function.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from pocketpaw.agents.pool import get_agent_pool  # re-exported for test patching
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    attach_agent_identity,
    attach_sse_event_sink,
    build_behavior_instructions,
    build_knowledge_context,
    detach_agent_identity,
    detach_sse_event_sink,
    push_sse_event,
    session_key_for,
)
from pocketpaw_ee.cloud.chat.agent_service import (
    resolve_scope_context as resolve_scope_context,
)
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport

logger = logging.getLogger(__name__)


RIPPLE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _stream_ttl() -> int:
    """Read the run-stream TTL at call time so tests can monkeypatch env."""
    return int(os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "900"))


# ---------------------------------------------------------------------------
# Persistence + broadcast helpers
# ---------------------------------------------------------------------------


async def _persist_assistant_message(
    ctx: ScopeContext, content: str, attachments: list[dict[str, Any]]
) -> Any:
    from pocketpaw_ee.cloud.chat import message_service

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
    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import manager

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
    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import manager

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


# ---------------------------------------------------------------------------
# Specialist response detection
# ---------------------------------------------------------------------------


def _extract_specialist_payload(output: Any) -> dict[str, Any] | None:
    """Return the specialist's ``{ok, action, pocket, ...}`` dict if ``output``
    looks like a pocket-specialist response, else ``None``.

    The same payload surfaces in three shapes depending on the agent backend:
      * raw dict — in-process function tool returning the dict directly
      * JSON string — most common (BaseTool, codex shell stdout, MCP text)
      * list of MCP content blocks — claude_agent_sdk's in-process MCP server
        wraps the JSON in ``[{"type": "text", "text": "<json>"}]``
    """
    if output is None:
        return None

    def _coerce(data: Any) -> dict[str, Any] | None:
        if (
            isinstance(data, dict)
            and "ok" in data
            and "action" in data
            and isinstance(data.get("pocket"), dict)
        ):
            return data
        return None

    if isinstance(output, dict):
        direct = _coerce(output)
        if direct is not None:
            return direct
        content = output.get("content")
        if isinstance(content, list):
            return _extract_specialist_payload(content)
        return None

    if isinstance(output, str):
        text = output.strip()
        if not text or not text.startswith("{"):
            return None
        try:
            return _coerce(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            return None

    if isinstance(output, list):
        for block in output:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parsed = _extract_specialist_payload(block.get("text", ""))
                if parsed is not None:
                    return parsed
        return None

    return None


async def _maybe_handle_specialist_response(
    *,
    ctx: ScopeContext,
    session_mongo_id: str | None,
    output: Any,
    handled_pocket_ids: set[str],
) -> None:
    """Bind session → pocket and push ``pocket_created`` SSE on detection.

    Idempotent per ``pocket_id``. Failure to bind or to push the SSE frame
    is logged and swallowed — neither is allowed to break the agent stream.
    """
    payload = _extract_specialist_payload(output)
    if payload is None:
        return
    if not payload.get("ok"):
        return
    pocket = payload.get("pocket") or {}
    pocket_id = pocket.get("id") or pocket.get("_id")
    if not pocket_id or pocket_id in handled_pocket_ids:
        return
    handled_pocket_ids.add(pocket_id)

    if session_mongo_id:
        try:
            from pocketpaw_ee.cloud.sessions import service as sessions_service

            await sessions_service.attach_pocket_to_session_doc(
                session_mongo_id, ctx.user_id, pocket_id
            )
        except Exception:
            logger.warning(
                "attach_pocket_to_session_doc failed after specialist run",
                exc_info=True,
            )

    try:
        push_sse_event(
            "pocket_created",
            {
                "pocket_id": pocket_id,
                "pocket": pocket,
                "action": payload.get("action"),
                "session_id": ctx.session_id,
            },
        )
    except Exception:
        logger.debug("push_sse_event(pocket_created) failed", exc_info=True)

    # Re-emit the realtime ``pocket.created`` / ``pocket.updated`` event from
    # the parent process so every connected client sees the new pocket
    # without a manual refresh.
    try:
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud._core.realtime.emit import emit
        from pocketpaw_ee.cloud._core.realtime.events import PocketCreated, PocketUpdated
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
        from pocketpaw_ee.cloud.pockets.service import _pocket_event_payload

        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
        if doc is not None:
            event_payload = await _pocket_event_payload(doc)
            event_cls = PocketUpdated if payload.get("action") == "extended" else PocketCreated
            await emit(event_cls(data=event_payload))
    except Exception:
        logger.debug(
            "realtime re-emit of pocket %s after specialist run failed",
            pocket_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# First-turn auto-titling
# ---------------------------------------------------------------------------


_DEFAULT_TITLES = ("", "New Chat", "Chat")
_TITLE_PLACEHOLDER_LIMIT = 60


def _truncate_for_title(message: str) -> str:
    """One-line, ~tweet-sized preview of the user's first message."""
    raw = (message or "").strip().replace("\n", " ").replace("\r", " ")
    one_line = " ".join(raw.split())
    if len(one_line) > _TITLE_PLACEHOLDER_LIMIT:
        return one_line[:_TITLE_PLACEHOLDER_LIMIT].rstrip() + "…"
    return one_line


async def _set_session_title_in_mongo(session_id: str, title: str) -> bool:
    """Persist a title via :func:`sessions.service.set_title`."""
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    return await sessions_service.set_title(session_id, title)


async def _generate_session_title(ctx: ScopeContext, first_message: str) -> None:
    """Set placeholder, then upgrade to Haiku-generated title in the background."""
    if not ctx.session_id:
        return

    placeholder = _truncate_for_title(first_message)
    if placeholder:
        if await _set_session_title_in_mongo(ctx.session_id, placeholder):
            push_sse_event(
                "session_titled",
                {"session_id": ctx.session_id, "title": placeholder},
            )

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
        logger.warning("cloud Haiku title generation failed for %s", ctx.session_id, exc_info=True)
        return

    if not title or title == placeholder:
        return

    if await _set_session_title_in_mongo(ctx.session_id, title):
        push_sse_event(
            "session_titled",
            {"session_id": ctx.session_id, "title": title},
        )


# ---------------------------------------------------------------------------
# Run-status seam (thin wrapper so tests can patch ``_mark_running`` cheaply)
# ---------------------------------------------------------------------------


async def _mark_running(run_id: str) -> None:
    await run_service.mark_running(run_id)


# ---------------------------------------------------------------------------
# Persist + broadcast tail
# ---------------------------------------------------------------------------


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _extract_ripple_attachment(full_text: str) -> tuple[str, dict[str, Any] | None]:
    """Strip the trailing ``ripple`` JSON block from ``full_text`` and return
    ``(remaining_text, normalized_spec_or_None)``.

    Mirrors the regex-based extraction used by ``agent_bridge`` so the cloud
    SSE path produces identical attachments to the OSS bus path.
    """
    match = RIPPLE_JSON_RE.search(full_text)
    if not match:
        return full_text, None
    try:
        candidate = json.loads(match.group(1))
    except Exception:
        logger.debug("Ripple parse failed", exc_info=True)
        return full_text, None
    if not (isinstance(candidate, dict) and ("lifecycle" in candidate or "widgets" in candidate)):
        return full_text, None
    spec: dict[str, Any] = candidate
    try:
        from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

        normalized = normalize_ripple_spec(candidate)
        if normalized:
            spec = normalized
    except Exception:
        logger.debug("Ripple normalize failed", exc_info=True)
    remaining = (full_text[: match.start()] + full_text[match.end() :]).strip()
    return remaining, spec


async def _persist_and_complete(
    spec: RunSpec,
    ctx: ScopeContext,
    full_text: str,
    attachments: list[dict[str, Any]],
) -> str:
    """Persist the assistant message, mark the run completed, broadcast.

    Returns the persisted assistant message id (string ``ObjectId``).
    Side-effects: appends ``message_service`` write, ``run_service.mark_completed``,
    WS broadcast, best-effort ``pool.observe`` for soul.
    """
    msg = await _persist_assistant_message(ctx, full_text, attachments)
    assistant_id = str(msg.id)
    await run_service.mark_completed(
        spec.run_id,
        assistant_message_id=assistant_id,
        partial_text=full_text,
    )
    await _broadcast_message_new(
        ctx, assistant_id, full_text, attachments, created_at=msg.createdAt
    )

    # Best-effort per-agent soul observation. Failure must not block the run.
    try:
        pool = get_agent_pool()
        await pool.observe(ctx.target_agent_id, spec.content, full_text)
    except Exception:
        logger.warning(
            "pool.observe failed for agent %s — per-agent soul not updated",
            ctx.target_agent_id,
            exc_info=True,
        )
    return assistant_id


# ---------------------------------------------------------------------------
# Agent-pool driver
# ---------------------------------------------------------------------------


async def _drive_agent_loop(
    ctx: ScopeContext,
    *,
    user_content: str,
    attachments_in: list[dict[str, Any]] | None,
    mentions_in: list[Any] | None,
    history: list[dict[str, str]] | None,
    is_cancelled: Any,
    emit_stream_start: bool,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Drive ``AgentPool.run`` and yield ``(event_name, event_data)`` tuples.

    ``is_cancelled`` is an awaitable nullary callable returning ``bool``.
    """
    pool = get_agent_pool()
    try:
        instance = await pool.get(ctx.target_agent_id)
    except Exception as e:
        logger.exception("Failed to load agent instance %s", ctx.target_agent_id)
        yield ("error", {"code": "agent.load_failed", "message": str(e)})
        return

    knowledge_context = await build_knowledge_context(
        ctx,
        user_message=user_content,
        attachments=attachments_in,
        mentions=mentions_in,
    )
    backend_name = (
        instance.config.get("backend", "claude_agent_sdk") if hasattr(instance, "config") else None
    )
    behavior_instructions = build_behavior_instructions(ctx, backend_name=backend_name)

    if emit_stream_start:
        stream_start_payload: dict[str, Any] = {
            "run_id": _new_run_id(),
            "agent_id": ctx.target_agent_id,
            "agent_name": getattr(instance, "agent_name", ""),
            "scope": ctx.kind.value,
            "scope_id": ctx.scope_id,
        }
        if ctx.session_id:
            stream_start_payload["session_id"] = ctx.session_id
        yield ("stream_start", stream_start_payload)

    side_channel_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    sink_token = attach_sse_event_sink(side_channel_queue)
    session_mongo_id = ctx.scope_id if ctx.kind is ScopeKind.SESSION else None
    identity_tokens = attach_agent_identity(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        session_mongo_id=session_mongo_id,
    )

    if not history and ctx.session_id:
        asyncio.create_task(_generate_session_title(ctx, user_content))

    def _drain_side_channel() -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        while True:
            try:
                events.append(side_channel_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    handled_pocket_ids: set[str] = set()
    try:
        session_key = session_key_for(ctx)
        agent_iter = pool.run(
            ctx.target_agent_id,
            user_content,
            session_key,
            history=history,
            knowledge_context=knowledge_context,
            instructions=behavior_instructions,
        ).__aiter__()
        next_event_task: asyncio.Task[Any] | None = asyncio.create_task(agent_iter.__anext__())
        next_queue_task: asyncio.Task[tuple[str, dict[str, Any]]] = asyncio.create_task(
            side_channel_queue.get()
        )
        while True:
            if await is_cancelled():
                break
            wait_set: set[asyncio.Task[Any]] = {next_queue_task}
            if next_event_task is not None:
                wait_set.add(next_event_task)
            done, _pending = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            if next_queue_task in done:
                yield next_queue_task.result()
                for ev in _drain_side_channel():
                    yield ev
                next_queue_task = asyncio.create_task(side_channel_queue.get())
            if next_event_task is None or next_event_task not in done:
                continue
            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                next_event_task = None
                next_queue_task.cancel()
                break
            next_event_task = asyncio.create_task(agent_iter.__anext__())
            etype = getattr(event, "type", None)
            econtent = getattr(event, "content", "")
            if etype == "message":
                yield (
                    "chunk",
                    {
                        "content": econtent if isinstance(econtent, str) else "",
                        "type": "text",
                    },
                )
            elif etype == "thinking":
                yield ("thinking", {"content": econtent if isinstance(econtent, str) else ""})
            elif etype == "tool_use":
                meta = getattr(event, "metadata", None) or {}
                name = ""
                tool_input: Any = {}
                if isinstance(meta, dict):
                    name = meta.get("name") or meta.get("tool") or ""
                    tool_input = meta.get("input") or {}
                if not name:
                    if isinstance(econtent, dict):
                        name = econtent.get("tool") or econtent.get("name") or ""
                        tool_input = econtent
                    elif isinstance(econtent, str):
                        name = econtent
                yield ("tool_start", {"tool": name, "input": tool_input})
            elif etype == "tool_result":
                meta = getattr(event, "metadata", None) or {}
                name = ""
                output: Any = econtent
                if isinstance(meta, dict):
                    name = meta.get("name") or meta.get("tool") or ""
                if not name and isinstance(econtent, dict):
                    name = econtent.get("tool") or econtent.get("name") or ""
                if isinstance(econtent, dict):
                    output = econtent.get("result", econtent)
                await _maybe_handle_specialist_response(
                    ctx=ctx,
                    session_mongo_id=session_mongo_id,
                    output=output,
                    handled_pocket_ids=handled_pocket_ids,
                )
                yield ("tool_result", {"tool": name, "output": output})
            elif etype == "done":
                break
        for ev in _drain_side_channel():
            yield ev
    finally:
        try:
            detach_sse_event_sink(sink_token)
        except Exception:
            pass
        try:
            detach_agent_identity(identity_tokens)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _iter_agent_events — used by ``execute_run`` (no transport calls inside)
# ---------------------------------------------------------------------------


async def _iter_agent_events(
    spec: RunSpec, ctx: ScopeContext
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, event_data)`` tuples for the executor.

    The transport is purposefully NOT touched in here — ``execute_run`` is
    the single place that writes to the transport so the seam stays clean.
    The cancel check is handled by ``execute_run`` *between* events.
    """

    async def _never_cancelled() -> bool:
        return False

    async for ev in _drive_agent_loop(
        ctx,
        user_content=spec.content,
        attachments_in=list(spec.attachments) if spec.attachments else None,
        mentions_in=list(spec.mentions) if spec.mentions else None,
        history=list(spec.history),
        is_cancelled=_never_cancelled,
        emit_stream_start=True,
    ):
        yield ev


# ---------------------------------------------------------------------------
# execute_run — the executor-facing entry point
# ---------------------------------------------------------------------------


async def execute_run(spec: RunSpec) -> None:
    """Run the agent for ``spec`` and write every event to the transport.

    Status transitions: ``queued → running → completed``, or the terminal
    ``cancelled`` / ``failed`` paths. A stream that produced no text is
    treated like ``cancelled`` for persistence purposes (no assistant
    message is created) but the run is still marked completed.
    """
    transport = get_stream_transport()
    ctx = await resolve_scope_context(
        scope=spec.context_type,
        scope_id=spec.scope_id,
        user_id=spec.user_id,
        agent_id_hint=spec.agent_id,
    )
    ctx.intent = spec.intent

    await _mark_running(spec.run_id)
    await _broadcast_agent_typing(ctx, active=True)

    full_text = ""
    cancelled = False
    error: Exception | None = None
    try:
        async for event_name, event_data in _iter_agent_events(spec, ctx):
            if await transport.is_cancelled(spec.run_id):
                cancelled = True
                break
            if event_name == "chunk":
                content = event_data.get("content", "")
                if isinstance(content, str):
                    full_text += content
            await transport.append_event(spec.run_id, event_name, event_data)
    except Exception as exc:
        error = exc
        logger.exception("execute_run %s crashed", spec.run_id)
        await transport.append_event(
            spec.run_id,
            "error",
            {"code": "agent.run_failed", "message": str(exc)},
        )

    # Always drop the typing indicator; do it before the persist/broadcast
    # tail so a slow Mongo write doesn't leave the indicator stuck on.
    try:
        await _broadcast_agent_typing(ctx, active=False)
    except Exception:
        logger.debug("agent.typing(active=False) broadcast failed", exc_info=True)

    if error is not None:
        try:
            await run_service.mark_terminal(
                spec.run_id,
                status="failed",
                partial_text=full_text,
                error=str(error),
            )
        except Exception:
            logger.exception("mark_terminal(failed) failed for %s", spec.run_id)
        await transport.set_ttl(spec.run_id, _stream_ttl())
        return

    # Cancelled OR empty stream — no assistant message; no broadcast.
    if cancelled or not full_text.strip():
        if cancelled:
            try:
                await run_service.mark_terminal(
                    spec.run_id,
                    status="cancelled",
                    partial_text=full_text,
                )
            except Exception:
                logger.exception("mark_terminal(cancelled) failed for %s", spec.run_id)
        await transport.append_event(
            spec.run_id,
            "stream_end",
            {"assistant_message_id": None, "usage": {}, "cancelled": cancelled},
        )
        await transport.set_ttl(spec.run_id, _stream_ttl())
        return

    # Strip ripple attachment from the tail of the text before persistence.
    remaining_text, ripple_spec = _extract_ripple_attachment(full_text)
    attachments: list[dict[str, Any]] = []
    if ripple_spec is not None:
        attachments.append({"type": "ripple", "meta": ripple_spec})
        full_text = remaining_text
        await transport.append_event(spec.run_id, "ripple", {"spec": ripple_spec})

    assistant_id = await _persist_and_complete(spec, ctx, full_text, attachments)
    await transport.append_event(
        spec.run_id,
        "stream_end",
        {"assistant_message_id": assistant_id, "usage": {}, "cancelled": False},
    )
    await transport.set_ttl(spec.run_id, _stream_ttl())
