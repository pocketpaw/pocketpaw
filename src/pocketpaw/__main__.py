"""PocketPaw entry point.

Changes:
  - 2026-04-07: Auto-detect free port in range 8000-9000 if requested port is busy.
  - 2026-03-18: Added CLI subcommands: doctor, health, channels, skills,
                sessions, memory, config, errors, logs.
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

# Force UTF-8 encoding on Windows before any imports that might produce output
import os
import sys

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

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


def _check_python_version() -> None:
    """Warn if running on an unsupported Python version."""
    if sys.version_info[:2] >= (3, 14):
        sys.stderr.write(
            "Warning: Python 3.14+ may not be fully supported. "
            "Recommended version is 3.11 or 3.12.\n"
        )


def run_dashboard_mode(settings: Settings, host: str, port: int, dev: bool = False) -> None:
    """Run in web dashboard mode."""
    from pocketpaw.dashboard import run_dashboard

    run_dashboard(
        host=host,
        port=port,
        open_browser=not _is_headless() and not dev,
        dev=dev,
    )


# ── Subcommands that exit early (no settings/health needed) ─────────────

_EARLY_COMMANDS = {
    "update",
    "doctor",
    "health",
    "skills",
    "memory",
    "sessions",
    "config",
    "errors",
    "logs",
}


def _handle_early_command(args) -> int | None:
    """Handle subcommands that don't need settings/health/env setup.

    Returns exit code, or None if the command is not an early command.
    """
    cmd = args.command

    if cmd == "update":
        from pocketpaw.cli.update import run_update

        return run_update(get_version("pocketpaw"))

    if cmd == "doctor":
        from pocketpaw.cli.doctor import run_doctor_cmd

        return _run_async(run_doctor_cmd(as_json=getattr(args, "json", False)))

    if cmd == "health":
        from pocketpaw.cli.health import run_health_cmd

        return run_health_cmd(as_json=getattr(args, "json", False))

    if cmd == "skills":
        from pocketpaw.cli.skills import run_skills_cmd

        return run_skills_cmd(
            search=getattr(args, "search", None),
            as_json=getattr(args, "json", False),
        )

    if cmd == "memory":
        from pocketpaw.cli.memory import run_memory_cmd

        return run_memory_cmd(
            action=getattr(args, "subaction", None),
            query=getattr(args, "query", None),
            limit=getattr(args, "limit", 10),
            as_json=getattr(args, "json", False),
        )

    if cmd == "sessions":
        from pocketpaw.cli.sessions import run_sessions_cmd

        return run_sessions_cmd(
            action=getattr(args, "subaction", None),
            query=getattr(args, "query", None),
            limit=getattr(args, "limit", 20),
            as_json=getattr(args, "json", False),
        )

    if cmd == "config":
        from pocketpaw.cli.config_cmd import run_config_cmd

        return run_config_cmd(
            action=getattr(args, "subaction", None),
            key=getattr(args, "key", None),
            value=getattr(args, "value", None),
            as_json=getattr(args, "json", False),
        )

    if cmd == "errors":
        from pocketpaw.cli.errors import run_errors_cmd

        return run_errors_cmd(
            limit=getattr(args, "limit", 20),
            search=getattr(args, "search", None),
            as_json=getattr(args, "json", False),
        )

    if cmd == "logs":
        from pocketpaw.cli.logs import run_logs_cmd

        return run_logs_cmd(
            limit=getattr(args, "limit", 50),
            follow=getattr(args, "follow", False),
            as_json=getattr(args, "json", False),
        )

    return None


# ── Argument parser ─────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PocketPaw - The AI agent that runs on your laptop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pocketpaw                            Start web dashboard (default)
  pocketpaw serve                      Start API-only server (no dashboard)
  pocketpaw status                     Show agent status
  pocketpaw status --json --watch      Monitor status as JSON

  pocketpaw doctor                     Full diagnostics (config + connectivity)
  pocketpaw health                     Quick health check (no network)
  pocketpaw errors                     Show recent errors
  pocketpaw logs --follow              Tail audit log

  pocketpaw channels                   List channel status
  pocketpaw channels start discord     Start Discord adapter
  pocketpaw channels stop slack        Stop Slack adapter

  pocketpaw skills                     List available skills
  pocketpaw sessions                   List chat sessions
  pocketpaw sessions delete <key>      Delete a session
  pocketpaw sessions search <query>    Search session content

  pocketpaw memory                     Show memory stats
  pocketpaw memory search <query>      Search long-term memories

  pocketpaw config                     Show config (secrets masked)
  pocketpaw config set <key> <value>   Set a config value
  pocketpaw config validate            Validate API keys
  pocketpaw config path                Print config file path

  pocketpaw update                     Update to latest version

  pocketpaw --telegram                 Start Telegram-only mode
  pocketpaw --discord --slack          Run multiple channels headless
""",
    )

    # ── Global flags ────────────────────────────────────────────────────
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
        "--whatsapp",
        action="store_true",
        help="Run headless WhatsApp webhook server",
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
        help="Port for web server (default: 8888; auto-falls back if busy)",
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
        help="Check OpenAI-compatible endpoint connectivity and tool calling support",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="(deprecated: use 'pocketpaw doctor') Run diagnostics",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {get_version('pocketpaw')}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON (works with most subcommands)",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=2.0,
        default=0,
        help="Watch mode: refresh status every N seconds (default: 2)",
    )

    # ── Subcommand (positional) ─────────────────────────────────────────
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        choices=[
            "serve",
            "status",
            "update",
            "doctor",
            "health",
            "channels",
            "skills",
            "sessions",
            "memory",
            "config",
            "errors",
            "logs",
        ],
        help="Subcommand to run",
    )
    parser.add_argument(
        "subargs",
        nargs="*",
        default=[],
        help=argparse.SUPPRESS,
    )

    # ── Flags for subcommands (shared namespace) ────────────────────────
    parser.add_argument("--search", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of results (for errors, logs, sessions, memory)",
    )
    parser.add_argument("--follow", action="store_true", help="Tail mode (for logs)")

    return parser


def _resolve_subargs(args) -> None:
    """Parse positional subargs into named attributes based on the command.

    Transforms: pocketpaw channels start discord
    Into: args.subaction="start", args.query="discord"
    """
    subargs = args.subargs or []
    args.subaction = None
    args.query = None
    args.key = None
    args.value = None

    cmd = args.command

    if cmd == "channels" and subargs:
        args.subaction = subargs[0]
        if len(subargs) > 1:
            args.query = subargs[1]
    elif cmd == "sessions" and subargs:
        args.subaction = subargs[0]
        if len(subargs) > 1:
            args.query = subargs[1]
    elif cmd == "memory" and subargs:
        args.subaction = subargs[0]
        if len(subargs) > 1:
            args.query = subargs[1]
    elif cmd == "config" and subargs:
        args.subaction = subargs[0]
        if len(subargs) > 1:
            args.key = subargs[1]
        if len(subargs) > 2:
            args.value = subargs[2]

    if args.limit is None:
        defaults = {"errors": 20, "logs": 50, "sessions": 20, "memory": 10}
        args.limit = defaults.get(cmd, 20)


def _serve(
    fn, *args, port: int = 8888, max_attempts: int = 10, host: str = "127.0.0.1", **kwargs
) -> None:
    """Start server, retrying with port+1 on EADDRINUSE.

    Uses SO_REUSEADDR socket probe as best-effort pre-check (fast feedback),
    then passes the port directly to the server. The probe window is tiny so
    the race is acceptable; the real guard is the server bind itself.
    The probe binds to the same host the server will use, fixing the
    0.0.0.0 vs 127.0.0.1 mismatch. Scanning starts from the requested port,
    not from 8000, so fallback is always requested+N.
    """
    import errno as _errno
    import socket as _socket

    current_port = port
    for attempt in range(max_attempts):
        # Best-effort probe using same host the server will bind to
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            try:
                s.bind((host, current_port))
            except OSError:
                next_port = current_port + 1
                print(f"\n  [WARN] Port {current_port} busy — trying {next_port}\n")
                current_port = next_port
                continue
        # Probe passed — attempt real server startup
        try:
            fn(*args, port=current_port, host=host, **kwargs)
            return
        except OSError as e:
            if e.errno in (_errno.EADDRINUSE, 10048):
                next_port = current_port + 1
                print(f"\n  [WARN] Port {current_port} taken at bind — trying {next_port}\n")
                current_port = next_port
            else:
                raise
    raise RuntimeError(
        f"No free port found after {max_attempts} attempts (tried {port}-{current_port - 1})."
    )


def main() -> None:
    """Main entry point."""
    _check_python_version()
    parser = _build_parser()
    args = parser.parse_args()
    _resolve_subargs(args)

    # ── Early-exit commands (no settings, health, or env setup needed) ──
    if args.command in _EARLY_COMMANDS:
        exit_code = _handle_early_command(args)
        if exit_code is not None:
            raise SystemExit(exit_code)

    # ── Channels subcommand (needs settings for list, API for start/stop) ──
    if args.command == "channels":
        from pocketpaw.cli.channels import run_channels_cmd

        exit_code = run_channels_cmd(
            action=args.subaction,
            channel=args.query,
            port=args.port,
            as_json=args.json,
        )
        raise SystemExit(exit_code)

    # ── Legacy --doctor flag ──
    if args.doctor:
        exit_code = _run_async(run_doctor())
        raise SystemExit(exit_code)

    # Fail fast if optional deps are missing for the chosen mode
    _check_extras_installed(args)

    settings = get_settings()

    # Push unified PocketPaw env vars so backends see the correct API keys
    # regardless of which backend is selected. This fixes the issue where
    # switching backends required manually setting different env vars.
    from pocketpaw.llm.client import resolve_backend_env

    resolve_backend_env(settings)

    # Run startup health checks (non-blocking, informational only)
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
        if args.command == "serve":
            from pocketpaw.api.serve import run_api_server

            _serve(run_api_server, host=host, port=args.port, dev=args.dev)
        elif args.command == "status":
            from pocketpaw.cli.status import run_status

            exit_code = run_status(
                port=args.port,
                as_json=args.json,
                watch=args.watch,
            )
            raise SystemExit(exit_code)
        elif args.check_ollama:
            exit_code = _run_async(check_ollama(settings))
            raise SystemExit(exit_code)
        elif args.check_openai_compatible:
            exit_code = _run_async(check_openai_compatible(settings))
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
            # Default: web dashboard (also handles --web flag)
            _serve(run_dashboard_mode, settings, host=host, port=args.port, dev=args.dev)
    except KeyboardInterrupt:
        logger.info("PocketPaw stopped.")
    finally:
        # Coordinated singleton shutdown
        from pocketpaw.lifecycle import shutdown_all

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(shutdown_all())
            loop.close()
        except (RuntimeError, OSError):
            # RuntimeError: event loop already closed (common on Windows)
            # OSError: socket/fd cleanup errors during forced shutdown
            pass


if __name__ == "__main__":
    main()
