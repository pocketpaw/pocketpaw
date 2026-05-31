"""Bridge between cloud chat events and the PocketPaw agent pool.

Pure cross-domain orchestrator: subscribes to the legacy ``message.sent``
``event_bus`` event and delegates every Beanie touch to the owning
entity service (``chat.group_service`` for group lookup,
``agents.service`` for persona, ``chat.message_service`` for history
rehydration + reply persistence, ``pockets.service`` for ripple-spec
auto-pocket creation).

Responsibilities:
1. Checks each agent's respond_mode (silent, auto, mention_only, smart)
2. Triggers agents that should respond and streams responses via WebSocket
3. Parses ripple specs from agent responses
4. Delegates pocket creation to ``pockets_service.create_from_ripple_spec``
5. Persists agent messages via ``message_service.create_agent_message``

User-message attachments ride the ``message.sent`` payload so channel
agents see the same filename/mime/size context DM agents already get —
appended to the user prompt as an ``Attached files:`` block before
``pool.run`` (matching ``src/pocketpaw/agents/loop.py``'s DM shape).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud.realtime.emit import emit
from pocketpaw_ee.cloud.realtime.events import (
    AgentStreamChunk,
    AgentStreamEnd,
    AgentStreamStart,
    AgentToolUse,
)
from pocketpaw_ee.cloud.shared.events import event_bus

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
    respond-mode evaluation and per-agent response execution. Runs
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

    from pocketpaw_ee.cloud.chat import group_service

    group = await group_service.get_for_dispatch(group_id)
    if group is None or not group.agents:
        logger.info("Agent bridge: group %s missing or has no agents", group_id)
        return

    logger.info(
        "Agent bridge: group has %d agents: %s",
        len(group.agents),
        [(a.agent_id, a.respond_mode) for a in group.agents],
    )

    agents_to_run: list[str] = []
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
            group_agent.agent_id,
            group_agent.respond_mode,
            should,
        )
        if should:
            agents_to_run.append(group_agent.agent_id)

    agents_to_run = _reorder_agent_ids_for_mentions(agents_to_run, mentions)
    responses_by_agent: list[tuple[str, str]] = []
    for idx, agent_id in enumerate(agents_to_run, start=1):
        logger.info(
            "Agent bridge: dispatching agent %s (%d/%d) sequentially",
            agent_id,
            idx,
            len(agents_to_run),
        )
        try:
            response_text = await _run_agent_response(
                agent_id=agent_id,
                group_id=group_id,
                workspace_id=workspace_id,
                user_message=content,
                group_members=group.members,
                attachments=attachments,
                response_label=None,
            )
        except Exception:
            logger.exception(
                "Agent bridge: agent %s failed during sequential dispatch in group %s",
                agent_id,
                group_id,
            )
            continue
        if response_text:
            responses_by_agent.append((agent_id, response_text))

    # Skip the synthesis pass when fewer than 2 agents actually responded.
    # If only one agent succeeded (others raised), having that survivor
    # synthesize its own output produces a redundant "Final response:"
    # duplicate visible to the user.
    if len(agents_to_run) < 2 or len(responses_by_agent) < 2:
        return

    final_agent_id = responses_by_agent[-1][0]
    responded_agent_ids = {agent_id for agent_id, _ in responses_by_agent}
    failed_agent_ids = [
        agent_id for agent_id in agents_to_run if agent_id not in responded_agent_ids
    ]
    logger.info(
        (
            "Agent bridge: generating final collaborative response "
            "with agent %s after %d agent replies"
        ),
        final_agent_id,
        len(responses_by_agent),
    )
    try:
        await _run_agent_response(
            agent_id=final_agent_id,
            group_id=group_id,
            workspace_id=workspace_id,
            user_message=_build_collaboration_final_prompt(
                content, responses_by_agent, failed_agent_ids
            ),
            group_members=group.members,
            attachments=None,
            response_label="Final response:",
        )
    except Exception:
        logger.exception(
            "Agent bridge: final collaborative response failed in group %s (agent %s)",
            group_id,
            final_agent_id,
        )


def _reorder_agent_ids_for_mentions(agent_ids: list[str], mentions: list[dict]) -> list[str]:
    """Dispatch mentioned agents first, preserving mention order from the message."""
    seen: set[str] = set()
    mentioned_order: list[str] = []
    for mention in mentions:
        if not isinstance(mention, dict) or mention.get("type") != "agent":
            continue
        agent_id = mention.get("id")
        if isinstance(agent_id, str) and agent_id and agent_id not in seen:
            seen.add(agent_id)
            mentioned_order.append(agent_id)

    prioritized = [agent_id for agent_id in mentioned_order if agent_id in agent_ids]
    prioritized_set = set(prioritized)
    return prioritized + [agent_id for agent_id in agent_ids if agent_id not in prioritized_set]


def _build_collaboration_final_prompt(
    user_message: str,
    responses_by_agent: list[tuple[str, str]],
    failed_agent_ids: list[str] | None = None,
) -> str:
    """Build a synthesis prompt so one agent can produce the final collaborative answer."""
    lines = [
        "You are generating the final collaborative answer for a group chat.",
        "Original user message:",
        user_message.strip(),
        "",
        "Other agent responses:",
    ]
    for idx, (agent_id, response) in enumerate(responses_by_agent, start=1):
        clipped = response.strip()[:4000]
        lines.extend([f"{idx}. Agent {agent_id}:", clipped, ""])

    if failed_agent_ids:
        lines.extend(
            [
                "Agents that could not produce a full response:",
                ", ".join(failed_agent_ids),
                "",
            ]
        )

    lines.extend(
        [
            (
                "Now provide one final answer for the user that reconciles the agents, resolves "
                "disagreements, and is directly actionable."
            ),
            "Do not repeat raw internal notes; provide only the final user-facing answer.",
        ]
    )
    return "\n".join(lines)


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
        return any(m.get("id") == group_agent.agent_id for m in agent_mentions)

    # Directed reply handling — see docstring step 3.
    if reply_to:
        if reply_to_sender_type == "user":
            return False
        if reply_to_sender_type == "agent" and reply_to_agent_id != group_agent.agent_id:
            return False

    if mode == "auto":
        return True
    if mode == "mention_only":
        return False
    if mode == "smart":
        return await _smart_relevance_check(group_agent.agent_id, content)
    return False


async def _smart_relevance_check(agent_id: str, content: str) -> bool:
    """Use a cheap LLM call to check if the message is relevant to the agent."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    try:
        persona = await agents_service.get_persona(agent_id)
        if not persona:
            return False

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


def _augment_message_with_attachments(content: str, attachments: list[dict] | None) -> str:
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
    response_label: str | None = None,
) -> str | None:
    """Run an agent's response and stream it to the group.

    ``attachments`` carries the triggering user message's files (shape matches
    ``ee.cloud.models.message.Attachment``: ``type``, ``url``, ``name``,
    ``meta``). They're formatted into ``user_message`` before ``pool.run`` so
    the agent sees filename/mime/size the same way it does on the DM path.
    """
    from pocketpaw.agents.pool import get_agent_pool
    from pocketpaw_ee.cloud.chat import message_service

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
        return None

    # Fetch recent conversation history from cloud Messages.
    recent_msgs = await message_service.list_recent_for_group(group_id, limit=20)
    history = [
        {
            "role": "assistant" if m.sender_type == "agent" else "user",
            "content": m.content,
        }
        for m in recent_msgs
    ]

    # Inject knowledge context from agent's knowledge engine
    knowledge_context = ""
    try:
        from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

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
    saw_error_event = False
    error_summary = ""
    last_emit_ts = 0.0
    STREAM_CHUNK_THROTTLE_S = 0.2
    try:
        async for event in pool.run(
            agent_id, user_message, session_key, history, knowledge_context=knowledge_context
        ):
            if event.type in {"message", "text"}:
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
            elif event.type == "error":
                saw_error_event = True
                if isinstance(event.content, str):
                    error_summary = event.content.strip()[:300]
            elif event.type == "done":
                break
    except Exception:
        logger.exception("Agent %s response failed in group %s", agent_id, group_id)
        full_text = full_text or "[Agent response failed]"

    if saw_error_event and not full_text.strip():
        details = f": {error_summary}" if error_summary else ""
        full_text = f"[Agent encountered an error and could not produce a full response{details}]"

    if not full_text.strip():
        return None

    # Check for ripple spec in response
    attachment_dicts: list[dict] = []
    ripple_spec = None
    try:
        json_match = re.search(r"```json\s*(\{.*?\})\s*```", full_text, re.DOTALL)
        if json_match:
            candidate = json.loads(json_match.group(1))
            if "lifecycle" in candidate or "widgets" in candidate:
                from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

                ripple_spec = normalize_ripple_spec(candidate)
                attachment_dicts.append({"type": "ripple", "meta": ripple_spec})
                full_text = full_text[: json_match.start()] + full_text[json_match.end() :]
                full_text = full_text.strip()
    except Exception:
        pass

    # Auto-create pocket from ripple spec
    pocket_id = None
    if ripple_spec:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket_id = await pockets_service.create_from_ripple_spec(
            workspace_id=workspace_id,
            owner_id=group_members[0] if group_members else "",
            ripple_spec=ripple_spec,
            description=f"Generated by {instance.agent_name}",
        )

    # Persist agent message via the chat service so this file stays out
    # of ``ee.cloud.models.message``.
    final_text = full_text
    if response_label:
        final_text = f"{response_label}\n\n{full_text}"

    msg = await message_service.create_agent_message(
        group_id=group_id,
        agent_id=agent_id,
        content=final_text,
        attachments=attachment_dicts or None,
    )

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
                "content": final_text,
                "ripple_spec": ripple_spec,
                "pocket_id": pocket_id,
                "agent_name": instance.agent_name,
            },
        )
    )

    # Observe with soul
    await pool.observe(agent_id, user_message, final_text)

    logger.info(
        "Agent %s responded in group %s (%d chars)",
        instance.agent_name,
        group_id,
        len(final_text),
    )
    return final_text


def register_agent_bridge() -> None:
    """Register the agent bridge event handler."""
    event_bus.subscribe("message.sent", on_message_for_agents)
    logger.info("Agent bridge registered")
