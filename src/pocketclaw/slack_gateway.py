"""Slack bot gateway."""

import asyncio
import logging

from pocketclaw.agents.loop import AgentLoop
from pocketclaw.bus import get_message_bus
from pocketclaw.bus.adapters.slack_adapter import SlackAdapter
from pocketclaw.config import Settings

logger = logging.getLogger(__name__)


async def run_slack_bot(settings: Settings) -> None:
    """Run the Slack bot."""

    bus = get_message_bus()

    adapter = SlackAdapter(
        bot_token=settings.slack_bot_token,
        app_token=settings.slack_app_token,
        allowed_channel_ids=settings.slack_allowed_channel_ids,
    )

    agent_loop = AgentLoop()

    logger.info("Starting PocketPaw Slack bot...")

    await adapter.start(bus)
    loop_task = asyncio.create_task(agent_loop.start())

    try:
        await loop_task
    except asyncio.CancelledError:
        logger.info("Stopping Slack bot...")
    finally:
        await agent_loop.stop()
        await adapter.stop()
