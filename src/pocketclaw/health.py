"""Health check diagnostics for PocketPaw.

Collects subsystem status and returns a structured health report.
Used by the dashboard API, Docker healthchecks, and the desktop launcher.

Created: 2026-02-12
"""

import platform
import sys
import time
from typing import Callable

from pocketclaw.agents.loop import AgentLoop
from pocketclaw.bus import get_message_bus
from pocketclaw.config import Settings
from pocketclaw.memory import get_memory_manager


def _get_version() -> str:
    """Get the package version from pocketclaw.__version__."""
    try:
        from pocketclaw import __version__

        return __version__
    except Exception:
        return "unknown"


def collect_health(
    agent_loop: AgentLoop,
    channel_status_fn: Callable[[str], bool],
    start_time: float,
) -> tuple[dict, int]:
    """Collect health diagnostics from all subsystems.

    Args:
        agent_loop: The global AgentLoop instance.
        channel_status_fn: Callable(channel_name) -> bool indicating if running.
        start_time: Monotonic timestamp from app startup.

    Returns:
        Tuple of (response_body, http_status_code).
    """
    settings = Settings.load()
    bus = get_message_bus()

    # --- Agent backend ---
    agent_ok = agent_loop.is_running()
    agent_info = {
        "status": "ok" if agent_ok else "down",
        "backend": settings.agent_backend,
    }

    # --- Memory ---
    try:
        mem = get_memory_manager()
        session_count = mem.get_session_count()
        memory_info = {
            "status": "ok",
            "backend": settings.memory_backend,
            "sessions": session_count,
        }
    except Exception:
        memory_info = {"status": "error", "backend": settings.memory_backend, "sessions": 0}

    # --- Message bus ---
    bus_info = {"status": "ok", "subscribers": bus.get_subscriber_count()}

    # --- Channels ---
    channels_info = {}
    for ch in ("telegram", "discord", "slack", "whatsapp"):
        channels_info[ch] = "connected" if channel_status_fn(ch) else "disconnected"

    # --- Overall status ---
    # Agent loop + memory are critical (503).  Channel failures degrade (200).
    if not agent_ok or memory_info["status"] == "error":
        overall = "unhealthy"
    elif any(v == "disconnected" for v in channels_info.values()):
        overall = "degraded"
    else:
        overall = "healthy"

    body = {
        "status": overall,
        "version": _get_version(),
        "uptime_seconds": round(time.monotonic() - start_time),
        "subsystems": {
            "agent_backend": agent_info,
            "memory": memory_info,
            "message_bus": bus_info,
            "channels": channels_info,
        },
        "python_version": platform.python_version(),
        "platform": sys.platform,
    }

    status_code = 503 if overall == "unhealthy" else 200
    return body, status_code
