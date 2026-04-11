"""Bridge between cloud chat events and the PocketPaw agent pool.

Changes: Replaced inline pocket creation with PocketService.create_from_ripple_spec()
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

from ee.cloud.chat.schemas import WsOutbound
from ee.cloud.chat.ws import manager as ws_manager
from ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


async def on_message_for_agents(data: dict) -> None:
    """Handle message.sent event — check if any agents should respond."""
    group_id = data.get("group_id")
    sender_id = data.get("sender_id")
    sender_type = data.get("sender_type", "user")
    content = data.get("content", "")
    mentions = data.get("mentions", [])
    workspace_id = data.get("workspace_id", "")

    if not group_id or not content:
        return

    # Don't respond to agent messages (prevent loops)
    if sender_type == "agent":
        return

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
        should = await _should_agent_respond(group_agent, content, mentions)
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
                )
            )


async def _should_agent_respond(group_agent: Any, content: str, mentions: list) -> bool:
    """Determine if an agent should respond based on its respond_mode."""
    mode = group_agent.respond_mode

    if mode == "silent":
        return False
    if mode == "auto":
        return True
    if mode == "mention_only":
        return any(m.get("type") == "agent" and m.get("id") == group_agent.agent for m in mentions)
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


async def _run_agent_response(
    agent_id: str,
    group_id: str,
    workspace_id: str,
    user_message: str,
    group_members: list[str],
) -> None:
    """Run an agent's response and stream it to the group."""
    from ee.cloud.models.message import Attachment, Message
    from pocketpaw.agents.pool import get_agent_pool

    pool = get_agent_pool()
    session_key = f"cloud:{group_id}:{agent_id}"

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
    await ws_manager.broadcast_to_group(
        group_id,
        group_members,
        WsOutbound(
            type="agent.stream_start",
            data={
                "group_id": group_id,
                "agent_id": agent_id,
                "agent_name": instance.agent_name,
                "message_id": temp_msg_id,
            },
        ),
    )

    # Stream response
    full_text = ""
    try:
        async for event in pool.run(
            agent_id, user_message, session_key, history, knowledge_context=knowledge_context
        ):
            if event.type == "message":
                full_text += event.content
                await ws_manager.broadcast_to_group(
                    group_id,
                    group_members,
                    WsOutbound(
                        type="agent.stream_chunk",
                        data={
                            "group_id": group_id,
                            "agent_id": agent_id,
                            "message_id": temp_msg_id,
                            "content": full_text,
                        },
                    ),
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

    # Broadcast final message
    await ws_manager.broadcast_to_group(
        group_id,
        group_members,
        WsOutbound(
            type="agent.stream_end",
            data={
                "group_id": group_id,
                "agent_id": agent_id,
                "message_id": str(msg.id),
                "content": full_text,
                "ripple_spec": ripple_spec,
                "pocket_id": pocket_id,
                "agent_name": instance.agent_name,
            },
        ),
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
