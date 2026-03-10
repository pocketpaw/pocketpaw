"""PocketPaw entry point.

Changes:
  - 2026-02-20: Extracted diagnostics to diagnostics.py, headless runners to headless.py.
  - 2026-02-18: Added --doctor CLI flag (runs all health checks + version check, prints report).
  - 2026-02-18: Styled update notice (ANSI box on stderr, suppressed in CI/non-TTY).
  - 2026-02-17: Run startup health checks after settings load (prints colored summary).
  - 2026-02-16: Add startup version check against PyPI (cached daily, silent on error).
  - 2026-02-14: Dashboard deps moved to core — `pip install pocketpaw` just works.
  - 2026-02-12: Fixed --version to read dynamically from package metadata.
  - 2026-02-06: Web dashboard is now the default mode (no flags needed).
  - 2026-02-06: Added --telegram flag for legacy Telegram-only mode.
  - 2026-02-06: Added --discord, --slack, --whatsapp CLI modes.
  - 2026-02-02: Added Rich logging for beautiful console output.
  - 2026-02-03: Handle port-in-use gracefully with automatic port finding.
"""

import os
import sys

# Force UTF-8 encoding on Windows before anything else might produce output
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import logging
import argparse
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version as get_version

from pocketpaw.config import Settings, get_settings
from pocketpaw.diagnostics import check_ollama, check_openai_compatible, run_doctor
from pocketpaw.headless import (
    _check_extras_installed,
    _is_headless,
    run_multi_channel_mode,
    run_telegram_mode,
)
from pocketpaw.logging_setup import setup_logging


def _run_async(coro):
    """Run coroutine; use asyncio.run() when no loop is running, else run in a thread to avoid
    'Runner.run() cannot be called from a running event loop' (e.g. under pytest-asyncio)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


# Setup beautiful logging with Rich
setup_logging(level="INFO")
logger = logging.getLogger(__name__)

APP_VERSION = get_version("pocketpaw")


def run_dashboard_mode(settings: Settings, host: str, port: int, dev: bool = False) -> None:
    """Run in web dashboard mode."""
    from pocketpaw.dashboard import run_dashboard

    run_dashboard(
        host=host,
        port=port,
        open_browser=not _is_headless() and not dev,
        dev=dev,
    )


def main() -> None:
    """Main entry point."""

    parser = argparse.ArgumentParser(
        description="🐾 PocketPaw (Beta) - The AI agent that runs on your laptop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pocketpaw
  pocketpaw --discord
  pocketpaw --discord --slack
  pocketpaw --telegram
  pocketpaw serve --port 9000
""",
    )

    # Dashboard / integration modes
    parser.add_argument("--web", "-w", action="store_true", help="Run web dashboard")
    parser.add_argument("--telegram", action="store_true", help="Run Telegram-only mode")
    parser.add_argument("--discord", action="store_true", help="Run headless Discord bot")
    parser.add_argument("--slack", action="store_true", help="Run headless Slack bot")
    parser.add_argument("--whatsapp", action="store_true", help="Run WhatsApp webhook server")
    parser.add_argument("--signal", action="store_true", help="Run headless Signal bot")
    parser.add_argument("--matrix", action="store_true", help="Run headless Matrix bot")
    parser.add_argument("--teams", action="store_true", help="Run headless Microsoft Teams bot")
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
        "--pii-scan",
        action="store_true",
        help="Scan existing memory files for PII and report findings",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host to bind web server (default: auto-detect; 0.0.0.0 on headless servers)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8888,
        help="Port for web server (default: 8888)",
    )
    parser.add_argument("--dev", action="store_true", help="Development mode with auto-reload")
    parser.add_argument(
        "--check-ollama",
        action="store_true",
        help="Check Ollama connectivity, model availability, and tool calling support",
    )
    parser.add_argument(
        "--check-openai-compatible",
        action="store_true",
        help="Check OpenAI compatible API",
    )

    parser.add_argument("--doctor", action="store_true", help="Run system diagnostics")

    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
        help="Show PocketPaw version",
    )

    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="Subcommand: 'serve' starts API-only server",
    )

    args = parser.parse_args()

    channel_flags = [
        args.discord,
        args.slack,
        args.whatsapp,
        args.signal,
        args.matrix,
        args.teams,
        args.gchat,
    ]

    if args.telegram and any(channel_flags):
        parser.error("--telegram cannot be combined with other channel flags")

    _check_extras_installed(args)

    settings = get_settings()

    # Startup health checks
    if settings.health_check_on_startup:
        try:
            from pocketpaw.health import get_health_engine

            engine = get_health_engine()
            results = engine.run_startup_checks()
            issues = [r for r in results if r.status != "ok"]
            if issues:
                print()
                for r in results:
                    if r.status == "ok":
                        print(f"  \033[32m[OK]\033[0m   {r.name}: {r.message}")
                    elif r.status == "warning":
                        print(f"  \033[33m[WARN]\033[0m {r.name}: {r.message}")
                        if r.fix_hint:
                            print(f"         {r.fix_hint}")
                    else:
                        print(f"  \033[31m[FAIL]\033[0m {r.name}: {r.message}")
                        if r.fix_hint:
                            print(f"         {r.fix_hint}")
                status = engine.overall_status
                color = {"healthy": "32", "degraded": "33", "unhealthy": "31"}.get(status, "0")
                print(f"\n  System: \033[{color}m{status.upper()}\033[0m\n")
        except Exception:
            pass  # Health engine failure never blocks startup

    # Check for updates in background thread to avoid blocking startup
    # (cold start or stale cache triggers a sync HTTP request to PyPI)
    import threading

    def _bg_update_check() -> None:
        try:
            from pocketpaw.config import get_config_dir
            from pocketpaw.update_check import check_for_updates, print_styled_update_notice

            update_info = check_for_updates(get_version("pocketpaw"), get_config_dir())
            if update_info and update_info.get("update_available"):
                print_styled_update_notice(update_info)
        except Exception:
            pass  # Update check failure never interrupts startup

    threading.Thread(target=_bg_update_check, daemon=True).start()

    if args.host is not None:
        host = args.host
    elif settings.web_host != "127.0.0.1":
        host = settings.web_host
    elif _is_headless():
        host = "0.0.0.0"
        logger.info("Headless server detected — binding to 0.0.0.0")
    else:
        host = "127.0.0.1"

    try:
        if args.command == "serve":
            from pocketpaw.api.serve import run_api_server

            run_api_server(host=host, port=args.port, dev=args.dev)

        elif args.check_ollama:
            exit_code = _run_async(check_ollama(settings))
            raise SystemExit(exit_code)

        elif args.check_openai_compatible:
            exit_code = _run_async(check_openai_compatible(settings))
            raise SystemExit(exit_code)

        elif args.doctor:
            exit_code = _run_async(run_doctor())
            raise SystemExit(exit_code)

        elif args.security_audit:
            from pocketpaw.security.audit_cli import run_security_audit

            exit_code = _run_async(run_security_audit(fix=args.fix))
            raise SystemExit(exit_code)
        elif args.pii_scan:
            from pocketpaw.security.audit_cli import scan_memory_for_pii

            exit_code = asyncio.run(scan_memory_for_pii())
            raise SystemExit(exit_code)

        elif args.telegram:
            _run_async(run_telegram_mode(settings))
        elif has_channel_flag:
            _run_async(run_multi_channel_mode(settings, args))
        else:
            run_dashboard_mode(settings, host, args.port, dev=args.dev)

    except KeyboardInterrupt:
        logger.info("PocketPaw stopped by user.")

    finally:
        from pocketpaw.lifecycle import shutdown_all

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(shutdown_all())
            loop.close()
        except (RuntimeError, OSError) as e:
            # RuntimeError can occur if the event loop is already closed
            # (common during interpreter shutdown on Windows).
            # OSError may occur if sockets are already cleaned up
            # during a forced shutdown.
            logger.debug("Shutdown cleanup issue: %s", e)


if __name__ == "__main__":
    main()
