# CLI errors command - show recent errors from the health engine.

from __future__ import annotations

from pocketpaw.cli.utils import BOLD, DIM, RED, RESET, YELLOW, output_json, print_header


def run_errors_cmd(
    limit: int = 20,
    search: str | None = None,
    as_json: bool = False,
) -> int:
    """Show recent errors from the health engine error store."""
    from pocketpaw.health import get_health_engine

    engine = get_health_engine()
    errors = engine.get_recent_errors(limit=limit, search=search or "")

    if as_json:
        output_json(errors)
        return 0

    title = "Recent Errors"
    if search:
        title += f" (filter: '{search}')"
    print_header(title)

    if not errors:
        print(f"  {DIM}No errors found.{RESET}\n")
        return 0

    for err in errors:
        severity = err.get("severity", "error")
        timestamp = err.get("timestamp", "")
        if isinstance(timestamp, str) and len(timestamp) > 19:
            timestamp = timestamp[:19]
        source = err.get("source", "unknown")
        message = err.get("message", "")

        if severity == "critical":
            icon = f"{RED}[CRIT]{RESET}"
        elif severity == "error":
            icon = f"{RED}[ERR]{RESET} "
        else:
            icon = f"{YELLOW}[WARN]{RESET}"

        print(f"  {icon} {DIM}{timestamp}{RESET} {BOLD}{source}{RESET}")
        print(f"         {message}")

        tb = err.get("traceback")
        if tb:
            # Show just the last line of traceback
            last_line = tb.strip().splitlines()[-1] if tb.strip() else ""
            if last_line:
                print(f"         {DIM}{last_line}{RESET}")
        print()

    return 0
