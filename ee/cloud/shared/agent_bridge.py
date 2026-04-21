"""Bridge between cloud chat events and the PocketPaw agent pool.

Changes:
- 2026-04-19: Forward ``Message.attachments`` through ``on_message_for_agents``
  and ``_run_agent_response`` so channel agents see filename/mime/size context.
  Matches the DM path's shape (``Attached files:`` block appended to the user
  prompt before ``pool.run``); keeps ``pool.run``'s signature unchanged.
- Replaced inline pocket creation with PocketService.create_from_ripple_spec()
  to reduce coupling. Pocket creation logic now lives in the pockets domain.

Responsibilities (focused orchestrator):
1. Checks each agent's respond_mode (silent, auto, mention_only, smart)
2. Triggers agents that should respond and streams responses via WebSocket
3. Parses ripple specs from agent responses (understanding the response)
4. Delegates pocket creation to PocketService
5. Persists agent messages to MongoDB
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    AgentStreamChunk,
    AgentStreamEnd,
    AgentStreamStart,
    AgentToolUse,
)
from ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


_background_tasks: set[asyncio.Task] = set()


async def on_message_for_agents(data: dict) -> None:
    """Handle message.sent event — check if any agents should respond.

    Dispatches all the actual work (respond-mode checks, smart-mode LLM
    calls, ``_run_agent_response``) to a background task so the ``event_bus``
    emitter — which is awaited on the group-chat send path — returns
    immediately. Before this, a ``smart``-mode agent blocked the
    ``MessageSent`` realtime broadcast for the full duration of a Haiku
    relevance-check call (5–10s), making the sender wait seconds to see
    their own message ack.
    """
    group_id = data.get("group_id")
    sender_type = data.get("sender_type", "user")
    content = data.get("content", "")

    if not group_id or not content:
        return
    if sender_type == "agent":
        # Don't respond to agent messages (prevent loops)
        return

    task = asyncio.create_task(_dispatch_agent_responses(data))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _dispatch_agent_responses(data: dict) -> None:
    """Background worker for ``on_message_for_agents`` — does the actual
    respond-mode evaluation and per-agent response spawning. Runs
    detached from the emitter's await chain."""
    group_id = data.get("group_id")
    sender_id = data.get("sender_id")
    content = data.get("content", "")
    mentions = data.get("mentions", [])
    workspace_id = data.get("workspace_id", "")
    # Attachments ride the ``message.sent`` payload so channel agents see the
    # same filename/mime/size context DM agents already get. Defaults to ``[]``
    # so emit sites that don't populate it (legacy or test) stay compatible.
    attachments = data.get("attachments", []) or []
    # Reply metadata — used to skip ``auto``-mode agents when the message is
    # a directed reply to another human (or to a different agent). Absent
    # for top-level messages.
    reply_to = data.get("reply_to")
    reply_to_sender_type = data.get("reply_to_sender_type")
    reply_to_agent_id = data.get("reply_to_agent_id")

    logger.info("Agent bridge: message in group %s from %s: %s", group_id, sender_id, content[:50])

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    try:
        group = await Group.get(PydanticObjectId(group_id))
    except Exception:
        logger.error("Agent bridge: failed to load group %s", group_id, exc_info=True)
        return
    if not group or not group.agents:
        logger.info("Agent bridge: group %s has no agents", group_id)
        return

    logger.info(
        "Agent bridge: group has %d agents: %s",
        len(group.agents),
        [(a.agent, a.respond_mode) for a in group.agents],
    )

    for group_agent in group.agents:
        should = await _should_agent_respond(
            group_agent,
            content,
            mentions,
            reply_to=reply_to,
            reply_to_sender_type=reply_to_sender_type,
            reply_to_agent_id=reply_to_agent_id,
        )
        logger.info(
            "Agent bridge: agent %s respond_mode=%s should_respond=%s",
            group_agent.agent,
            group_agent.respond_mode,
            should,
        )
        if should:
            asyncio.create_task(
                _run_agent_response(
                    agent_id=group_agent.agent,
                    group_id=group_id,
                    workspace_id=workspace_id,
                    user_message=content,
                    group_members=group.members,
                    attachments=attachments,
                )
            )


async def _should_agent_respond(
    group_agent: Any,
    content: str,
    mentions: list,
    *,
    reply_to: str | None = None,
    reply_to_sender_type: str | None = None,
    reply_to_agent_id: str | None = None,
) -> bool:
    """Determine if an agent should respond.

    Precedence:
    1. ``silent`` always opts out — even when explicitly mentioned.
    2. If the message contains *any* agent-typed mention, only the mentioned
       agents respond. Non-mentioned agents — even those on ``auto`` — stay
       quiet so multiple auto agents don't all answer when the user is
       clearly addressing one.
    3. If the message is a reply aimed at someone else, no agent chimes in
       unless it was specifically mentioned (handled in step 2):
       - Reply to a human → nobody auto-responds; the message is a directed
         side-conversation between two users.
       - Reply to a *different* agent → that agent's turn, not ours.
       - Reply to *this* agent → fall through to the normal mode checks so
         a follow-up actually gets answered.
    4. With no agent mentions and no directed reply, fall back to per-agent
       mode:
       - ``auto`` → respond
       - ``mention_only`` → don't respond
       - ``smart`` → ask a cheap LLM whether this agent is relevant
    """
    mode = group_agent.respond_mode

    if mode == "silent":
        return False

    agent_mentions = [m for m in mentions if m.get("type") == "agent"]
    if agent_mentions:
        return any(m.get("id") == group_agent.agent for m in agent_mentions)

    # Directed reply handling — see docstring step 3.
    if reply_to:
        if reply_to_sender_type == "user":
            return False
        if reply_to_sender_type == "agent" and reply_to_agent_id != group_agent.agent:
            return False

    if mode == "auto":
        return True
    if mode == "mention_only":
        return False
    if mode == "smart":
        return await _smart_relevance_check(group_agent.agent, content)
    return False


async def _smart_relevance_check(agent_id: str, content: str) -> bool:
    """Use a cheap LLM call to check if the message is relevant to the agent."""
    from beanie import PydanticObjectId

    from ee.cloud.models.agent import Agent

    try:
        agent = await Agent.get(PydanticObjectId(agent_id))
        if not agent:
            return False

        persona = agent.config.soul_persona or agent.config.system_prompt or agent.name

        from pocketpaw.agents.registry import get_backend_class
        from pocketpaw.config import Settings

        settings = Settings.load()
        settings.agent_backend = "claude_agent_sdk"
        settings.claude_sdk_model = "claude-haiku-4-5-20251001"

        backend_cls = get_backend_class("claude_agent_sdk")
        if not backend_cls:
            return False
        backend = backend_cls(settings)

        prompt = (
            f"You are deciding if an AI agent should respond to a message.\n"
            f"Agent persona: {persona[:200]}\n"
            f"Message: {content[:500]}\n"
            f"Should this agent respond? Reply only YES or NO."
        )

        result = ""
        async for event in backend.run(prompt, system_prompt="Reply only YES or NO."):
            if event.type == "message":
                result += event.content
            if event.type == "done":
                break
        await backend.stop()

        return result.strip().upper().startswith("YES")
    except Exception:
        logger.debug("Smart relevance check failed for agent %s", agent_id)
        return False


def _format_bytes(n: int | None) -> str:
    """Compact human-readable byte size (``12.3 KB``).

    Mirrors the helper used on the DM path so channel prompts render file
    sizes the same way. ``None`` / unknown returns an empty string.
    """
    if not isinstance(n, int) or n < 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _augment_message_with_attachments(
    content: str, attachments: list[dict] | None
) -> str:
    """Append an ``Attached files`` block to ``content`` so agents see context.

    Attachment dicts come off the ``message.sent`` event payload. Each entry
    is permissive: ``name``, ``url``, ``meta.mime``, ``meta.size``. Missing
    fields degrade gracefully — an attachment with only a name still shows up,
    which is better than silently dropping it like the pre-fix behavior did.
    """
    if not attachments:
        return content
    lines: list[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        name = att.get("name") or "file"
        meta = att.get("meta") or {}
        mime = meta.get("mime") or att.get("type") or "application/octet-stream"
        size_str = _format_bytes(meta.get("size"))
        url = att.get("url", "")
        meta_suffix = f", {size_str}" if size_str else ""
        location = f" at {url}" if url else ""
        lines.append(f"- {name} ({mime}{meta_suffix}){location}")
    if not lines:
        return content
    return f"{content}\n\nAttached files:\n" + "\n".join(lines)


async def _run_agent_response(
    agent_id: str,
    group_id: str,
    workspace_id: str,
    user_message: str,
    group_members: list[str],
    attachments: list[dict] | None = None,
) -> None:
    """Run an agent's response and stream it to the group.

    ``attachments`` carries the triggering user message's files (shape matches
    ``ee.cloud.models.message.Attachment``: ``type``, ``url``, ``name``,
    ``meta``). They're formatted into ``user_message`` before ``pool.run`` so
    the agent sees filename/mime/size the same way it does on the DM path.
    """
    from ee.cloud.models.message import Attachment, Message
    from pocketpaw.agents.pool import get_agent_pool

    pool = get_agent_pool()
    session_key = f"cloud:{group_id}:{agent_id}"

    # Match the DM path's shape (``src/pocketpaw/agents/loop.py``) — append an
    # "Attached files" block to the prompt so agents can reason about channel
    # uploads. Kept inline so ``pool.run``'s signature stays untouched.
    user_message = _augment_message_with_attachments(user_message, attachments)

    logger.info("Agent bridge: running response for agent %s in group %s", agent_id, group_id)

    try:
        instance = await pool.get(agent_id)
    except Exception:
        logger.error("Failed to get agent instance %s", agent_id, exc_info=True)
        return

    # Fetch recent conversation history from cloud Messages
    recent_msgs = (
        await Message.find(
            Message.group == group_id,
            Message.deleted == False,  # noqa: E712
        )
        .sort(-Message.createdAt)
        .limit(20)
        .to_list()
    )
    recent_msgs.reverse()  # oldest first

    history = []
    for m in recent_msgs:
        role = "assistant" if m.sender_type == "agent" else "user"
        history.append({"role": role, "content": m.content})

    # Inject knowledge context from agent's knowledge engine
    knowledge_context = ""
    try:
        from ee.cloud.agents.knowledge import KnowledgeService

        knowledge_context = await KnowledgeService.search_context(agent_id, user_message)
        if knowledge_context:
            logger.info(
                "Agent bridge: injected %d chars of knowledge for agent %s",
                len(knowledge_context),
                agent_id,
            )
    except Exception:
        logger.warning("Knowledge search failed for agent %s", agent_id, exc_info=True)

    # Notify: agent starts generating
    temp_msg_id = f"agent-stream-{agent_id}-{int(datetime.now(UTC).timestamp() * 1000)}"
    await emit(
        AgentStreamStart(
            data={
                "group_id": group_id,
                "agent_id": agent_id,
                "agent_name": instance.agent_name,
                "message_id": temp_msg_id,
            },
        )
    )

    # Stream response — throttle chunk emits so WS bandwidth doesn't grow
    # O(n²) with response length. stream_end delivers the authoritative final
    # text, so a coalesced chunk is a lossless UX compromise.
    full_text = ""
    last_emit_ts = 0.0
    STREAM_CHUNK_THROTTLE_S = 0.2
    try:
        async for event in pool.run(
            agent_id, user_message, session_key, history, knowledge_context=knowledge_context
        ):
            if event.type == "message":
                full_text += event.content
                now = asyncio.get_event_loop().time()
                if now - last_emit_ts >= STREAM_CHUNK_THROTTLE_S:
                    last_emit_ts = now
                    await emit(
                        AgentStreamChunk(
                            data={
                                "group_id": group_id,
                                "agent_id": agent_id,
                                "message_id": temp_msg_id,
                                "content": full_text,
                            },
                        )
                    )
            elif event.type == "tool_use":
                # Notify clients which tool the agent is using
                tool_name = ""
                if isinstance(event.content, dict):
                    tool_name = event.content.get("tool") or event.content.get("name") or ""
                elif isinstance(event.content, str):
                    tool_name = event.content
                await emit(
                    AgentToolUse(
                        data={
                            "group_id": group_id,
                            "agent_id": agent_id,
                            "agent_name": instance.agent_name,
                            "tool": tool_name,
                        },
                    )
                )
            elif event.type == "thinking":
                await emit(
                    AgentToolUse(
                        data={
                            "group_id": group_id,
                            "agent_id": agent_id,
                            "agent_name": instance.agent_name,
                            "tool": "thinking",
                        },
                    )
                )
            elif event.type == "done":
                break
    except Exception:
        logger.exception("Agent %s response failed in group %s", agent_id, group_id)
        full_text = full_text or "[Agent response failed]"

    if not full_text.strip():
        return

    # Check for ripple spec in response
    attachments: list[Attachment] = []
    ripple_spec = None
    try:
        json_match = re.search(r"```json\s*(\{.*?\})\s*```", full_text, re.DOTALL)
        if json_match:
            candidate = json.loads(json_match.group(1))
            if "lifecycle" in candidate or "widgets" in candidate:
                from ee.cloud.ripple_normalizer import normalize_ripple_spec

                ripple_spec = normalize_ripple_spec(candidate)
                attachments.append(Attachment(type="ripple", meta=ripple_spec))
                full_text = full_text[: json_match.start()] + full_text[json_match.end() :]
                full_text = full_text.strip()
    except Exception:
        pass

    # Auto-create pocket from ripple spec (delegated to PocketService)
    pocket_id = None
    if ripple_spec:
        from ee.cloud.pockets.service import PocketService

        pocket_id = await PocketService.create_from_ripple_spec(
            workspace_id=workspace_id,
            owner_id=group_members[0] if group_members else "",
            ripple_spec=ripple_spec,
            description=f"Generated by {instance.agent_name}",
        )

    # Persist agent message to MongoDB
    msg = Message(
        group=group_id,
        sender=None,
        sender_type="agent",
        agent=agent_id,
        content=full_text,
        attachments=attachments,
    )
    await msg.insert()

    # Broadcast final message. ``temp_message_id`` is echoed from the
    # matching ``stream_start`` so the FE can precisely replace the
    # streaming placeholder — without it, a group with two agents
    # responding concurrently would race on a startsWith('agent-stream-')
    # lookup and finalize the wrong row.
    await emit(
        AgentStreamEnd(
            data={
                "group_id": group_id,
                "agent_id": agent_id,
                "message_id": str(msg.id),
                "temp_message_id": temp_msg_id,
                "content": full_text,
                "ripple_spec": ripple_spec,
                "pocket_id": pocket_id,
                "agent_name": instance.agent_name,
            },
        )
    )

    # Observe with soul
    await pool.observe(agent_id, user_message, full_text)

    logger.info(
        "Agent %s responded in group %s (%d chars)",
        instance.agent_name,
        group_id,
        len(full_text),
    )


def register_agent_bridge() -> None:
    """Register the agent bridge event handler."""
    event_bus.subscribe("message.sent", on_message_for_agents)
    logger.info("Agent bridge registered")
