"""Discord bot gateway."""

import asyncio
import logging

from pocketclaw.agents.loop import AgentLoop
from pocketclaw.bus import get_message_bus
from pocketclaw.bus.adapters.discord_adapter import DiscordAdapter
from pocketclaw.config import Settings

logger = logging.getLogger(__name__)


async def run_discord_bot(settings: Settings) -> None:
    """Run the Discord bot."""

    bus = get_message_bus()

    adapter = DiscordAdapter(
        token=settings.discord_bot_token,
        allowed_guild_ids=settings.discord_allowed_guild_ids,
        allowed_user_ids=settings.discord_allowed_user_ids,
    )

    agent_loop = AgentLoop()

    logger.info("Starting PocketPaw Discord bot...")

    await adapter.start(bus)
    loop_task = asyncio.create_task(agent_loop.start())

    try:
        await loop_task
    except asyncio.CancelledError:
        logger.info("Stopping Discord bot...")
    finally:
        await agent_loop.stop()
        await adapter.stop()
