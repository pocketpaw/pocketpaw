# CLI logs command - tail the audit log.

from __future__ import annotations

import json
import time

from pocketpaw.cli.utils import BOLD, DIM, GREEN, RED, RESET, YELLOW, output_json, print_header


def run_logs_cmd(
    limit: int = 50,
    follow: bool = False,
    as_json: bool = False,
) -> int:
    """Show or tail the audit log at ~/.pocketpaw/audit.jsonl."""

    from pocketpaw.config import get_config_dir

    log_path = get_config_dir() / "audit.jsonl"

    if not log_path.exists():
        print(f"  {DIM}No audit log found at {log_path}{RESET}\n")
        return 0

    if follow:
        return _follow_log(log_path)

    return _show_log(log_path, limit, as_json)


def _show_log(log_path, limit: int, as_json: bool) -> int:
    """Show the last N audit log entries."""
    lines = _tail_lines(log_path, limit)
    entries = _parse_lines(lines)

    if as_json:
        output_json(entries)
        return 0

    print_header("Audit Log", f"last {len(entries)} entries from {log_path}")

    if not entries:
        print(f"  {DIM}Log is empty.{RESET}\n")
        return 0

    for entry in entries:
        _print_entry(entry)

    return 0


def _follow_log(log_path) -> int:
    """Tail the log file, printing new entries as they appear."""
    print(f"  {DIM}Tailing {log_path} (Ctrl+C to stop){RESET}\n")

    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            # Seek to end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    entry = _parse_line(line)
                    if entry:
                        _print_entry(entry)
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def _tail_lines(path, n: int) -> list[str]:
    """Read the last N lines of a file efficiently."""
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            lines = all_lines[-n:]
    except Exception:
        pass
    return lines


def _parse_lines(lines: list[str]) -> list[dict]:
    entries = []
    for line in lines:
        entry = _parse_line(line)
        if entry:
            entries.append(entry)
    return entries


def _parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _print_entry(entry: dict) -> None:
    """Print a single audit log entry with color."""
    ts = entry.get("timestamp", entry.get("ts", ""))
    if isinstance(ts, str) and len(ts) > 19:
        ts = ts[:19]

    event = entry.get("event", entry.get("type", entry.get("action", "unknown")))
    level = entry.get("level", entry.get("severity", "info"))

    # Color by level
    if level in ("error", "critical"):
        color = RED
    elif level == "warning":
        color = YELLOW
    elif level in ("ok", "success"):
        color = GREEN
    else:
        color = ""

    detail = entry.get("detail", entry.get("message", entry.get("data", "")))
    if isinstance(detail, dict):
        detail = json.dumps(detail, default=str)
    if isinstance(detail, str) and len(detail) > 100:
        detail = detail[:97] + "..."

    reset = RESET if color else ""
    print(f"  {DIM}{ts}{RESET} {color}{BOLD}{event}{reset}  {DIM}{detail}{RESET}")
