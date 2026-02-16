"""Telegram bot gateway.

This module wires the external Telegram transport into the internal
PocketPaw message bus and long-running agent loop. High-level flow:

* `get_message_bus()` returns the process-wide message bus used by agents.
* `TelegramAdapter` connects to the Telegram Bot API and forwards inbound
  updates into the bus, and sends outbound messages back to Telegram.
* `AgentLoop` consumes messages from the bus and drives the core agent logic.
* `run_bot()` composes these components into a long-lived async task that
  runs until cancelled by the hosting process.

No business logic lives here; this is an integration boundary focused on
orchestration, lifecycle, and observability.
"""

import asyncio
import logging

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.bus import get_message_bus
from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter
from pocketpaw.config import Settings

logger: logging.Logger = logging.getLogger(__name__)


async def run_bot(settings: Settings) -> None:
    """Run the Telegram bot gateway for a single process.

    This coroutine is intended to be awaited once by the application entry
    point. It returns only when the agent loop stops or the task is
    cancelled by the caller. Any startup failures in the adapter or agent
    loop are logged and propagated to the caller for visibility.

    Args:
        settings: Loaded application settings providing Telegram credentials
            and access control (`telegram_bot_token`, `allowed_user_id`).
    """

    # 1. Initialize bus shared across adapters and agent loop.
    bus = get_message_bus()

    # 2. Initialize Telegram adapter (transport boundary).
    adapter = TelegramAdapter(
        token=settings.telegram_bot_token, allowed_user_id=settings.allowed_user_id
    )

    # 3. Initialize long-running agent loop (core reasoning engine).
    agent_loop = AgentLoop()

    logger.info("Starting PocketPaw Telegram bot gateway")

    # Start components
    try:
        await adapter.start(bus)
    except Exception:
        logger.exception("Failed to start Telegram adapter")
        raise

    # Start Loop (background task)
    try:
        loop_task = asyncio.create_task(agent_loop.start())
    except Exception:
        logger.exception("Failed to start agent loop")
        # Attempt to shut down adapter before propagating error
        try:
            await adapter.stop()
        except Exception:
            logger.exception("Error while stopping adapter after agent loop start failure")
        raise

    try:
        # Keep running
        # We need to await the loop task or just sleep
        # The loop task runs forever until stopped
        await loop_task
    except asyncio.CancelledError:
        logger.info("👋 Stopping...")
    finally:
        try:
            await agent_loop.stop()
        except Exception:
            # Log but do not mask any exception from the main loop.
            logger.exception("Error while stopping agent loop")
        try:
            await adapter.stop()
        except Exception:
            logger.exception("Error while stopping Telegram adapter")
