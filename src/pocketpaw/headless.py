"""Headless channel runners for PocketPaw CLI.

Extracted from __main__.py — contains run_telegram_mode(),
run_multi_channel_mode(), _is_headless(), and _check_extras_installed().
"""

import argparse
import asyncio
import importlib.util
import logging
import sys
import webbrowser

from pocketpaw.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def run_telegram_mode(settings: Settings) -> None:
    """Run in Telegram bot mode."""
    from pocketpaw.bot_gateway import run_bot
    from pocketpaw.web_server import find_available_port, run_pairing_server

    # Check if we need to run pairing flow
    if not settings.telegram_bot_token or not settings.allowed_user_id:
        logger.info("🔧 First-time setup: Starting pairing server...")

        # Find available port before showing instructions
        try:
            port = find_available_port(settings.web_port)
        except OSError:
            logger.error(
                "❌ Could not find an available port."
                " Please close other applications and try again."
            )
            return

        print("\n" + "=" * 50)
        print("🐾 POCKETPAW SETUP")
        print("=" * 50)
        print("\n1. Create a Telegram bot via @BotFather")
        print("2. Copy the bot token")
        print(f"3. Open http://localhost:{port} in your browser")
        print("4. Paste the token and scan the QR code\n")

        # Open browser automatically with correct port
        webbrowser.open(f"http://localhost:{port}")

        # Run pairing server (blocks until pairing complete)
        await run_pairing_server(settings)

        # Reload settings after pairing
        settings = get_settings(force_reload=True)

    # Start the bot
    logger.info("🚀 Starting PocketPaw (Beta)...")
    await run_bot(settings)


async def run_multi_channel_mode(settings: Settings, args: argparse.Namespace) -> None:
    """Run one or more channel adapters sharing a single bus and AgentLoop."""
    from pocketpaw.agents.loop import AgentLoop
    from pocketpaw.bus import get_message_bus

    bus = get_message_bus()
    adapters = []

    if args.discord:
        if not settings.discord_bot_token:
            logger.error("Discord bot token not configured. Set POCKETPAW_DISCORD_BOT_TOKEN.")
        else:
            from pocketpaw.bus.adapters.discord_adapter import DiscordAdapter

            adapters.append(
                DiscordAdapter(
                    token=settings.discord_bot_token,
                    allowed_guild_ids=settings.discord_allowed_guild_ids,
                    allowed_user_ids=settings.discord_allowed_user_ids,
                )
            )

    if args.slack:
        if not settings.slack_bot_token or not settings.slack_app_token:
            logger.error(
                "Slack tokens not configured. Set POCKETPAW_SLACK_BOT_TOKEN "
                "and POCKETPAW_SLACK_APP_TOKEN."
            )
        else:
            from pocketpaw.bus.adapters.slack_adapter import SlackAdapter

            adapters.append(
                SlackAdapter(
                    bot_token=settings.slack_bot_token,
                    app_token=settings.slack_app_token,
                    allowed_channel_ids=settings.slack_allowed_channel_ids,
                )
            )

    if args.whatsapp:
        if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
            logger.error(
                "WhatsApp not configured. Set POCKETPAW_WHATSAPP_ACCESS_TOKEN "
                "and POCKETPAW_WHATSAPP_PHONE_NUMBER_ID."
            )
        else:
            from pocketpaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter

            adapters.append(
                WhatsAppAdapter(
                    access_token=settings.whatsapp_access_token,
                    phone_number_id=settings.whatsapp_phone_number_id,
                    verify_token=settings.whatsapp_verify_token or "",
                    allowed_phone_numbers=settings.whatsapp_allowed_phone_numbers,
                )
            )

    if getattr(args, "signal", False):
        if not settings.signal_phone_number:
            logger.error("Signal not configured. Set POCKETPAW_SIGNAL_PHONE_NUMBER.")
        else:
            from pocketpaw.bus.adapters.signal_adapter import SignalAdapter

            adapters.append(
                SignalAdapter(
                    api_url=settings.signal_api_url,
                    phone_number=settings.signal_phone_number,
                    allowed_phone_numbers=settings.signal_allowed_phone_numbers,
                )
            )

    if getattr(args, "matrix", False):
        if not settings.matrix_homeserver or not settings.matrix_user_id:
            logger.error(
                "Matrix not configured. Set POCKETPAW_MATRIX_HOMESERVER "
                "and POCKETPAW_MATRIX_USER_ID."
            )
        else:
            from pocketpaw.bus.adapters.matrix_adapter import MatrixAdapter

            adapters.append(
                MatrixAdapter(
                    homeserver=settings.matrix_homeserver,
                    user_id=settings.matrix_user_id,
                    access_token=settings.matrix_access_token,
                    password=settings.matrix_password,
                    allowed_room_ids=settings.matrix_allowed_room_ids,
                    device_id=settings.matrix_device_id,
                )
            )

    if getattr(args, "teams", False):
        if not settings.teams_app_id or not settings.teams_app_password:
            logger.error(
                "Teams not configured. Set POCKETPAW_TEAMS_APP_ID and POCKETPAW_TEAMS_APP_PASSWORD."
            )
        else:
            from pocketpaw.bus.adapters.teams_adapter import TeamsAdapter

            adapters.append(
                TeamsAdapter(
                    app_id=settings.teams_app_id,
                    app_password=settings.teams_app_password,
                    allowed_tenant_ids=settings.teams_allowed_tenant_ids,
                    webhook_port=settings.teams_webhook_port,
                )
            )

    if getattr(args, "gchat", False):
        if not settings.gchat_service_account_key:
            logger.error("Google Chat not configured. Set POCKETPAW_GCHAT_SERVICE_ACCOUNT_KEY.")
        else:
            from pocketpaw.bus.adapters.gchat_adapter import GoogleChatAdapter

            adapters.append(
                GoogleChatAdapter(
                    mode=settings.gchat_mode,
                    service_account_key=settings.gchat_service_account_key,
                    project_id=settings.gchat_project_id,
                    subscription_id=settings.gchat_subscription_id,
                    allowed_space_ids=settings.gchat_allowed_space_ids,
                )
            )

    if not adapters:
        logger.error("No channel adapters could be started. Check your configuration.")
        return

    agent_loop = AgentLoop()

    for adapter in adapters:
        await adapter.start(bus)
        logger.info(f"Started {adapter.channel.value} adapter")

    loop_task = asyncio.create_task(agent_loop.start())

    # If WhatsApp is one of the adapters, start a minimal webhook server
    whatsapp_server = None
    if args.whatsapp:
        import uvicorn

        import pocketpaw.whatsapp_gateway as wa_gw
        from pocketpaw.whatsapp_gateway import create_whatsapp_app

        # Point the gateway module at our adapter
        for a in adapters:
            if a.channel.value == "whatsapp":
                wa_gw._whatsapp_adapter = a
                break

        wa_app = create_whatsapp_app(settings)
        config = uvicorn.Config(
            wa_app,
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
        )
        whatsapp_server = uvicorn.Server(config)
        asyncio.create_task(whatsapp_server.serve())

    try:
        await loop_task
    except asyncio.CancelledError:
        logger.info("Stopping channels...")
    finally:
        await agent_loop.stop()
        for adapter in adapters:
            await adapter.stop()


def _is_headless() -> bool:
    """Detect headless server (no display)."""
    import os

    if sys.platform in ("darwin", "win32"):
        return False  # macOS and Windows always have a display
    return not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")


def _check_extras_installed(args: argparse.Namespace) -> None:
    """Check that required optional dependencies are installed for the chosen mode.

    Exits with a helpful message if something is missing.
    """
    missing: list[tuple[str, str, str]] = []  # (package, import_name, extra)

    # Dashboard deps are now in core — no need to check for them.

    if args.telegram:
        if importlib.util.find_spec("telegram") is None:
            missing.append(("python-telegram-bot", "telegram", "telegram"))

    channel_checks = {
        "discord": ("discord.py", "discord", "discord"),
        "slack": ("slack-bolt", "slack_bolt", "slack"),
    }
    for flag, (pkg, mod, extra) in channel_checks.items():
        if getattr(args, flag, False) and importlib.util.find_spec(mod) is None:
            missing.append((pkg, mod, extra))

    if not missing:
        return

    print("\n  Missing dependencies detected:\n")
    extras = set()
    for pkg, _mod, extra in missing:
        print(f"    - {pkg}  (extra: {extra})")
        extras.add(extra)
    extras_str = ",".join(sorted(extras))
    print(f"\n  Install with:  pip install 'pocketpaw[{extras_str}]'\n")
    sys.exit(1)
