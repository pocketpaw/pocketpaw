# CLI channels command - list, start, stop channel adapters.

from __future__ import annotations

from pocketpaw.cli.utils import (
    BOLD,
    DIM,
    GREEN,
    RESET,
    output_json,
    print_fail,
    print_header,
)

# All known channels
_ALL_CHANNELS = [
    "discord",
    "slack",
    "whatsapp",
    "telegram",
    "signal",
    "matrix",
    "teams",
    "google_chat",
]


def run_channels_cmd(
    action: str | None = None,
    channel: str | None = None,
    port: int = 8888,
    as_json: bool = False,
) -> int:
    """Manage channel adapters.

    - No action: list all channels with status
    - start <channel>: start via REST API
    - stop <channel>: stop via REST API
    """
    if action in ("start", "stop"):
        if not channel:
            print_fail(f"Usage: pocketpaw channels {action} <channel-name>")
            return 1
        return _toggle_channel(channel, action, port)

    return _list_channels(as_json)


def _list_channels(as_json: bool) -> int:
    """List all channels with configured/running status."""
    from pocketpaw.config import get_settings

    settings = get_settings()

    rows = []
    for ch in _ALL_CHANNELS:
        configured = _is_configured(ch, settings)
        autostart = _get_autostart(ch, settings)
        rows.append(
            {
                "channel": ch,
                "configured": configured,
                "autostart": autostart,
            }
        )

    if as_json:
        output_json(rows)
        return 0

    print_header("Channels")
    print(f"  {'CHANNEL':<14} {'CONFIGURED':<14} {'AUTOSTART'}")
    print(f"  {'─' * 42}")
    for r in rows:
        name = r["channel"]
        cfg = f"{GREEN}yes{RESET}" if r["configured"] else f"{DIM}no{RESET}"
        auto = f"{GREEN}yes{RESET}" if r["autostart"] else f"{DIM}no{RESET}"
        print(f"  {name:<14} {cfg:<23} {auto}")
    print()
    print(f"  {DIM}To start/stop a channel on a running instance:{RESET}")
    print(f"  {DIM}  pocketpaw channels start discord{RESET}")
    print(f"  {DIM}  pocketpaw channels stop slack{RESET}\n")
    return 0


def _toggle_channel(channel: str, action: str, port: int) -> int:
    """Start or stop a channel via the dashboard REST API."""
    import httpx

    if channel not in _ALL_CHANNELS:
        print_fail(f"Unknown channel '{channel}'. Available: {', '.join(_ALL_CHANNELS)}")
        return 1

    enable = action == "start"
    url = f"http://localhost:{port}/api/channels/toggle"

    try:
        resp = httpx.post(url, json={"channel": channel, "enable": enable}, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "unknown")
        if status == "ok" or data.get("running") == enable:
            verb = "Started" if enable else "Stopped"
            print(f"  {GREEN}{verb}{RESET} {BOLD}{channel}{RESET}")
        else:
            msg = data.get("error") or data.get("message") or str(data)
            print_fail(f"Could not {action} {channel}: {msg}")
            return 1
    except httpx.ConnectError:
        print_fail(f"Cannot connect to PocketPaw at localhost:{port}. Is the dashboard running?")
        return 1
    except httpx.HTTPStatusError as e:
        print_fail(f"API error: {e.response.status_code} - {e.response.text}")
        return 1
    except Exception as e:
        print_fail(f"Error: {e}")
        return 1

    return 0


def _is_configured(channel: str, settings) -> bool:
    """Check if a channel has its required config set."""
    required = {
        "discord": "discord_bot_token",
        "slack": "slack_bot_token",
        "whatsapp": "whatsapp_access_token",
        "telegram": "telegram_bot_token",
        "signal": "signal_phone_number",
        "matrix": "matrix_homeserver",
        "teams": "teams_app_id",
        "google_chat": "google_chat_project_id",
    }
    field = required.get(channel)
    if not field:
        return False
    return bool(getattr(settings, field, None))


def _get_autostart(channel: str, settings) -> bool:
    """Check if autostart is enabled for a channel."""
    field = f"{channel}_autostart"
    return bool(getattr(settings, field, False))
