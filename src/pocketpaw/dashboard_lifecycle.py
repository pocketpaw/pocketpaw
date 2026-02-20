"""Dashboard lifecycle management — startup, shutdown, and broadcast helpers.

Extracted from dashboard.py — contains:
- ``broadcast_reminder()`` / ``broadcast_intention()`` — push to WS + notification channels
- ``_broadcast_audit_entry()`` / ``_broadcast_health_update()`` — WS-only broadcasts
- ``startup_event()`` — initializes bus, agent loop, channels, MCP, health, scheduler, daemon
- ``shutdown_event()`` — tears down all services
"""

import asyncio
import logging

import pocketpaw.dashboard_state as _state
from pocketpaw.bus import get_message_bus
from pocketpaw.config import Settings
from pocketpaw.daemon import get_daemon
from pocketpaw.dashboard_state import (
    _channel_adapters,
    _channel_autostart_enabled,
    active_connections,
    agent_loop,
    ws_adapter,
)
from pocketpaw.scheduler import get_scheduler
from pocketpaw.security import get_audit_logger
from pocketpaw.security.rate_limiter import cleanup_all

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------


async def broadcast_reminder(reminder: dict):
    """Broadcast a reminder notification to all connected clients."""
    # Use new adapter for broadcast
    await ws_adapter.broadcast(reminder, msg_type="reminder")

    # Legacy broadcast (backup)
    message = {"type": "reminder", "reminder": reminder}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            pass

    # Push to notification channels
    try:
        from pocketpaw.bus.notifier import notify

        await notify(f"Reminder: {reminder.get('text', '')}")
    except Exception:
        pass


async def broadcast_intention(intention_id: str, chunk: dict):
    """Broadcast intention execution results to all connected clients."""
    message = {"type": "intention_event", "intention_id": intention_id, **chunk}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)

    # Push message-type intention chunks to notification channels
    if chunk.get("type") == "message":
        try:
            from pocketpaw.bus.notifier import notify

            await notify(chunk.get("content", ""))
        except Exception:
            pass


async def _broadcast_audit_entry(entry: dict):
    """Broadcast a new audit log entry to all connected WebSocket clients."""
    message = {"type": "system_event", "event_type": "audit_entry", "data": entry}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)


async def _broadcast_health_update(summary: dict):
    """Broadcast health status update to all connected WebSocket clients."""
    message = {"type": "health_update", "data": summary}
    for ws in active_connections[:]:
        try:
            await ws.send_json(message)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def startup_event(
    *,
    _start_channel_adapter_fn=None,
):
    """Start services on app startup.

    Parameters
    ----------
    _start_channel_adapter_fn:
        Callable for starting a channel adapter. Injected from dashboard.py
        to avoid circular import with dashboard_channels.
    """
    # Start Message Bus Integration
    bus = get_message_bus()
    await ws_adapter.start(bus)

    # Start Agent Loop
    asyncio.create_task(agent_loop.start())
    logger.info("Agent Loop started")

    # Auto-start configured channel adapters (respects per-channel autostart setting)
    settings = Settings.load()
    if _start_channel_adapter_fn:
        for ch in (
            "discord",
            "slack",
            "whatsapp",
            "telegram",
            "signal",
            "matrix",
            "teams",
            "google_chat",
        ):
            if not _channel_autostart_enabled(ch, settings):
                logger.debug("Skipping %s auto-start (disabled in settings)", ch)
                continue
            try:
                if await _start_channel_adapter_fn(ch, settings):
                    logger.info(f"{ch.title()} adapter auto-started alongside dashboard")
            except Exception as e:
                logger.warning(f"Failed to auto-start {ch} adapter: {e}")

        # Auto-start webhook adapter if webhooks are configured
        if settings.webhook_configs:
            try:
                if await _start_channel_adapter_fn("webhook", settings):
                    count = len(settings.webhook_configs)
                    logger.info("Webhook adapter auto-started (%d slots)", count)
            except Exception as e:
                logger.warning("Failed to auto-start webhook adapter: %s", e)

    # Ensure project directories exist for all Deep Work projects
    try:
        from pocketpaw.mission_control.manager import get_mission_control_manager

        mc_manager = get_mission_control_manager()
        await mc_manager.ensure_project_directories()
    except Exception as e:
        logger.warning("Failed to ensure project directories: %s", e)

    # Recover Deep Work projects interrupted by previous shutdown
    try:
        from pocketpaw.deep_work import recover_interrupted_projects

        recovered = await recover_interrupted_projects()
        if recovered:
            logger.info("Recovered %d interrupted Deep Work project(s)", recovered)
    except Exception as e:
        logger.warning("Failed to recover interrupted projects: %s", e)

    # Wire MCP OAuth broadcast + auto-start enabled MCP servers (non-blocking)
    try:
        from pocketpaw.mcp.manager import get_mcp_manager, set_ws_broadcast

        async def _mcp_ws_broadcast(message: dict) -> None:
            """Broadcast an MCP message to all connected WebSocket clients."""
            for ws in active_connections[:]:
                try:
                    await ws.send_json(message)
                except Exception:
                    pass

        set_ws_broadcast(_mcp_ws_broadcast)

        mcp = get_mcp_manager()

        async def _start_mcp_background() -> None:
            """Start MCP servers in background so dashboard isn't blocked."""
            try:
                await mcp.start_enabled_servers()
            except Exception as exc:
                logger.warning("Failed to start MCP servers: %s", exc)

        asyncio.create_task(_start_mcp_background())
    except Exception as e:
        logger.warning("Failed to initialize MCP manager: %s", e)

    # Initialize health engine and run startup checks
    try:
        from pocketpaw.health import get_health_engine

        health_engine = get_health_engine()
        health_engine.run_startup_checks()
        # Fire connectivity checks in background (non-blocking)
        asyncio.create_task(health_engine.run_connectivity_checks())
        logger.info("Health engine initialized: %s", health_engine.overall_status)
    except Exception as e:
        logger.warning("Failed to initialize health engine: %s", e)

    # Register audit log callback for live updates
    audit_logger = get_audit_logger()
    audit_logger.on_log(lambda entry: asyncio.ensure_future(_broadcast_audit_entry(entry)))

    # Start reminder scheduler
    scheduler = get_scheduler()
    scheduler.start(callback=broadcast_reminder)

    # Start proactive daemon
    daemon = get_daemon()
    daemon.start(stream_callback=broadcast_intention)

    # Health heartbeat — periodic checks every 5 min, broadcast on status transitions
    try:
        from pocketpaw.health import get_health_engine

        _health_engine = get_health_engine()
        _prev_status = _health_engine.overall_status

        async def _health_heartbeat():
            nonlocal _prev_status
            try:
                _health_engine.run_startup_checks()
                await _health_engine.run_connectivity_checks()
                new_status = _health_engine.overall_status
                if new_status != _prev_status:
                    logger.info("Health status changed: %s -> %s", _prev_status, new_status)
                    _prev_status = new_status
                    await _broadcast_health_update(_health_engine.summary)
            except Exception as e:
                logger.warning("Health heartbeat error: %s", e)

        # Reuse the daemon's APScheduler
        daemon.trigger_engine.scheduler.add_job(
            _health_heartbeat,
            "interval",
            minutes=5,
            id="health_heartbeat",
            replace_existing=True,
        )
        logger.info("Health heartbeat registered (every 5 min)")
    except Exception as e:
        logger.warning("Failed to register health heartbeat: %s", e)

    # Hourly rate-limiter cleanup
    async def _rate_limit_cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            removed = cleanup_all()
            if removed:
                logger.debug("Rate limiter cleanup: removed %d stale entries", removed)

    asyncio.create_task(_rate_limit_cleanup_loop())

    # Open browser now that the server is actually listening
    if _state._open_browser_url:
        import webbrowser

        webbrowser.open(_state._open_browser_url)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def shutdown_event(*, _stop_channel_adapter_fn=None):
    """Stop services on app shutdown.

    Parameters
    ----------
    _stop_channel_adapter_fn:
        Callable for stopping a channel adapter. Injected from dashboard.py.
    """
    # Stop Agent Loop
    await agent_loop.stop()
    await ws_adapter.stop()

    # Stop all channel adapters
    if _stop_channel_adapter_fn:
        for channel in list(_channel_adapters):
            try:
                await _stop_channel_adapter_fn(channel)
            except Exception as e:
                logger.warning(f"Error stopping {channel} adapter: {e}")

    # Stop proactive daemon
    daemon = get_daemon()
    daemon.stop()

    # Stop reminder scheduler
    scheduler = get_scheduler()
    scheduler.stop()

    # Stop MCP servers
    try:
        from pocketpaw.mcp.manager import get_mcp_manager

        mcp = get_mcp_manager()
        await mcp.stop_all()
    except Exception as e:
        logger.warning("Error stopping MCP servers: %s", e)
