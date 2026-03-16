# CLI status command — queries the agent status API and prints results.
# Created: 2026-03-12

from __future__ import annotations

import json
import os
import sys
import time


def _get_status(port: int) -> dict | None:
    """Fetch agent status from the local API."""
    import httpx

    url = f"http://localhost:{port}/api/v1/agent/status"
    headers = {}
    key = os.environ.get("POCKETPAW_STATUS_API_KEY", "")
    if key:
        headers["X-Status-Key"] = key

    try:
        resp = httpx.get(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.ConnectError:
        return None
    except httpx.HTTPStatusError as e:
        print(f"Error: {e.response.status_code} - {e.response.text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _print_table(data: dict) -> None:
    """Print human-readable status table."""
    g = data["global"]
    sessions = data["sessions"]

    state_display = g["state"].upper()
    print()
    print("PocketPaw Status")
    print(f"  State:    {state_display}")
    print(f"  Sessions: {g['active_sessions']} / {g['max_concurrent']}")
    print(f"  Uptime:   {_format_duration(g['uptime_seconds'])}")

    if sessions:
        print()
        print("Active Sessions")
        print(f"  {'SESSION':<20} {'CHANNEL':<12} {'STATE':<18} {'TOOL':<12} {'DURATION'}")
        for s in sessions:
            title = (s.get("title") or s["session_id"])[:18]
            tool = s.get("tool_name") or "-"
            dur = _format_duration(s["duration_seconds"])
            state = s["state"]
            if state == "error":
                msg = s.get("error_message", "")
                state = f"error: {msg}"[:18]
            print(f"  {title:<20} {s['channel']:<12} {state:<18} {tool:<12} {dur}")
    print()


def run_status(port: int = 8888, as_json: bool = False, watch: float = 0) -> int:
    """Run the status command. Returns exit code."""
    if watch > 0:
        return _run_watch(port, as_json, watch)

    data = _get_status(port)
    if data is None:
        print(f"PocketPaw is not running (could not connect to localhost:{port})")
        return 1

    if as_json:
        print(json.dumps(data, indent=2))
    else:
        _print_table(data)
    return 0


def _run_watch(port: int, as_json: bool, interval: float) -> int:
    """Poll and redraw status at interval."""
    try:
        while True:
            # Clear screen
            print("\033[2J\033[H", end="")
            data = _get_status(port)
            if data is None:
                print(f"PocketPaw is not running (could not connect to localhost:{port})")
            elif as_json:
                print(json.dumps(data, indent=2))
            else:
                _print_table(data)
            print(f"[Refreshing every {interval}s | Ctrl+C to stop]")
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
