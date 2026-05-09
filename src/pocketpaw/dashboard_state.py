"""Shared mutable state for the PocketPaw web dashboard.

Extracted from dashboard.py to prevent circular imports when dashboard
sub-modules (channels, auth, lifecycle, ws) need access to the same globals.
"""

import asyncio
import importlib
from typing import Any

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.bus.adapters.websocket_adapter import WebSocketAdapter
from pocketpaw.bus.commands import get_command_handler as _get_cmd_handler
from pocketpaw.config import Settings
from pocketpaw.status import StatusTracker

try:
    from fastapi import WebSocket
except ImportError:
    WebSocket = object  # type: ignore[assignment,misc]


# ── Singletons ──────────────────────────────────────────────────────────────

ws_adapter = WebSocketAdapter()
agent_loop = AgentLoop()
status_tracker = StatusTracker()

# Wire up the agent loop so /kill can cancel in-flight sessions
_get_cmd_handler().set_agent_loop(agent_loop)

# Per-agent AgentLoop registry — cached on first use, keyed by cloud agent id.
# These loops don't consume from the bus (unlike the default ``agent_loop``);
# they're invoked directly via ``process_message`` from the /chat/stream
# handler when a request targets a specific cloud agent.
#
# Values hold the AgentLoop plus the Agent doc's ``updatedAt`` at build time
# so we can detect staleness and rebuild when the operator edits the agent
# in Mongo. A sentinel ``_NOT_FOUND`` is cached for agent ids whose doc
# lookup returned ``None`` — avoids pounding Mongo with Agent.get() on every
# message for a deleted agent.


class _NotFound:
    """Sentinel entry for agent ids that don't resolve to a Mongo doc."""


_NOT_FOUND = _NotFound()
_agent_loops: dict[str, AgentLoop | _NotFound] = {}
_agent_loop_stamps: dict[str, Any] = {}  # agent_id -> Agent.updatedAt snapshot
_agent_loops_lock = asyncio.Lock()


async def get_agent_loop_for(agent_id: str) -> AgentLoop:
    """Fetch-or-build the per-agent AgentLoop for ``agent_id``.

    Falls back to the default singleton loop when the agent doc can't be
    loaded (e.g. Beanie/Mongo not initialised, or the id doesn't exist)
    so callers never hit a hard failure — the default loop will still
    produce *some* response, just without the per-agent persona/backend
    override.

    Cache invalidation mirrors ``AgentPool``: when the Agent doc's
    ``updatedAt`` advances past the value we stored at build time, the
    cached loop is discarded and rebuilt. Negative results (doc missing)
    are cached as ``_NOT_FOUND`` to keep repeated lookups out of Mongo.
    """
    from beanie import PydanticObjectId

    from ee.cloud.models.agent import Agent

    async with _agent_loops_lock:
        try:
            doc = await Agent.get(PydanticObjectId(agent_id))
        except Exception:
            # Transient DB error — don't poison the cache; use the default
            # loop for this request and retry lookup on the next one.
            return agent_loop

        if doc is None:
            _agent_loops[agent_id] = _NOT_FOUND
            _agent_loop_stamps.pop(agent_id, None)
            return agent_loop

        cached = _agent_loops.get(agent_id)
        stamp = _agent_loop_stamps.get(agent_id)
        fresh = (
            isinstance(cached, AgentLoop)
            and stamp is not None
            and doc.updatedAt is not None
            and doc.updatedAt <= stamp
        )
        if fresh:
            return cached  # type: ignore[return-value]

        loop = AgentLoop(
            agent_id=agent_id,
            agent_name=doc.name,
            agent_config=dict(doc.config or {}),
        )
        _agent_loops[agent_id] = loop
        _agent_loop_stamps[agent_id] = doc.updatedAt
        return loop


def iter_per_agent_loops() -> list[AgentLoop]:
    """Snapshot of live per-agent loops for cross-loop ops (cancel, stop).

    Skips the ``_NOT_FOUND`` sentinel so callers only ever see real loops.
    Reads the dict synchronously — safe under asyncio's single-threaded
    scheduling, and avoids forcing every caller to hold ``_agent_loops_lock``
    for a read-only snapshot.
    """
    return [v for v in _agent_loops.values() if isinstance(v, AgentLoop)]


# Retain active_connections for legacy broadcasts until fully migrated
active_connections: list[WebSocket] = []

# Channel adapters (auto-started when configured, keyed by channel name)
_channel_adapters: dict[str, object] = {}

# Protects settings read-modify-write from concurrent WebSocket clients
_settings_lock = asyncio.Lock()

# Set by run_dashboard() so the startup event can open the browser once the server is ready
_open_browser_url: str | None = None

# Global state for Telegram pairing
_telegram_pairing_state: dict = {
    "session_secret": None,
    "paired": False,
    "user_id": None,
    "temp_bot_app": None,
}


# ── Config lookup dicts ─────────────────────────────────────────────────────

# Maps channel config keys from the frontend to Settings field names
_CHANNEL_CONFIG_KEYS: dict[str, dict[str, str]] = {
    "discord": {
        "bot_token": "discord_bot_token",
        "allowed_guild_ids": "discord_allowed_guild_ids",
        "allowed_user_ids": "discord_allowed_user_ids",
        "allowed_channel_ids": "discord_allowed_channel_ids",
        "conversation_channel_ids": "discord_conversation_channel_ids",
        "conversation_all_channels": "discord_conversation_all_channels",
        "conversation_exclude_channel_ids": "discord_conversation_exclude_channel_ids",
        "bot_name": "discord_bot_name",
        "status_type": "discord_status_type",
        "activity_type": "discord_activity_type",
        "activity_text": "discord_activity_text",
    },
    "slack": {
        "bot_token": "slack_bot_token",
        "app_token": "slack_app_token",
        "allowed_channel_ids": "slack_allowed_channel_ids",
    },
    "whatsapp": {
        "mode": "whatsapp_mode",
        "neonize_db": "whatsapp_neonize_db",
        "access_token": "whatsapp_access_token",
        "phone_number_id": "whatsapp_phone_number_id",
        "verify_token": "whatsapp_verify_token",
        "allowed_phone_numbers": "whatsapp_allowed_phone_numbers",
    },
    "telegram": {
        "bot_token": "telegram_bot_token",
        "allowed_user_id": "allowed_user_id",
    },
    "signal": {
        "api_url": "signal_api_url",
        "phone_number": "signal_phone_number",
        "allowed_phone_numbers": "signal_allowed_phone_numbers",
    },
    "matrix": {
        "homeserver": "matrix_homeserver",
        "user_id": "matrix_user_id",
        "access_token": "matrix_access_token",
        "password": "matrix_password",
        "allowed_room_ids": "matrix_allowed_room_ids",
        "device_id": "matrix_device_id",
    },
    "teams": {
        "app_id": "teams_app_id",
        "app_password": "teams_app_password",
        "allowed_tenant_ids": "teams_allowed_tenant_ids",
        "webhook_port": "teams_webhook_port",
    },
    "google_chat": {
        "mode": "gchat_mode",
        "service_account_key": "gchat_service_account_key",
        "project_id": "gchat_project_id",
        "subscription_id": "gchat_subscription_id",
        "allowed_space_ids": "gchat_allowed_space_ids",
    },
}

# Required fields per channel (at least these must be set to start the adapter)
_CHANNEL_REQUIRED: dict[str, list[str]] = {
    "discord": ["discord_bot_token"],
    "slack": ["slack_bot_token", "slack_app_token"],
    "whatsapp": ["whatsapp_access_token", "whatsapp_phone_number_id"],
    "telegram": ["telegram_bot_token"],
    "signal": ["signal_phone_number"],
    "matrix": ["matrix_homeserver", "matrix_user_id"],
    "teams": ["teams_app_id", "teams_app_password"],
    "google_chat": ["gchat_service_account_key"],
}

# Maps channel name → (import_module, display_package, pip_spec)
_CHANNEL_DEPS: dict[str, tuple[str, str, str]] = {
    "discord": ("discli", "discord-cli-agent", "pocketpaw[discord]"),
    "slack": ("slack_bolt", "slack-bolt", "pocketpaw[slack]"),
    "whatsapp": ("neonize", "neonize", "pocketpaw[whatsapp-personal]"),
    "telegram": ("telegram.ext", "python-telegram-bot", "pocketpaw[telegram]"),
    "matrix": ("nio", "matrix-nio", "pocketpaw[matrix]"),
    "teams": ("botbuilder.core", "botbuilder-core", "pocketpaw[teams]"),
    "google_chat": ("googleapiclient.discovery", "google-api-python-client", "pocketpaw[gchat]"),
    "graph": ("networkx", "networkx", "pocketpaw[graph]"),
}

_MEMORY_CONFIG_KEYS = {
    "memory_backend": "memory_backend",
    "memory_use_inference": "memory_use_inference",
    "file_vector_enabled": "file_vector_enabled",
    "vector_store": "vector_store",
    "embedding_provider": "embedding_provider",
    "embedding_model": "embedding_model",
    "embedding_base_url": "embedding_base_url",
    "mem0_llm_provider": "mem0_llm_provider",
    "mem0_llm_model": "mem0_llm_model",
    "mem0_embedder_provider": "mem0_embedder_provider",
    "mem0_embedder_model": "mem0_embedder_model",
    "mem0_vector_store": "mem0_vector_store",
    "mem0_ollama_base_url": "mem0_ollama_base_url",
    "mem0_auto_learn": "mem0_auto_learn",
}

# OAuth scopes per service
_OAUTH_SCOPES: dict[str, list[str]] = {
    "google_gmail": [
        "https://mail.google.com/",
    ],
    "google_calendar": [
        "https://www.googleapis.com/auth/calendar",
    ],
    "google_drive": [
        "https://www.googleapis.com/auth/drive",
    ],
    "google_docs": [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
    "spotify": [
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-currently-playing",
        "playlist-read-private",
        "playlist-modify-public",
        "playlist-modify-private",
    ],
}

_LOCALHOST_ADDRS = {"127.0.0.1", "localhost", "::1"}
_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for")


# ── Helper functions ────────────────────────────────────────────────────────


def _channel_autostart_enabled(channel: str, settings: Settings) -> bool:
    """Check if a channel should auto-start on dashboard launch.

    Missing keys default to True for backward compatibility.
    """
    return settings.channel_autostart.get(channel, True)


def _channel_is_configured(channel: str, settings: Settings) -> bool:
    """Check if a channel has its required fields set."""
    # Personal mode WhatsApp needs no tokens — just start and scan QR
    if channel == "whatsapp" and settings.whatsapp_mode == "personal":
        return True
    for field in _CHANNEL_REQUIRED.get(channel, []):
        if not getattr(settings, field, None):
            return False
    return True


def _channel_is_running(channel: str) -> bool:
    """Check if a channel adapter is currently running."""
    adapter = _channel_adapters.get(channel)
    if adapter is None:
        return False
    return getattr(adapter, "_running", False)


def _is_module_importable(module_name: str) -> bool:
    """Check if a module can actually be imported (not just found on disk).

    ``find_spec`` only checks whether a module file exists — it doesn't verify
    that the module loads without errors.  A real import is the only reliable
    test, especially for packages with native extensions or heavy transitive
    dependencies like ``python-telegram-bot``.
    """
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False
