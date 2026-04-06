"""Telegram bot gateway."""

import asyncio
import logging

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.bus import get_message_bus
from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


async def run_bot(settings: Settings) -> None:
    """Run the Telegram bot."""

    # 1. Initialize Bus
    bus = get_message_bus()

    # 2. Initialize Adapter
    adapter = TelegramAdapter(
        token=settings.telegram_bot_token, allowed_user_id=settings.allowed_user_id
    )

    # 3. Initialize Agent Loop
    agent_loop = AgentLoop()
    from pocketpaw.bus.commands import get_command_handler

    get_command_handler().set_agent_loop(agent_loop)

    logger.info("🚀 Starting PocketPaw...")

    # Start components
    await adapter.start(bus)

    # Start Loop (background task)
    loop_task = asyncio.create_task(agent_loop.start())

    try:
        # Keep running
        # We need to await the loop task or just sleep
        # The loop task runs forever until stopped
        await loop_task
    except asyncio.CancelledError:
        logger.info("👋 Stopping...")
    finally:
        await agent_loop.stop()
        await adapter.stop()
