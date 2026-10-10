# ee/pocketpaw_ee/cloud/notifications/webhook_platforms.py
# Which chat app a site's lead-notification webhook URL belongs to, so the outbox
# can send that app's native message shape (``webhook_formats``). Pure: no I/O,
# no DNS. The frontend mirrors ``detect_platform`` for an instant badge while the
# owner types; this module is the authority.
#
# Detection is by host + path of an incoming-webhook URL:
#   hooks.slack.com/services/...                         -> slack
#   discord.com | discordapp.com (+ptb/canary)/api[/vN]/webhooks/... -> discord
#   *.webhook.office.com, *.logic.azure.com, *.powerplatform.com -> teams
#   chat.googleapis.com/v1/spaces/<space>/messages       -> google_chat
#   anything else (Zapier, Make, n8n, Pipedream, a CRM)  -> json
# Slack *workflow* webhooks (hooks.slack.com/workflows|triggers/...) take flat
# variables, not Block Kit, so they are json. The owner's override wins over the
# detected value (``effective_platform``). Only json is HMAC-signed: the chat
# apps can't verify a signature.

from __future__ import annotations

import re
from urllib.parse import urlparse

PLATFORMS: tuple[str, ...] = ("slack", "discord", "teams", "google_chat", "json")

PLATFORM_LABELS: dict[str, str] = {
    "slack": "Slack",
    "discord": "Discord",
    "teams": "Microsoft Teams",
    "google_chat": "Google Chat",
    "json": "JSON (Zapier, Make, n8n, CRM)",
}

_DISCORD_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "ptb.discord.com",
        "canary.discord.com",
        "ptb.discordapp.com",
        "canary.discordapp.com",
    }
)
_DISCORD_PATH = re.compile(r"^/api(?:/v\d+)?/webhooks/[^/]+/[^/]+")
_GOOGLE_CHAT_PATH = re.compile(r"^/v1/spaces/[^/]+/messages/?$")
_TEAMS_SUFFIXES = ("webhook.office.com", "logic.azure.com", "powerplatform.com")


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def detect_platform(url: str | None) -> str:
    """The platform an incoming-webhook URL belongs to; ``json`` when unknown."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return "json"
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or ""
    if host == "hooks.slack.com" and path.startswith("/services/"):
        return "slack"
    if host in _DISCORD_HOSTS and _DISCORD_PATH.match(path):
        return "discord"
    if any(_host_matches(host, s) for s in _TEAMS_SUFFIXES):
        return "teams"
    if host == "chat.googleapis.com" and _GOOGLE_CHAT_PATH.match(path):
        return "google_chat"
    return "json"


def effective_platform(url: str | None, override: str | None) -> str:
    """The owner's override when it names a known platform, else the detected one."""
    if override in PLATFORMS:
        return str(override)
    return detect_platform(url)


def is_signed(platform: str) -> bool:
    """Only the generic json envelope carries the X-Paw signature headers."""
    return platform == "json"


__all__ = [
    "PLATFORMS",
    "PLATFORM_LABELS",
    "detect_platform",
    "effective_platform",
    "is_signed",
]
