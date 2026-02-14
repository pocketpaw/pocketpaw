"""PocketPaw entry point.

Changes:
  - 2026-02-06: Web dashboard is now the default mode (no flags needed).
  - 2026-02-06: Added --telegram flag for legacy Telegram-only mode.
  - 2026-02-06: Added --discord, --slack, --whatsapp CLI modes.
  - 2026-02-02: Added Rich logging for beautiful console output.
  - 2026-02-03: Handle port-in-use gracefully with automatic port finding.
  - 2026-02-13: Fixed UTF-8 encoding for Windows console output.
"""

import argparse
import asyncio
import importlib.util
import logging
import sys
import webbrowser

from pocketclaw.config import Settings, get_settings
from pocketclaw.logging_setup import setup_logging

# Fix UTF-8 encoding for Windows console output
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Setup beautiful logging with Rich
setup_logging(level="INFO")
logger = logging.getLogger(__name__)


async def run_telegram_mode(settings: Settings) -> None:
    """Run in Telegram bot mode."""
    from pocketclaw.bot_gateway import run_bot
    from pocketclaw.web_server import find_available_port, run_pairing_server

    # Check if we need to run pairing flow
    if not settings.telegram_bot_token or not settings.allowed_user_id:
        logger.info("🔧 First-time setup: Starting pairing server...")

        # Find available port before showing instructions
        try:
            port = find_available_port(settings.web_port)
        except OSError:
            logger.error(
                "❌ Could not find an available port. Please close other applications and try again."
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
    from pocketclaw.agents.loop import AgentLoop
    from pocketclaw.bus import get_message_bus

    bus = get_message_bus()
    adapters = []

    if args.discord:
        if not settings.discord_bot_token:
            logger.error("Discord bot token not configured. Set POCKETCLAW_DISCORD_BOT_TOKEN.")
        else:
            from pocketclaw.bus.adapters.discord_adapter import DiscordAdapter

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
                "Slack tokens not configured. Set POCKETCLAW_SLACK_BOT_TOKEN "
                "and POCKETCLAW_SLACK_APP_TOKEN."
            )
        else:
            from pocketclaw.bus.adapters.slack_adapter import SlackAdapter

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
                "WhatsApp not configured. Set POCKETCLAW_WHATSAPP_ACCESS_TOKEN "
                "and POCKETCLAW_WHATSAPP_PHONE_NUMBER_ID."
            )
        else:
            from pocketclaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter

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
            logger.error("Signal not configured. Set POCKETCLAW_SIGNAL_PHONE_NUMBER.")
        else:
            from pocketclaw.bus.adapters.signal_adapter import SignalAdapter

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
                "Matrix not configured. Set POCKETCLAW_MATRIX_HOMESERVER "
                "and POCKETCLAW_MATRIX_USER_ID."
            )
        else:
            from pocketclaw.bus.adapters.matrix_adapter import MatrixAdapter

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
                "Teams not configured. Set POCKETCLAW_TEAMS_APP_ID "
                "and POCKETCLAW_TEAMS_APP_PASSWORD."
            )
        else:
            from pocketclaw.bus.adapters.teams_adapter import TeamsAdapter

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
            logger.error("Google Chat not configured. Set POCKETCLAW_GCHAT_SERVICE_ACCOUNT_KEY.")
        else:
            from pocketclaw.bus.adapters.gchat_adapter import GoogleChatAdapter

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

        import pocketclaw.whatsapp_gateway as wa_gw
        from pocketclaw.whatsapp_gateway import create_whatsapp_app

        # Point the gateway module at our adapter
        for a in adapters:
            if a.channel.value == "whatsapp":
                wa_gw._whatsapp_adapter = a
                break

        wa_app = create_whatsapp_app(settings)
        config = uvicorn.Config(
            wa_app, host=settings.web_host, port=settings.web_port, log_level="info"
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

    if sys.platform == "darwin":
        return False  # macOS always has a display
    return not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")


def run_dashboard_mode(settings: Settings, host: str, port: int) -> None:
    """Run in web dashboard mode."""
    from pocketclaw.dashboard import run_dashboard

    run_dashboard(host=host, port=port, open_browser=not _is_headless())


def _check_extras_installed(args: argparse.Namespace) -> None:
    """Check that required optional dependencies are installed for the chosen mode.

    Exits with a helpful message if something is missing.
    """
    missing: list[tuple[str, str, str]] = []  # (package, import_name, extra)

    has_channel_flag = (
        args.discord
        or args.slack
        or args.whatsapp
        or getattr(args, "signal", False)
        or getattr(args, "matrix", False)
        or getattr(args, "teams", False)
        or getattr(args, "gchat", False)
    )

    # Default mode (dashboard) requires fastapi
    if not args.telegram and not has_channel_flag and not args.security_audit:
        if importlib.util.find_spec("fastapi") is None:
            missing.append(("fastapi", "fastapi", "dashboard"))
        if importlib.util.find_spec("uvicorn") is None:
            missing.append(("uvicorn", "uvicorn", "dashboard"))

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


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="🐾 PocketPaw (Beta) - The AI agent that runs on your laptop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pocketpaw                          Start web dashboard (default)
  pocketpaw --telegram               Start in Telegram-only mode
  pocketpaw --discord                Start headless Discord bot
  pocketpaw --slack                  Start headless Slack bot (Socket Mode)
  pocketpaw --whatsapp               Start headless WhatsApp webhook server
  pocketpaw --discord --slack        Run Discord + Slack simultaneously
""",
    )

    parser.add_argument(
        "--web",
        "-w",
        action="store_true",
        help="Run web dashboard (same as default, kept for compatibility)",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Run Telegram-only mode (legacy pairing flow)",
    )
    parser.add_argument("--discord", action="store_true", help="Run headless Discord bot")
    parser.add_argument("--slack", action="store_true", help="Run headless Slack bot (Socket Mode)")
    parser.add_argument(
        "--whatsapp", action="store_true", help="Run headless WhatsApp webhook server"
    )
    parser.add_argument("--signal", action="store_true", help="Run headless Signal bot")
    parser.add_argument("--matrix", action="store_true", help="Run headless Matrix bot")
    parser.add_argument("--teams", action="store_true", help="Run headless Teams bot")
    parser.add_argument("--gchat", action="store_true", help="Run headless Google Chat bot")
    parser.add_argument(
        "--security-audit",
        action="store_true",
        help="Run security audit and print report",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix fixable issues found by --security-audit",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host to bind web server (default: auto-detect; 0.0.0.0 on headless servers)",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=8888, help="Port for web server (default: 8888)"
    )
    parser.add_argument("--version", "-v", action="version", version="%(prog)s 0.2.0")

    args = parser.parse_args()

    # Fail fast if optional deps are missing for the chosen mode
    _check_extras_installed(args)

    settings = get_settings()

    # Resolve host: explicit flag > config > auto-detect
    if args.host is not None:
        host = args.host
    elif settings.web_host != "127.0.0.1":
        host = settings.web_host
    elif _is_headless():
        host = "0.0.0.0"
        logger.info("Headless server detected — binding to 0.0.0.0")
    else:
        host = "127.0.0.1"

    has_channel_flag = (
        args.discord
        or args.slack
        or args.whatsapp
        or args.signal
        or args.matrix
        or args.teams
        or args.gchat
    )

    try:
        if args.security_audit:
            from pocketclaw.security.audit_cli import run_security_audit

            exit_code = asyncio.run(run_security_audit(fix=args.fix))
            raise SystemExit(exit_code)
        elif args.telegram:
            asyncio.run(run_telegram_mode(settings))
        elif has_channel_flag:
            asyncio.run(run_multi_channel_mode(settings, args))
        else:
            # Default: web dashboard (also handles --web flag)
            run_dashboard_mode(settings, host, args.port)
    except KeyboardInterrupt:
        logger.info("👋 PocketPaw stopped.")
    finally:
        # Coordinated singleton shutdown
        from pocketclaw.lifecycle import shutdown_all

        try:
            asyncio.run(shutdown_all())
        except RuntimeError:
            # Event loop already closed — best-effort sync cleanup
            pass


if __name__ == "__main__":
    main()
