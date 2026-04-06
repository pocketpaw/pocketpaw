# A2A Protocol — Server implementation for PocketPaw.
#
# Phase 1: Exposes PocketPaw as an A2A-compatible agent.
#
# Endpoints:
#   GET  /.well-known/agent.json       — Agent Card (capability manifest)
#   GET  /.well-known/agent-card.json  — Alias (spec-correct path)
#   POST /a2a                          — JSON-RPC 2.0 dispatcher
#   POST /a2a/tasks/send               — Submit a task (returns task JSON)
#   POST /a2a/tasks/send/stream        — Submit a task; SSE response stream
#   GET  /a2a/tasks/{task_id}          — Poll task status
#   POST /a2a/tasks/{task_id}/cancel   — Cancel an in-flight task
#
# All task processing is routed through the existing PocketPaw AgentLoop
# via the internal message bus (same path as REST /api/v1/chat).

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
import uuid
from datetime import UTC, datetime
from importlib.metadata import version as _pkg_version
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pocketpaw.a2a.errors import (
    TASK_NOT_CANCELABLE,
    TASK_NOT_FOUND,
    TASK_NOT_MODIFIABLE,
    UNSUPPORTED_OPERATION,
    JSONRPCError,
    json_rpc_success_response,
)
from pocketpaw.a2a.jsonrpc import A2ADispatcher
from pocketpaw.a2a.models import (
    A2AMessage,
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    Task,
    TaskArtifactUpdateEvent,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    validate_transition,
)
from pocketpaw.api.deps import require_scope
from pocketpaw.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory task store (sufficient for single-process; Phase 3 may persist)
# ---------------------------------------------------------------------------
_MAX_TASKS = 1000
_TASK_TTL_SECONDS = 3600  # Expire terminal tasks after 1 hour
_tasks: dict[str, Task] = {}
_task_timestamps: dict[str, float] = {}  # task_id -> creation monotonic time
_cancel_events: dict[str, asyncio.Event] = {}  # task_id -> cancellation flag

# Task ID format: alphanumeric, hyphens, underscores, dots (1-128 chars)
_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9._\-]{1,128}$")

# ---------------------------------------------------------------------------
# Agent card cache (avoids rebuilding skill list from tool registry each time)
# ---------------------------------------------------------------------------
_agent_card_cache: dict[str, tuple[float, dict]] = {}
_CARD_CACHE_TTL = 30.0


def _get_agent_card_cached(base_url: str) -> dict:
    """Return cached agent card dict, rebuilding if stale."""
    now = _time.monotonic()
    cached = _agent_card_cache.get(base_url)
    if cached and (now - cached[0]) < _CARD_CACHE_TTL:
        return cached[1]
    card = _build_agent_card(base_url)
    card_dict = card.model_dump(mode="json")
    _agent_card_cache[base_url] = (now, card_dict)
    return card_dict


def _validate_task_id(task_id: str) -> None:
    """Validate that a task ID is a safe, well-formed identifier."""
    if not _TASK_ID_RE.match(task_id):
        raise JSONRPCError(
            -32602,
            "Invalid task ID format: must be 1-128 alphanumeric, hyphen, underscore, or dot chars",
        )


def _prune_expired_tasks() -> None:
    """Remove terminal tasks that have exceeded their TTL."""
    now = _time.monotonic()
    terminal = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}
    expired = [
        tid
        for tid, t in _tasks.items()
        if t.status.state in terminal and (now - _task_timestamps.get(tid, now)) > _TASK_TTL_SECONDS
    ]
    for tid in expired:
        _tasks.pop(tid, None)
        _task_timestamps.pop(tid, None)
        _cancel_events.pop(tid, None)


def _store_task(task: Task) -> None:
    """Store a task and prune old tasks to prevent memory leaks."""
    _prune_expired_tasks()
    if len(_tasks) >= _MAX_TASKS:
        terminal = {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
            TaskState.REJECTED,
        }
        evict_id = next(
            (tid for tid, t in _tasks.items() if t.status.state in terminal),
            next(iter(_tasks)),  # fallback: evict oldest if all are active
        )
        _tasks.pop(evict_id, None)
        _task_timestamps.pop(evict_id, None)
        _cancel_events.pop(evict_id, None)
    _tasks[task.id] = task
    _task_timestamps[task.id] = _time.monotonic()


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


def _check_a2a_enabled() -> None:
    if not get_settings().a2a_enabled:
        raise HTTPException(status_code=403, detail="A2A protocol is disabled on this agent.")


# The agent-card route lives at the well-known path (outside /api prefix)
well_known_router = APIRouter(
    tags=["A2A"], dependencies=[Depends(_check_a2a_enabled), Depends(require_scope("chat"))]
)

# Task endpoints live under /a2a
tasks_router = APIRouter(
    prefix="/a2a/tasks",
    tags=["A2A"],
    dependencies=[Depends(_check_a2a_enabled), Depends(require_scope("chat"))],
)

# JSON-RPC 2.0 endpoint
jsonrpc_router = APIRouter(
    tags=["A2A"],
    dependencies=[Depends(_check_a2a_enabled), Depends(require_scope("chat"))],
)


# ---------------------------------------------------------------------------
# Agent Card helpers
# ---------------------------------------------------------------------------


def _build_agent_card(base_url: str) -> AgentCard:
    """Build an A2A-compliant Agent Card from current PocketPaw config."""
    settings = get_settings()

    # Try to populate skills from ToolRegistry
    skills: list[AgentSkill] = []
    try:
        from pocketpaw.tools.registry import ToolRegistry

        registry = ToolRegistry()
        for defn in registry.get_definitions(format="openai"):
            fn = defn.get("function", {})
            skills.append(
                AgentSkill(
                    id=fn.get("name", "unknown"),
                    name=fn.get("name", "unknown"),
                    description=fn.get("description", ""),
                    input_modes=["text/plain"],
                    output_modes=["text/plain"],
                )
            )
    except Exception:
        logger.warning("Failed to load tools for A2A agent card", exc_info=True)

    # Fallback if no tools registered
    if not skills:
        skills = [
            AgentSkill(
                id="general-assistant",
                name="General Assistant",
                description=(
                    "Answer questions, run shell commands, search the web, manage files, "
                    "read/send email, control Spotify, and more."
                ),
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ]

    agent_name = getattr(settings, "a2a_agent_name", "PocketPaw")
    agent_desc = getattr(settings, "a2a_agent_description", "") or (
        "Self-hosted, modular AI agent. "
        "Runs locally with 60+ built-in tools across productivity, coding, and research."
    )
    agent_version = getattr(settings, "a2a_agent_version", "") or ""

    if not agent_version:
        try:
            agent_version = _pkg_version("pocketpaw")
        except Exception:
            agent_version = "unknown"

    return AgentCard(
        name=agent_name,
        description=agent_desc,
        url=base_url,
        version=agent_version,
        provider={"organization": "PocketPaw", "url": base_url},
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            state_transition_history=True,
        ),
        skills=skills,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            {
                "url": f"{base_url}/a2a",
                "protocol_binding": "jsonrpc-over-https",
                "protocol_version": "0.2.5",
            }
        ],
    )


# ---------------------------------------------------------------------------
# Internal: bridge A2A task to AgentLoop via message bus
# ---------------------------------------------------------------------------


class _A2ASessionBridge:
    """Bridges the message bus outbound stream to an asyncio Queue for A2A SSE.

    Mirrors the pattern in ``api/v1/chat.py::_APISessionBridge``.
    """

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self._outbound_cb = None
        self._system_cb = None

    async def start(self) -> None:
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import Channel, OutboundMessage, SystemEvent

        bus = get_message_bus()

        async def _on_outbound(msg: OutboundMessage) -> None:
            if msg.chat_id != self.chat_id:
                return
            if msg.is_stream_chunk:
                await self.queue.put({"type": "chunk", "content": msg.content})
            elif msg.is_stream_end:
                await self.queue.put({"type": "stream_end"})
            else:
                await self.queue.put({"type": "chunk", "content": msg.content})

        async def _on_system(evt: SystemEvent) -> None:
            data = evt.data or {}
            sk = data.get("session_key", "")
            if not sk or not sk.endswith(f":{self.chat_id}"):
                return
            if evt.event_type == "error":
                await self.queue.put({"type": "error", "message": data.get("message", "")})

        self._outbound_cb = _on_outbound
        self._system_cb = _on_system
        bus.subscribe_outbound(Channel.A2A, _on_outbound)
        bus.subscribe_system(_on_system)

    async def stop(self) -> None:
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import Channel

        bus = get_message_bus()
        if self._outbound_cb:
            bus.unsubscribe_outbound(Channel.A2A, self._outbound_cb)
        if self._system_cb:
            bus.unsubscribe_system(self._system_cb)


def _extract_message_text(message: A2AMessage) -> str:
    """Extract a text representation from all part types in a message.

    TextPart: use text directly.
    FilePart: include file name and URI/byte indicator.
    DataPart: serialize data as compact JSON.
    """
    segments: list[str] = []
    for part in message.parts:
        if hasattr(part, "text"):
            segments.append(part.text)
        elif hasattr(part, "uri") or hasattr(part, "bytes_data"):
            name = getattr(part, "name", None) or "unnamed_file"
            uri = getattr(part, "uri", None)
            if uri:
                segments.append(f"[File: {name} at {uri}]")
            else:
                segments.append(f"[File: {name} (embedded)]")
        elif hasattr(part, "data"):
            segments.append(json.dumps(part.data, separators=(",", ":")))
    return " ".join(segments)


async def _dispatch_to_agent(task_id: str, message: A2AMessage) -> str:
    """Publish an inbound message onto the bus and return the chat_id."""
    from pocketpaw.bus import get_message_bus
    from pocketpaw.bus.events import Channel, InboundMessage

    # Use task_id as the stable chat_id so session context is maintained
    chat_id = f"a2a:{task_id}"
    text = _extract_message_text(message)

    msg = InboundMessage(
        channel=Channel.A2A,
        sender_id="a2a_client",
        chat_id=chat_id,
        content=text,
        metadata={"source": "a2a_protocol", "task_id": task_id},
    )
    bus = get_message_bus()
    await bus.publish_inbound(msg)
    return chat_id


def _get_task_timeout() -> float:
    """Get the configured task timeout in seconds."""
    settings = get_settings()
    return float(getattr(settings, "a2a_task_timeout", 120))


# ---------------------------------------------------------------------------
# Core business logic (shared by REST endpoints and JSON-RPC dispatcher)
# ---------------------------------------------------------------------------


async def _core_message_send(params: dict[str, Any]) -> dict[str, Any]:
    """Core logic for message/send (non-streaming). Returns task dict."""
    send_params = TaskSendParams(**params)
    task_id = send_params.id
    _validate_task_id(task_id)

    # Reject if task exists in a terminal state (spec: terminal states are immutable)
    existing = _tasks.get(task_id)
    if existing is not None:
        terminal = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}
        if existing.status.state in terminal:
            raise JSONRPCError(
                TASK_NOT_MODIFIABLE,
                f"Task '{task_id}' is in terminal state '{existing.status.state}' "
                "and cannot accept new messages",
            )

    # Validate accepted output modes if specified
    if send_params.configuration and send_params.configuration.accepted_output_modes:
        supported = {"text/plain"}
        requested = set(send_params.configuration.accepted_output_modes)
        if not requested & supported:
            from pocketpaw.a2a.errors import INCOMPATIBLE_OUTPUT_MODES

            raise JSONRPCError(
                INCOMPATIBLE_OUTPUT_MODES,
                f"None of the requested output modes are supported. "
                f"Supported: {sorted(supported)}, requested: {sorted(requested)}",
            )

    chat_id = f"a2a:{task_id}"
    timeout = _get_task_timeout()

    cancel_event = asyncio.Event()
    _cancel_events[task_id] = cancel_event

    task = Task(
        id=task_id,
        context_id=send_params.context_id,
        session_id=send_params.session_id,
        status=TaskStatus(state=TaskState.SUBMITTED),
        history=[send_params.message],
        metadata=send_params.metadata,
    )
    _store_task(task)

    bridge = _A2ASessionBridge(chat_id)
    await bridge.start()

    task.status = TaskStatus(state=TaskState.WORKING)

    try:
        await _dispatch_to_agent(task_id, send_params.message)

        cancel_fut = asyncio.ensure_future(cancel_event.wait())
        content_parts: list[str] = []
        done = False
        while not done:
            get_fut = asyncio.ensure_future(bridge.queue.get())
            try:
                finished, _ = await asyncio.wait(
                    {get_fut, cancel_fut},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:
                logger.exception("Unexpected error waiting for task %s", task_id)
                finished = set()

            if not finished:  # timeout
                get_fut.cancel()
                cancel_fut.cancel()
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message=A2AMessage(
                        role="agent",
                        parts=[TextPart(text=f"Task timed out after {int(timeout)} seconds.")],
                    ),
                )
                return task.model_dump(mode="json")

            if cancel_fut in finished:  # cancellation requested
                get_fut.cancel()
                task.status = TaskStatus(state=TaskState.CANCELED)
                return task.model_dump(mode="json")

            event = get_fut.result()
            if event["type"] == "chunk":
                content_parts.append(event.get("content", ""))
            elif event["type"] == "stream_end":
                done = True
                cancel_fut.cancel()
            elif event["type"] == "error":
                cancel_fut.cancel()
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message=A2AMessage(
                        role="agent",
                        parts=[TextPart(text=event.get("message", "Agent error"))],
                    ),
                )
                return task.model_dump(mode="json")

        # Build completion response with artifact
        full_text = "".join(content_parts)
        agent_reply = A2AMessage(
            role="agent",
            parts=[TextPart(text=full_text)],
        )
        artifact = Artifact(
            name="response",
            description="Agent response",
            parts=[TextPart(text=full_text)],
        )
        task.artifacts.append(artifact)
        task.status = TaskStatus(state=TaskState.COMPLETED, message=agent_reply)
        task.history.append(agent_reply)
        return task.model_dump(mode="json")

    finally:
        await bridge.stop()
        _cancel_events.pop(task_id, None)


async def _core_message_stream(params: dict[str, Any], request_id: int | str | None = None):
    """Core logic for message/stream. Yields JSON-RPC response dicts for SSE."""
    send_params = TaskSendParams(**params)
    task_id = send_params.id
    _validate_task_id(task_id)
    chat_id = f"a2a:{task_id}"
    timeout = _get_task_timeout()

    # Validate accepted output modes if specified
    if send_params.configuration and send_params.configuration.accepted_output_modes:
        supported = {"text/plain"}
        requested = set(send_params.configuration.accepted_output_modes)
        if not requested & supported:
            from pocketpaw.a2a.errors import INCOMPATIBLE_OUTPUT_MODES

            raise JSONRPCError(
                INCOMPATIBLE_OUTPUT_MODES,
                f"None of the requested output modes are supported. "
                f"Supported: {sorted(supported)}, requested: {sorted(requested)}",
            )

    cancel_event = asyncio.Event()
    _cancel_events[task_id] = cancel_event

    task = Task(
        id=task_id,
        context_id=send_params.context_id,
        session_id=send_params.session_id,
        status=TaskStatus(state=TaskState.SUBMITTED),
        history=[send_params.message],
        metadata=send_params.metadata,
    )
    _store_task(task)

    bridge = _A2ASessionBridge(chat_id)
    await bridge.start()
    await _dispatch_to_agent(task_id, send_params.message)

    try:
        # Submitted acknowledgment
        submitted_event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=send_params.context_id,
            status=TaskStatus(state=TaskState.SUBMITTED),
            final=False,
        )
        yield json_rpc_success_response(request_id, submitted_event.model_dump(mode="json"))

        # Working notification
        working_status = TaskStatus(state=TaskState.WORKING)
        _tasks[task_id].status = working_status
        working_event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=send_params.context_id,
            status=working_status,
            final=False,
        )
        yield json_rpc_success_response(request_id, working_event.model_dump(mode="json"))

        # Stream content chunks
        accumulated: list[str] = []
        deadline = _time.monotonic() + timeout
        response_artifact_id = uuid.uuid4().hex
        while not cancel_event.is_set():
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                agent_reply = A2AMessage(
                    role="agent",
                    parts=[TextPart(text=f"Task timed out after {int(timeout)} seconds.")],
                )
                failed_status = TaskStatus(state=TaskState.FAILED, message=agent_reply)
                _tasks[task_id].status = failed_status
                failed_event = TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=send_params.context_id,
                    status=failed_status,
                    final=True,
                )
                yield json_rpc_success_response(request_id, failed_event.model_dump(mode="json"))
                break

            try:
                event = await asyncio.wait_for(bridge.queue.get(), timeout=min(remaining, 1.0))
            except TimeoutError:
                continue

            if event["type"] == "chunk":
                chunk_text = event.get("content", "")
                accumulated.append(chunk_text)
                # Emit artifact update per chunk
                chunk_artifact = Artifact(
                    artifact_id=response_artifact_id,
                    name="response",
                    parts=[TextPart(text=chunk_text)],
                )
                artifact_event = TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id=send_params.context_id,
                    artifact=chunk_artifact,
                    append=True,
                    last_chunk=False,
                )
                yield json_rpc_success_response(request_id, artifact_event.model_dump(mode="json"))

            elif event["type"] == "stream_end":
                full_text = "".join(accumulated)
                # Final artifact event
                final_artifact = Artifact(
                    artifact_id=response_artifact_id,
                    name="response",
                    description="Agent response",
                    parts=[TextPart(text=full_text)],
                )
                _tasks[task_id].artifacts.append(final_artifact)
                final_artifact_event = TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id=send_params.context_id,
                    artifact=final_artifact,
                    append=False,
                    last_chunk=True,
                )
                yield json_rpc_success_response(
                    request_id, final_artifact_event.model_dump(mode="json")
                )

                # Completed status event
                agent_reply = A2AMessage(role="agent", parts=[TextPart(text=full_text)])
                completed_status = TaskStatus(state=TaskState.COMPLETED, message=agent_reply)
                _tasks[task_id].status = completed_status
                _tasks[task_id].history.append(agent_reply)
                completed_event = TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=send_params.context_id,
                    status=completed_status,
                    final=True,
                )
                yield json_rpc_success_response(request_id, completed_event.model_dump(mode="json"))
                break

            elif event["type"] == "error":
                agent_reply = A2AMessage(
                    role="agent",
                    parts=[TextPart(text=event.get("message", "Agent error"))],
                )
                failed_status = TaskStatus(state=TaskState.FAILED, message=agent_reply)
                _tasks[task_id].status = failed_status
                failed_event = TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=send_params.context_id,
                    status=failed_status,
                    final=True,
                )
                yield json_rpc_success_response(request_id, failed_event.model_dump(mode="json"))
                break

    except asyncio.CancelledError:
        pass
    finally:
        await bridge.stop()
        _cancel_events.pop(task_id, None)


async def _core_tasks_get(task_id: str, history_length: int | None = None) -> dict[str, Any]:
    """Core logic for tasks/get. Returns task dict or raises JSONRPCError."""
    task = _tasks.get(task_id)
    if task is None:
        raise JSONRPCError(TASK_NOT_FOUND, f"Task '{task_id}' not found")
    result = task.model_dump(mode="json")
    if history_length is not None:
        if history_length == 0:
            result["history"] = []
        else:
            result["history"] = result["history"][-history_length:]
    return result


async def _core_tasks_cancel(task_id: str) -> dict[str, Any]:
    """Core logic for tasks/cancel. Returns result dict or raises JSONRPCError."""
    task = _tasks.get(task_id)
    if task is None:
        raise JSONRPCError(TASK_NOT_FOUND, f"Task '{task_id}' not found")

    # Validate state transition
    if not validate_transition(task.status.state, TaskState.CANCELED):
        raise JSONRPCError(
            TASK_NOT_CANCELABLE,
            f"Task in state '{task.status.state}' cannot be canceled",
        )

    cancel_event = _cancel_events.get(task_id)
    if cancel_event:
        cancel_event.set()

    task.status = TaskStatus(
        state=TaskState.CANCELED,
        timestamp=datetime.now(tz=UTC),
    )
    return {"id": task_id, "status": "canceled"}


async def _core_tasks_resubscribe(params: dict[str, Any], request_id: int | str | None = None):
    """Core logic for tasks/resubscribe. Yields current state, then live updates if active."""
    task_id = params.get("id") or params.get("task_id")
    if not task_id:
        raise JSONRPCError(-32602, "Missing required parameter: id")

    task = _tasks.get(task_id)
    if task is None:
        raise JSONRPCError(TASK_NOT_FOUND, f"Task '{task_id}' not found")

    terminal = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}

    # Emit current status
    status_event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id=task.context_id,
        status=task.status,
        final=task.status.state in terminal,
    )
    yield json_rpc_success_response(request_id, status_event.model_dump(mode="json"))

    # If already terminal, nothing more to stream
    if task.status.state in terminal:
        return

    # Subscribe to live updates via bridge
    chat_id = f"a2a:{task_id}"
    bridge = _A2ASessionBridge(chat_id)
    await bridge.start()
    timeout = _get_task_timeout()

    try:
        accumulated: list[str] = []
        deadline = _time.monotonic() + timeout
        response_artifact_id = uuid.uuid4().hex
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(bridge.queue.get(), timeout=min(remaining, 1.0))
            except TimeoutError:
                # Check if task was completed by another path
                current = _tasks.get(task_id)
                if current and current.status.state in terminal:
                    final_event = TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=task.context_id,
                        status=current.status,
                        final=True,
                    )
                    yield json_rpc_success_response(request_id, final_event.model_dump(mode="json"))
                    return
                continue

            if event["type"] == "chunk":
                chunk_text = event.get("content", "")
                accumulated.append(chunk_text)
                chunk_artifact = Artifact(
                    artifact_id=response_artifact_id,
                    name="response",
                    parts=[TextPart(text=chunk_text)],
                )
                artifact_event = TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id=task.context_id,
                    artifact=chunk_artifact,
                    append=True,
                    last_chunk=False,
                )
                yield json_rpc_success_response(request_id, artifact_event.model_dump(mode="json"))

            elif event["type"] == "stream_end":
                full_text = "".join(accumulated)
                completed_msg = A2AMessage(role="agent", parts=[TextPart(text=full_text)])
                completed_status = TaskStatus(state=TaskState.COMPLETED, message=completed_msg)
                completed_event = TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=task.context_id,
                    status=completed_status,
                    final=True,
                )
                yield json_rpc_success_response(request_id, completed_event.model_dump(mode="json"))
                return

            elif event["type"] == "error":
                err_msg = A2AMessage(
                    role="agent",
                    parts=[TextPart(text=event.get("message", "Agent error"))],
                )
                err_status = TaskStatus(state=TaskState.FAILED, message=err_msg)
                err_event = TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=task.context_id,
                    status=err_status,
                    final=True,
                )
                yield json_rpc_success_response(request_id, err_event.model_dump(mode="json"))
                return
    finally:
        await bridge.stop()


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 dispatcher setup
# ---------------------------------------------------------------------------

_dispatcher = A2ADispatcher()


async def _jsonrpc_message_send(params: dict[str, Any], request_id: int | str | None):
    return await _core_message_send(params)


async def _jsonrpc_message_stream(params: dict[str, Any], request_id: int | str | None):
    async for event in _core_message_stream(params, request_id):
        yield event


async def _jsonrpc_tasks_get(params: dict[str, Any], request_id: int | str | None):
    task_id = params.get("id") or params.get("task_id")
    if not task_id:
        raise JSONRPCError(-32602, "Missing required parameter: id")
    history_length = params.get("history_length")
    return await _core_tasks_get(task_id, history_length=history_length)


async def _jsonrpc_tasks_cancel(params: dict[str, Any], request_id: int | str | None):
    task_id = params.get("id") or params.get("task_id")
    if not task_id:
        raise JSONRPCError(-32602, "Missing required parameter: id")
    return await _core_tasks_cancel(task_id)


async def _jsonrpc_tasks_resubscribe(params: dict[str, Any], request_id: int | str | None):
    async for event in _core_tasks_resubscribe(params, request_id):
        yield event


async def _jsonrpc_push_notification_set(params: dict[str, Any], request_id: int | str | None):
    raise JSONRPCError(UNSUPPORTED_OPERATION, "Push notifications are not supported by this agent")


async def _jsonrpc_push_notification_get(params: dict[str, Any], request_id: int | str | None):
    raise JSONRPCError(UNSUPPORTED_OPERATION, "Push notifications are not supported by this agent")


_dispatcher.register("message/send", _jsonrpc_message_send)
_dispatcher.register_stream("message/stream", _jsonrpc_message_stream)
_dispatcher.register("tasks/get", _jsonrpc_tasks_get)
_dispatcher.register("tasks/cancel", _jsonrpc_tasks_cancel)
_dispatcher.register_stream("tasks/resubscribe", _jsonrpc_tasks_resubscribe)
_dispatcher.register("tasks/pushNotificationConfig/set", _jsonrpc_push_notification_set)
_dispatcher.register("tasks/pushNotificationConfig/get", _jsonrpc_push_notification_get)


# ---------------------------------------------------------------------------
# Endpoint: Agent Card
# ---------------------------------------------------------------------------


@well_known_router.get("/.well-known/agent.json", response_class=JSONResponse)
async def get_agent_card(request: Request):
    """Return the A2A Agent Card describing PocketPaw's capabilities."""
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(content=_get_agent_card_cached(base_url))


@well_known_router.get("/.well-known/agent-card.json", response_class=JSONResponse)
async def get_agent_card_alias(request: Request):
    """Alias for the spec-correct agent-card.json path."""
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(content=_get_agent_card_cached(base_url))


# ---------------------------------------------------------------------------
# Endpoint: JSON-RPC 2.0 (POST /a2a)
# ---------------------------------------------------------------------------


@jsonrpc_router.post("/a2a")
async def jsonrpc_endpoint(request: Request):
    """JSON-RPC 2.0 endpoint for A2A protocol methods."""
    body = await request.body()

    # Check if this is a streaming request
    try:
        parsed = json.loads(body)
        method = parsed.get("method", "") if isinstance(parsed, dict) else ""
    except (json.JSONDecodeError, ValueError):
        method = ""

    if method in _dispatcher._stream_methods:
        # Stream via SSE
        async def _sse_generator():
            async for event in _dispatcher.dispatch_stream(body):
                yield _format_sse("message", event)

        return StreamingResponse(
            _sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: dispatch and return JSON
    result = await _dispatcher.dispatch(body)
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# Endpoint: tasks/send (non-streaming) — REST wrapper
# ---------------------------------------------------------------------------


@tasks_router.post("/send", response_model=Task)
async def tasks_send(params: TaskSendParams):
    """Submit a task to PocketPaw and wait for the completed response.

    The task is routed through the internal AgentLoop. Completes when the
    agent signals ``stream_end`` or on timeout.
    """
    result = await _core_message_send(params.model_dump(mode="json"))
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# Endpoint: tasks/send/stream (SSE streaming) — REST wrapper
# ---------------------------------------------------------------------------


@tasks_router.post("/send/stream")
async def tasks_send_stream(params: TaskSendParams):
    """Submit a task and receive an SSE stream of state-update events."""
    params_dict = params.model_dump(mode="json")

    async def _event_generator():
        async for event in _core_message_stream(params_dict):
            # Extract status or artifact info for SSE event type
            result = event.get("result", {})
            if "status" in result:
                yield _format_sse("task_status_update", result)
            elif "artifact" in result:
                yield _format_sse("task_artifact_update", result)
            else:
                yield _format_sse("message", result)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Endpoint: tasks/{task_id} (GET — poll) — REST wrapper
# ---------------------------------------------------------------------------


@tasks_router.get("/{task_id}")
async def tasks_get(task_id: str, history_length: int | None = None):
    """Poll the current status of a previously submitted task."""
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    result = task.model_dump(mode="json")
    if history_length is not None:
        if history_length == 0:
            result["history"] = []
        else:
            result["history"] = result["history"][-history_length:]
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# Endpoint: tasks/{task_id}/cancel — REST wrapper
# ---------------------------------------------------------------------------


@tasks_router.post("/{task_id}/cancel")
async def tasks_cancel(task_id: str):
    """Request cancellation of an in-flight task."""
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    # Validate state transition
    if not validate_transition(task.status.state, TaskState.CANCELED):
        raise HTTPException(
            status_code=409,
            detail=f"Task in state '{task.status.state}' cannot be canceled",
        )

    cancel_event = _cancel_events.get(task_id)
    if cancel_event:
        cancel_event.set()

    task.status = TaskStatus(
        state=TaskState.CANCELED,
        timestamp=datetime.now(tz=UTC),
    )
    return {"id": task_id, "status": "canceled"}


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _format_sse(event: str, data: dict) -> str:
    """Format a single SSE message frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Router registration helper
# ---------------------------------------------------------------------------


def register_routes(app) -> None:
    """Mount all A2A routers onto *app*.

    Called during dashboard startup. Keeps dashboard.py free of A2A-specific
    import details and makes it easy for maintainers to see what gets exposed.

    Routes added:
      GET  /.well-known/agent.json       — Agent Card capability manifest
      GET  /.well-known/agent-card.json  — Agent Card alias (spec path)
      POST /a2a                          — JSON-RPC 2.0 dispatcher
      POST /a2a/tasks/send               — Submit task (blocking)
      POST /a2a/tasks/send/stream        — Submit task (SSE streaming)
      GET  /a2a/tasks/{task_id}          — Poll task status
      POST /a2a/tasks/{task_id}/cancel   — Cancel task
    """
    app.include_router(well_known_router)
    app.include_router(tasks_router)
    app.include_router(jsonrpc_router)
