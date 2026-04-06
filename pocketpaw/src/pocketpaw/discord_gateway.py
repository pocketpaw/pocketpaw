"""Discord bot gateway."""

import asyncio
import logging

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.bus import get_message_bus
from pocketpaw.bus.adapters.discord_adapter import DiscliAdapter as DiscordAdapter
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


async def run_discord_bot(settings: Settings) -> None:
    """Run the Discord bot."""

    bus = get_message_bus()

    adapter = DiscordAdapter(
        token=settings.discord_bot_token,
        allowed_guild_ids=settings.discord_allowed_guild_ids,
        allowed_user_ids=settings.discord_allowed_user_ids,
        allowed_channel_ids=settings.discord_allowed_channel_ids,
        conversation_channel_ids=settings.discord_conversation_channel_ids,
        conversation_all_channels=settings.discord_conversation_all_channels,
        conversation_exclude_channel_ids=settings.discord_conversation_exclude_channel_ids,
        bot_name=settings.discord_bot_name,
        status_type=settings.discord_status_type,
        activity_type=settings.discord_activity_type,
        activity_text=settings.discord_activity_text,
    )

    agent_loop = AgentLoop()
    from pocketpaw.bus.commands import get_command_handler

    get_command_handler().set_agent_loop(agent_loop)

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
