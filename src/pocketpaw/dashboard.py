"""PocketPaw Web Dashboard - API Server

Lightweight FastAPI server that serves the frontend and handles WebSocket communication.

Changes:
  - 2026-02-17: Health heartbeat — periodic checks every 5 min via APScheduler, broadcasts health_update on status transitions.
  - 2026-02-17: Health Engine API (GET /api/health, POST /api/health/check, WS get_health/run_health_check).
  - 2026-02-06: WebSocket auth via first message instead of URL query param; accept wss://.
  - 2026-02-06: Channel config REST API (GET /api/channels/status, POST save/toggle).
  - 2026-02-06: Refactored adapter storage to _channel_adapters dict; auto-start all configured.
  - 2026-02-06: Auto-start Discord/WhatsApp adapters alongside dashboard; WhatsApp webhook routes.
  - 2026-02-12: Call ensure_project_directories() on startup for migration.
  - 2026-02-12: handle_file_browse() accepts optional `context` param echoed in response for
    sidebar vs modal file routing.
  - 2026-02-12: Fixed handle_file_browse bug: filter hidden files BEFORE applying 50-item limit.
  - 2026-02-12: Added Deep Work API router at /api/deep-work/*.
  - 2026-02-05: Added Mission Control API router at /api/mission-control/*.
  - 2026-02-04: Added Telegram setup API endpoints (/api/telegram/status, /api/telegram/setup, /api/telegram/pairing-status).
  - 2026-02-03: Cleaned up duplicate imports, fixed duplicate save() calls.
  - 2026-02-02: Added agent status to get_settings response.
  - 2026-02-02: Enhanced logging to show which backend is processing requests.
"""

import asyncio
import base64
import io
import json
import logging
import uuid
from pathlib import Path

try:
    import qrcode
    import uvicorn
    from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
except ImportError as _exc:
    raise ImportError(
        "Dashboard dependencies (fastapi, uvicorn, qrcode, jinja2) are required "
        "but not installed. Reinstall with: pip install --upgrade pocketpaw"
    ) from _exc

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.bootstrap import DefaultBootstrapProvider
from pocketpaw.bus import get_message_bus
from pocketpaw.bus.adapters.websocket_adapter import WebSocketAdapter
from pocketpaw.config import Settings, get_access_token, get_config_path, regenerate_token
from pocketpaw.daemon import get_daemon
from pocketpaw.deep_work.api import router as deep_work_router
from pocketpaw.memory import MemoryType, get_memory_manager
from pocketpaw.mission_control.api import router as mission_control_router
from pocketpaw.scheduler import get_scheduler
from pocketpaw.security import get_audit_logger
from pocketpaw.security.rate_limiter import api_limiter, auth_limiter, cleanup_all, ws_limiter
from pocketpaw.security.session_tokens import create_session_token, verify_session_token
from pocketpaw.skills import SkillExecutor, get_skill_loader
from pocketpaw.tunnel import get_tunnel_manager

logger = logging.getLogger(__name__)


ws_adapter = WebSocketAdapter()
agent_loop = AgentLoop()
# Retain active_connections for legacy broadcasts until fully migrated
active_connections: list[WebSocket] = []

# Channel adapters (auto-started when configured, keyed by channel name)
_channel_adapters: dict[str, object] = {}

# Protects settings read-modify-write from concurrent WebSocket clients
_settings_lock = asyncio.Lock()

# Set by run_dashboard() so the startup event can open the browser once the server is ready
_open_browser_url: str | None = None

# Get frontend directory
FRONTEND_DIR = Path(__file__).parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"

# Initialize Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Create FastAPI app
app = FastAPI(title="PocketPaw Dashboard")

# CORS — restrict to localhost + Cloudflare tunnel subdomains
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
        r"|^https://[a-zA-Z0-9-]+\.trycloudflare\.com$"
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP: allow self + CDN + inline styles/scripts (required by Alpine.js/UnoCSS)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss: https://cdn.jsdelivr.net https://unpkg.com; "
        "frame-ancestors 'none'"
    )
    # HSTS only when accessed via HTTPS (tunnel or reverse proxy)
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Mount static files
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Mount Mission Control API router
app.include_router(mission_control_router, prefix="/api/mission-control")

# Mount Deep Work API router

app.include_router(deep_work_router, prefix="/api/deep-work")


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


async def _start_channel_adapter(channel: str, settings: Settings | None = None) -> bool:
    """Start a single channel adapter. Returns True on success."""
    if settings is None:
        settings = Settings.load()
    bus = get_message_bus()

    if channel == "discord":
        if not settings.discord_bot_token:
            return False
        from pocketpaw.bus.adapters.discord_adapter import DiscordAdapter

        adapter = DiscordAdapter(
            token=settings.discord_bot_token,
            allowed_guild_ids=settings.discord_allowed_guild_ids,
            allowed_user_ids=settings.discord_allowed_user_ids,
        )
        await adapter.start(bus)
        _channel_adapters["discord"] = adapter
        return True

    if channel == "slack":
        if not settings.slack_bot_token or not settings.slack_app_token:
            return False
        from pocketpaw.bus.adapters.slack_adapter import SlackAdapter

        adapter = SlackAdapter(
            bot_token=settings.slack_bot_token,
            app_token=settings.slack_app_token,
            allowed_channel_ids=settings.slack_allowed_channel_ids,
        )
        await adapter.start(bus)
        _channel_adapters["slack"] = adapter
        return True

    if channel == "whatsapp":
        mode = settings.whatsapp_mode

        if mode == "personal":
            from pocketpaw.bus.adapters.neonize_adapter import NeonizeAdapter

            db_path = settings.whatsapp_neonize_db or None
            adapter = NeonizeAdapter(db_path=db_path)
            await adapter.start(bus)
            _channel_adapters["whatsapp"] = adapter
            return True
        else:
            # Business mode (Cloud API)
            if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
                return False
            from pocketpaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter

            adapter = WhatsAppAdapter(
                access_token=settings.whatsapp_access_token,
                phone_number_id=settings.whatsapp_phone_number_id,
                verify_token=settings.whatsapp_verify_token or "",
                allowed_phone_numbers=settings.whatsapp_allowed_phone_numbers,
            )
            await adapter.start(bus)
            _channel_adapters["whatsapp"] = adapter
            return True

    if channel == "telegram":
        if not settings.telegram_bot_token:
            return False
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(
            token=settings.telegram_bot_token,
            allowed_user_id=settings.allowed_user_id,
        )
        await adapter.start(bus)
        _channel_adapters["telegram"] = adapter
        return True

    if channel == "signal":
        if not settings.signal_phone_number:
            return False
        from pocketpaw.bus.adapters.signal_adapter import SignalAdapter

        adapter = SignalAdapter(
            api_url=settings.signal_api_url,
            phone_number=settings.signal_phone_number,
            allowed_phone_numbers=settings.signal_allowed_phone_numbers,
        )
        await adapter.start(bus)
        _channel_adapters["signal"] = adapter
        return True

    if channel == "matrix":
        if not settings.matrix_homeserver or not settings.matrix_user_id:
            return False
        from pocketpaw.bus.adapters.matrix_adapter import MatrixAdapter

        adapter = MatrixAdapter(
            homeserver=settings.matrix_homeserver,
            user_id=settings.matrix_user_id,
            access_token=settings.matrix_access_token,
            password=settings.matrix_password,
            allowed_room_ids=settings.matrix_allowed_room_ids,
            device_id=settings.matrix_device_id,
        )
        await adapter.start(bus)
        _channel_adapters["matrix"] = adapter
        return True

    if channel == "teams":
        if not settings.teams_app_id or not settings.teams_app_password:
            return False
        from pocketpaw.bus.adapters.teams_adapter import TeamsAdapter

        adapter = TeamsAdapter(
            app_id=settings.teams_app_id,
            app_password=settings.teams_app_password,
            allowed_tenant_ids=settings.teams_allowed_tenant_ids,
            webhook_port=settings.teams_webhook_port,
        )
        await adapter.start(bus)
        _channel_adapters["teams"] = adapter
        return True

    if channel == "google_chat":
        if not settings.gchat_service_account_key:
            return False
        from pocketpaw.bus.adapters.gchat_adapter import GoogleChatAdapter

        adapter = GoogleChatAdapter(
            mode=settings.gchat_mode,
            service_account_key=settings.gchat_service_account_key,
            project_id=settings.gchat_project_id,
            subscription_id=settings.gchat_subscription_id,
            allowed_space_ids=settings.gchat_allowed_space_ids,
        )
        await adapter.start(bus)
        _channel_adapters["google_chat"] = adapter
        return True

    if channel == "webhook":
        from pocketpaw.bus.adapters.webhook_adapter import WebhookAdapter

        adapter = WebhookAdapter()
        await adapter.start(bus)
        _channel_adapters["webhook"] = adapter
        return True

    return False


async def _stop_channel_adapter(channel: str) -> bool:
    """Stop a single channel adapter. Returns True if it was running."""
    adapter = _channel_adapters.pop(channel, None)
    if adapter is None:
        return False
    await adapter.stop()
    return True


@app.on_event("startup")
async def startup_event():
    """Start services on app startup."""
    # Start Message Bus Integration
    bus = get_message_bus()
    await ws_adapter.start(bus)

    # Start Agent Loop
    asyncio.create_task(agent_loop.start())
    logger.info("Agent Loop started")

    # Auto-start all configured channel adapters
    settings = Settings.load()
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
        try:
            if await _start_channel_adapter(ch, settings):
                logger.info(f"{ch.title()} adapter auto-started alongside dashboard")
        except Exception as e:
            logger.warning(f"Failed to auto-start {ch} adapter: {e}")

    # Auto-start webhook adapter if webhooks are configured
    if settings.webhook_configs:
        try:
            if await _start_channel_adapter("webhook", settings):
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

    # Auto-start enabled MCP servers
    try:
        from pocketpaw.mcp.manager import get_mcp_manager

        mcp = get_mcp_manager()
        await mcp.start_enabled_servers()
    except Exception as e:
        logger.warning("Failed to start MCP servers: %s", e)

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
    if _open_browser_url:
        import webbrowser

        webbrowser.open(_open_browser_url)


@app.on_event("shutdown")
async def shutdown_event():
    """Stop services on app shutdown."""
    # Stop Agent Loop
    await agent_loop.stop()
    await ws_adapter.stop()

    # Stop all channel adapters
    for channel in list(_channel_adapters):
        try:
            await _stop_channel_adapter(channel)
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


# ==================== MCP Server API ====================


@app.get("/api/mcp/status")
async def get_mcp_status():
    """Get status of all configured MCP servers."""
    from pocketpaw.mcp.manager import get_mcp_manager

    mgr = get_mcp_manager()
    return mgr.get_server_status()


@app.post("/api/mcp/add")
async def add_mcp_server(request: Request):
    """Add a new MCP server configuration and optionally start it."""
    from pocketpaw.mcp.config import MCPServerConfig
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    config = MCPServerConfig(
        name=data.get("name", ""),
        transport=data.get("transport", "stdio"),
        command=data.get("command", ""),
        args=data.get("args", []),
        url=data.get("url", ""),
        env=data.get("env", {}),
        enabled=data.get("enabled", True),
    )
    if not config.name:
        raise HTTPException(status_code=400, detail="Server name is required")

    mgr = get_mcp_manager()
    mgr.add_server_config(config)

    # Auto-start if enabled
    if config.enabled:
        try:
            await mgr.start_server(config)
        except Exception as e:
            logger.warning("Failed to auto-start MCP server '%s': %s", config.name, e)

    return {"status": "ok"}


@app.post("/api/mcp/remove")
async def remove_mcp_server(request: Request):
    """Remove an MCP server config and stop it if running."""
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    name = data.get("name", "")

    mgr = get_mcp_manager()
    await mgr.stop_server(name)
    removed = mgr.remove_server_config(name)
    if not removed:
        return {"error": f"Server '{name}' not found"}
    return {"status": "ok"}


@app.post("/api/mcp/toggle")
async def toggle_mcp_server(request: Request):
    """Toggle an MCP server: start if stopped/disconnected, stop if running."""
    from pocketpaw.mcp.config import load_mcp_config
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    name = data.get("name", "")

    mgr = get_mcp_manager()
    status = mgr.get_server_status()
    server_info = status.get(name)

    if server_info is None:
        return {"error": f"Server '{name}' not found"}

    if server_info["connected"]:
        # Running → stop and disable
        mgr.toggle_server_config(name)  # enabled → False
        await mgr.stop_server(name)
        return {"status": "ok", "enabled": False}
    else:
        # Not connected → ensure enabled and (re)start
        configs = load_mcp_config()
        config = next((c for c in configs if c.name == name), None)
        if not config:
            return {"error": f"No config found for '{name}'"}
        if not config.enabled:
            mgr.toggle_server_config(name)  # disabled → enabled
        connected = await mgr.start_server(config)
        return {"status": "ok", "enabled": True, "connected": connected}


@app.post("/api/mcp/test")
async def test_mcp_server(request: Request):
    """Test an MCP server connection and return discovered tools."""
    from pocketpaw.mcp.config import MCPServerConfig
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    config = MCPServerConfig(
        name=data.get("name", "test"),
        transport=data.get("transport", "stdio"),
        command=data.get("command", ""),
        args=data.get("args", []),
        url=data.get("url", ""),
        env=data.get("env", {}),
    )

    mgr = get_mcp_manager()
    success = await mgr.start_server(config)
    if not success:
        status = mgr.get_server_status().get(config.name, {})
        return {"connected": False, "error": status.get("error", "Unknown error"), "tools": []}

    tools = mgr.discover_tools(config.name)
    # Stop the test server
    await mgr.stop_server(config.name)
    return {
        "connected": True,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }


# ==================== MCP Preset Routes ====================


@app.get("/api/mcp/presets")
async def list_mcp_presets():
    """Return all MCP presets with installed flag."""
    from pocketpaw.mcp.config import load_mcp_config
    from pocketpaw.mcp.presets import get_all_presets

    installed_names = {c.name for c in load_mcp_config()}
    presets = get_all_presets()
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "icon": p.icon,
            "category": p.category,
            "package": p.package,
            "transport": p.transport,
            "url": p.url,
            "docs_url": p.docs_url,
            "needs_args": p.needs_args,
            "installed": p.id in installed_names,
            "env_keys": [
                {
                    "key": e.key,
                    "label": e.label,
                    "required": e.required,
                    "placeholder": e.placeholder,
                    "secret": e.secret,
                }
                for e in p.env_keys
            ],
        }
        for p in presets
    ]


@app.post("/api/mcp/presets/install")
async def install_mcp_preset(request: Request):
    """Install an MCP preset by ID with user-supplied env vars."""
    from fastapi.responses import JSONResponse

    from pocketpaw.mcp.manager import get_mcp_manager
    from pocketpaw.mcp.presets import get_preset, preset_to_config

    data = await request.json()
    preset_id = data.get("preset_id", "")
    env = data.get("env", {})
    extra_args = data.get("extra_args", None)

    preset = get_preset(preset_id)
    if not preset:
        return JSONResponse({"error": f"Unknown preset: {preset_id}"}, status_code=404)

    # Validate required env keys
    missing = [ek.key for ek in preset.env_keys if ek.required and not env.get(ek.key)]
    if missing:
        return JSONResponse(
            {"error": f"Missing required env vars: {', '.join(missing)}"},
            status_code=400,
        )

    config = preset_to_config(preset, env=env, extra_args=extra_args)
    mgr = get_mcp_manager()
    mgr.add_server_config(config)
    connected = await mgr.start_server(config)
    tools = mgr.discover_tools(config.name) if connected else []

    return {
        "status": "ok",
        "connected": connected,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }


# ==================== MCP Registry API ====================

_MCP_REGISTRY_BASE = "https://registry.modelcontextprotocol.io"

# Server name parts that are too generic to use alone as a config name.
_GENERIC_SERVER_PARTS = {"mcp", "server", "mcp-server", "main", "app", "api"}


def _derive_registry_short_name(raw_name: str, title: str | None = None) -> str:
    """Derive a short, readable config name from a registry server name.

    Examples:
        "com.zomato/mcp"      -> "zomato-mcp"
        "acme/weather-server"  -> "weather-server"
        "@anthropic/claude"    -> "claude"
        "simple-tool"          -> "simple-tool"
    """
    if not raw_name:
        return ""

    if "/" not in raw_name:
        return raw_name

    parts = raw_name.split("/")
    org = parts[0]
    server_part = parts[-1]

    # Clean up org: "com.zomato" -> "zomato", "@anthropic" -> "anthropic"
    if "." in org:
        org = org.rsplit(".", 1)[-1]
    org = org.lstrip("@")

    # If the server part is too generic, combine with org for disambiguation
    if server_part.lower() in _GENERIC_SERVER_PARTS:
        return f"{org}-{server_part}"

    return server_part


@app.get("/api/mcp/registry/search")
async def search_mcp_registry(
    q: str = "",
    limit: int = 30,
    cursor: str = "",
):
    """Proxy search to the official MCP Registry (avoids CORS)."""
    import httpx

    params: dict[str, str | int] = {"limit": min(limit, 100)}
    if q:
        params["search"] = q
    if cursor:
        params["cursor"] = cursor

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_MCP_REGISTRY_BASE}/v0/servers",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            # Registry wraps each entry as {server: {...}, _meta: {...}}.
            # Unwrap so the frontend gets flat server objects.
            # Also: lift environmentVariables from packages[0] to server
            # level, remove $schema ($ prefix can confuse Alpine.js proxies),
            # and ensure expected fields have defaults.
            servers = []
            raw_entries = data.get("servers", [])
            if not isinstance(raw_entries, list):
                raw_entries = []

            for entry in raw_entries:
                if not isinstance(entry, dict):
                    continue

                raw_server = entry.get("server", entry)
                if not isinstance(raw_server, dict):
                    continue

                srv = dict(raw_server)
                meta = entry.get("_meta", srv.get("_meta", {}))
                srv["_meta"] = meta if isinstance(meta, dict) else {}
                srv.pop("$schema", None)

                name = srv.get("name")
                description = srv.get("description")
                packages = srv.get("packages")
                remotes = srv.get("remotes")
                env_vars = srv.get("environmentVariables")

                srv["name"] = name if isinstance(name, str) else ""
                srv["description"] = description if isinstance(description, str) else ""
                srv["packages"] = packages if isinstance(packages, list) else []
                srv["remotes"] = remotes if isinstance(remotes, list) else []
                srv["environmentVariables"] = env_vars if isinstance(env_vars, list) else []

                # Lift env vars from the first package to the server level.
                if not srv["environmentVariables"]:
                    for pkg in srv["packages"]:
                        if not isinstance(pkg, dict):
                            continue
                        pkg_env = pkg.get("environmentVariables")
                        if isinstance(pkg_env, list) and pkg_env:
                            srv["environmentVariables"] = pkg_env
                            break

                # Skip entries without a valid name.
                if srv["name"]:
                    servers.append(srv)

            metadata = data.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            if "nextCursor" not in metadata and "next_cursor" in metadata:
                metadata["nextCursor"] = metadata["next_cursor"]
            metadata.setdefault("count", len(servers))

            return {"servers": servers, "metadata": metadata}
    except Exception as exc:
        logger.warning("MCP registry search failed: %s", exc)
        return {"servers": [], "metadata": {"count": 0}, "error": str(exc)}


@app.post("/api/mcp/registry/install")
async def install_from_registry(request: Request):
    """Install an MCP server from registry metadata.

    Expects a JSON body with the server's registry data (name, packages/remotes,
    environmentVariables) and user-supplied env values.
    """
    from fastapi.responses import JSONResponse

    from pocketpaw.mcp.config import MCPServerConfig
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    server = data.get("server", {})
    user_env = data.get("env", {})

    # Derive a short, readable name from the registry name.
    # e.g. "com.zomato/mcp" -> "zomato-mcp", "acme/weather-server" -> "weather-server"
    raw_name = server.get("name", "")
    short_name = _derive_registry_short_name(raw_name, server.get("title"))
    if not short_name:
        return JSONResponse({"error": "Missing server name"}, status_code=400)

    # Try remotes first (HTTP transport — simplest, no npm needed)
    remotes = server.get("remotes", [])
    packages = server.get("packages", [])

    config = None

    if remotes:
        remote = remotes[0]
        # Registry API uses "type" (e.g. "streamable-http"), legacy uses "transportType"
        transport = remote.get("type", remote.get("transportType", "http"))
        # Normalize SSE to "http" but keep "streamable-http" distinct — they need
        # different MCP SDK clients.
        if transport == "sse":
            transport = "http"
        elif transport not in ("http", "streamable-http"):
            transport = "http"  # safe fallback
        config = MCPServerConfig(
            name=short_name,
            transport=transport,
            url=remote.get("url", ""),
            env=user_env,
            enabled=True,
        )
    elif packages:
        pkg = packages[0]
        registry_type = pkg.get("registryType", "")
        pkg_name = pkg.get("name", "") or pkg.get("identifier", "")
        runtime = pkg.get("runtime", "node")

        if registry_type == "docker":
            args = ["run", "-i", "--rm"]
            for ra in pkg.get("runtimeArguments", []):
                if ra.get("isFixed"):
                    args.append(ra.get("value", ""))
            args.append(pkg_name)
            config = MCPServerConfig(
                name=short_name,
                transport="stdio",
                command="docker",
                args=args,
                env=user_env,
                enabled=True,
            )
        elif registry_type == "pypi":
            config = MCPServerConfig(
                name=short_name,
                transport="stdio",
                command="uvx",
                args=[pkg_name],
                env=user_env,
                enabled=True,
            )
        elif registry_type == "npm" or runtime == "node":
            args = ["-y", pkg_name]
            for pa in pkg.get("packageArguments", []):
                if pa.get("isFixed"):
                    args.append(pa.get("value", ""))
            config = MCPServerConfig(
                name=short_name,
                transport="stdio",
                command="npx",
                args=args,
                env=user_env,
                enabled=True,
            )

    if config is None:
        return JSONResponse(
            {"error": "Could not determine install method from registry data"},
            status_code=400,
        )

    mgr = get_mcp_manager()
    mgr.add_server_config(config)
    connected = await mgr.start_server(config)
    tools = mgr.discover_tools(config.name) if connected else []

    result: dict = {
        "status": "ok",
        "name": config.name,
        "connected": connected,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }
    # Surface connection error so the frontend can display it
    if not connected:
        status = mgr.get_server_status()
        srv = status.get(config.name, {})
        if srv.get("error"):
            result["error"] = srv["error"]
    return result


# ==================== Skills Library API ====================


@app.get("/api/skills")
async def list_installed_skills():
    """List all installed user-invocable skills."""
    loader = get_skill_loader()
    loader.reload()
    return [
        {
            "name": s.name,
            "description": s.description,
            "argument_hint": s.argument_hint,
        }
        for s in loader.get_invocable()
    ]


@app.get("/api/skills/search")
async def search_skills_library(q: str = "", limit: int = 30):
    """Proxy search to skills.sh API (avoids CORS for browsers)."""
    import httpx

    if not q:
        return {"skills": [], "count": 0}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://skills.sh/api/search",
                params={"q": q, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("skills.sh search failed: %s", exc)
        return {"skills": [], "count": 0, "error": str(exc)}


@app.post("/api/skills/install")
async def install_skill(request: Request):
    """Install a skill by cloning its GitHub repo and copying the skill directory."""
    import shutil
    import tempfile
    from pathlib import Path

    from fastapi.responses import JSONResponse

    data = await request.json()
    source = data.get("source", "").strip()
    if not source:
        return JSONResponse({"error": "Missing 'source' field"}, status_code=400)

    if ".." in source or ";" in source or "|" in source or "&" in source:
        return JSONResponse({"error": "Invalid source format"}, status_code=400)

    parts = source.split("/")
    if len(parts) < 2:
        return JSONResponse(
            {"error": "Source must be owner/repo or owner/repo/skill"}, status_code=400
        )

    owner, repo = parts[0], parts[1]
    skill_name = parts[2] if len(parts) >= 3 else None

    install_dir = Path.home() / ".agents" / "skills"
    install_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth=1",
                f"https://github.com/{owner}/{repo}.git",
                tmpdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                return JSONResponse({"error": f"Clone failed: {err}"}, status_code=500)

            tmp = Path(tmpdir)

            # Find skill directories containing SKILL.md.
            # Repos may store skills at root level or inside a skills/ subdirectory.
            skill_dirs: list[tuple[str, Path]] = []

            if skill_name:
                for candidate in [tmp / skill_name, tmp / "skills" / skill_name]:
                    if (candidate / "SKILL.md").exists():
                        skill_dirs.append((skill_name, candidate))
                        break
            else:
                for scan_dir in [tmp, tmp / "skills"]:
                    if not scan_dir.is_dir():
                        continue
                    for item in sorted(scan_dir.iterdir()):
                        if item.is_dir() and (item / "SKILL.md").exists():
                            skill_dirs.append((item.name, item))

            if not skill_dirs:
                return JSONResponse(
                    {"error": f"No SKILL.md found for '{skill_name or source}'"},
                    status_code=404,
                )

            installed = []
            for name, src_dir in skill_dirs:
                dest = install_dir / name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src_dir, dest)
                installed.append(name)

            loader = get_skill_loader()
            loader.reload()
            return {"status": "ok", "installed": installed}

    except TimeoutError:
        return JSONResponse({"error": "Clone timed out (30s)"}, status_code=504)
    except Exception as exc:
        logger.exception("Skill install failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/skills/remove")
async def remove_skill(request: Request):
    """Remove an installed skill by deleting its directory."""
    import shutil
    from pathlib import Path

    from fastapi.responses import JSONResponse

    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Missing 'name' field"}, status_code=400)

    if ".." in name or "/" in name or ";" in name or "|" in name or "&" in name:
        return JSONResponse({"error": "Invalid name format"}, status_code=400)

    # Check both skill locations
    for base in [Path.home() / ".agents" / "skills", Path.home() / ".pocketpaw" / "skills"]:
        skill_dir = base / name
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
            shutil.rmtree(skill_dir)
            loader = get_skill_loader()
            loader.reload()
            return {"status": "ok"}

    return JSONResponse({"error": f"Skill '{name}' not found"}, status_code=404)


@app.post("/api/skills/reload")
async def reload_skills():
    """Force reload skills from disk."""
    loader = get_skill_loader()
    skills = loader.reload()
    return {
        "status": "ok",
        "count": len([s for s in skills.values() if s.user_invocable]),
    }


# ==================== WhatsApp Webhook Routes ====================


@app.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta webhook verification for WhatsApp."""
    from fastapi.responses import PlainTextResponse

    wa = _channel_adapters.get("whatsapp")
    if wa is None:
        return PlainTextResponse("Not configured", status_code=503)
    result = wa.handle_webhook_verify(hub_mode, hub_token, hub_challenge)
    if result:
        return PlainTextResponse(result)
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/webhook/whatsapp")
async def whatsapp_incoming(request: Request):
    """Incoming WhatsApp messages via webhook."""
    wa = _channel_adapters.get("whatsapp")
    if wa is None:
        return {"status": "not configured"}
    payload = await request.json()
    await wa.handle_webhook_message(payload)
    return {"status": "ok"}


@app.get("/api/whatsapp/qr")
async def get_whatsapp_qr():
    """Get current WhatsApp QR code for neonize pairing."""
    adapter = _channel_adapters.get("whatsapp")
    if adapter is None or not hasattr(adapter, "_qr_data"):
        return {"qr": None, "connected": False}
    return {
        "qr": getattr(adapter, "_qr_data", None),
        "connected": getattr(adapter, "_connected", False),
    }


# ==================== Generic Inbound Webhook API ====================


@app.post("/webhook/inbound/{webhook_name}")
async def webhook_inbound(
    webhook_name: str,
    request: Request,
    wait: bool = Query(False),
):
    """Receive an inbound webhook POST.

    Auth: ``X-Webhook-Secret`` header must match the slot's secret,
    OR ``X-Webhook-Signature: sha256=<hex>`` HMAC-SHA256 of the raw body.
    """
    import hashlib
    import hmac

    settings = Settings.load()
    slot_dict = None
    for cfg in settings.webhook_configs:
        if cfg.get("name") == webhook_name:
            slot_dict = cfg
            break

    if slot_dict is None:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_name}' not found")

    from pocketpaw.bus.adapters.webhook_adapter import WebhookSlotConfig

    slot = WebhookSlotConfig(
        name=slot_dict["name"],
        secret=slot_dict["secret"],
        description=slot_dict.get("description", ""),
        sync_timeout=slot_dict.get("sync_timeout", settings.webhook_sync_timeout),
    )

    # --- Auth: secret header or HMAC signature ---
    raw_body = await request.body()
    secret_header = request.headers.get("X-Webhook-Secret", "")
    sig_header = request.headers.get("X-Webhook-Signature", "")

    authed = False
    if secret_header and hmac.compare_digest(secret_header, slot.secret):
        authed = True
    elif sig_header.startswith("sha256="):
        expected = hmac.new(slot.secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig_header[7:], expected):
            authed = True

    if not authed:
        raise HTTPException(status_code=403, detail="Invalid webhook secret or signature")

    # Parse JSON body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Ensure webhook adapter is running (stateless — auto-start is cheap)
    if "webhook" not in _channel_adapters:
        try:
            await _start_channel_adapter("webhook", settings)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to start webhook adapter: {e}")

    adapter = _channel_adapters["webhook"]
    request_id = str(uuid.uuid4())

    if not wait:
        await adapter.handle_webhook(slot, body, request_id, sync=False)
        return {"status": "accepted", "request_id": request_id}

    # Sync mode — wait for agent response
    response_text = await adapter.handle_webhook(slot, body, request_id, sync=True)
    if response_text is None:
        return {"status": "timeout", "request_id": request_id}
    return {"status": "ok", "request_id": request_id, "response": response_text}


@app.get("/api/webhooks")
async def list_webhooks(request: Request):
    """List all configured webhook slots with generated URLs."""
    settings = Settings.load()
    host = request.headers.get("host", f"localhost:{settings.web_port}")
    protocol = "https" if "trycloudflare" in host else "http"

    slots = []
    for cfg in settings.webhook_configs:
        name = cfg.get("name", "")
        secret = cfg.get("secret", "")
        # Redact secret — only show last 4 chars so user can identify it
        redacted = f"***{secret[-4:]}" if len(secret) > 4 else "***"
        slots.append(
            {
                "name": name,
                "description": cfg.get("description", ""),
                "secret": redacted,
                "sync_timeout": cfg.get("sync_timeout", settings.webhook_sync_timeout),
                "url": f"{protocol}://{host}/webhook/inbound/{name}",
            }
        )
    return {"webhooks": slots}


@app.post("/api/webhooks/add")
async def add_webhook(request: Request):
    """Create a new webhook slot (auto-generates secret)."""
    import secrets

    data = await request.json()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Webhook name is required")

    # Validate name: alphanumeric, hyphens, underscores only
    import re

    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise HTTPException(
            status_code=400,
            detail="Webhook name must be alphanumeric (hyphens and underscores allowed)",
        )

    settings = Settings.load()

    # Check for duplicate name
    for cfg in settings.webhook_configs:
        if cfg.get("name") == name:
            raise HTTPException(status_code=409, detail=f"Webhook '{name}' already exists")

    secret = secrets.token_urlsafe(32)
    slot = {
        "name": name,
        "secret": secret,
        "description": description,
        "sync_timeout": data.get("sync_timeout", settings.webhook_sync_timeout),
    }
    settings.webhook_configs.append(slot)
    settings.save()

    return {"status": "ok", "webhook": slot}


@app.post("/api/webhooks/remove")
async def remove_webhook(request: Request):
    """Remove a webhook slot by name."""
    data = await request.json()
    name = data.get("name", "")

    settings = Settings.load()
    original_len = len(settings.webhook_configs)
    settings.webhook_configs = [c for c in settings.webhook_configs if c.get("name") != name]

    if len(settings.webhook_configs) == original_len:
        raise HTTPException(status_code=404, detail=f"Webhook '{name}' not found")

    settings.save()
    return {"status": "ok"}


@app.post("/api/webhooks/regenerate-secret")
async def regenerate_webhook_secret(request: Request):
    """Regenerate a webhook slot's secret."""
    import secrets

    data = await request.json()
    name = data.get("name", "")

    settings = Settings.load()
    for cfg in settings.webhook_configs:
        if cfg.get("name") == name:
            cfg["secret"] = secrets.token_urlsafe(32)
            settings.save()
            return {"status": "ok", "secret": cfg["secret"]}

    raise HTTPException(status_code=404, detail=f"Webhook '{name}' not found")


# ==================== Channel Configuration API ====================

# Maps channel config keys from the frontend to Settings field names
_CHANNEL_CONFIG_KEYS: dict[str, dict[str, str]] = {
    "discord": {
        "bot_token": "discord_bot_token",
        "allowed_guild_ids": "discord_allowed_guild_ids",
        "allowed_user_ids": "discord_allowed_user_ids",
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


@app.get("/api/channels/status")
async def get_channels_status():
    """Get status of all 4 channel adapters."""
    settings = Settings.load()
    result = {}
    all_channels = (
        "discord",
        "slack",
        "whatsapp",
        "telegram",
        "signal",
        "matrix",
        "teams",
        "google_chat",
    )
    for ch in all_channels:
        result[ch] = {
            "configured": _channel_is_configured(ch, settings),
            "running": _channel_is_running(ch),
        }
    # Add WhatsApp mode info
    result["whatsapp"]["mode"] = settings.whatsapp_mode
    return result


@app.post("/api/channels/save")
async def save_channel_config(request: Request):
    """Save token/config for a channel."""
    data = await request.json()
    channel = data.get("channel", "")
    config = data.get("config", {})

    if channel not in _CHANNEL_CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown channel: {channel}")

    key_map = _CHANNEL_CONFIG_KEYS[channel]
    settings = Settings.load()

    for frontend_key, value in config.items():
        settings_field = key_map.get(frontend_key)
        if settings_field:
            setattr(settings, settings_field, value)

    settings.save()
    return {"status": "ok"}


@app.post("/api/channels/toggle")
async def toggle_channel(request: Request):
    """Start or stop a channel adapter dynamically."""
    data = await request.json()
    channel = data.get("channel", "")
    action = data.get("action", "")

    if channel not in _CHANNEL_CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown channel: {channel}")

    settings = Settings.load()

    if action == "start":
        if _channel_is_running(channel):
            return {"error": f"{channel} is already running"}
        if not _channel_is_configured(channel, settings):
            return {"error": f"{channel} is not configured — save tokens first"}
        try:
            await _start_channel_adapter(channel, settings)
            logger.info(f"{channel.title()} adapter started via dashboard")
        except Exception as e:
            return {"error": f"Failed to start {channel}: {e}"}
    elif action == "stop":
        if not _channel_is_running(channel):
            return {"error": f"{channel} is not running"}
        try:
            await _stop_channel_adapter(channel)
            logger.info(f"{channel.title()} adapter stopped via dashboard")
        except Exception as e:
            return {"error": f"Failed to stop {channel}: {e}"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    return {
        "channel": channel,
        "configured": _channel_is_configured(channel, settings),
        "running": _channel_is_running(channel),
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


@app.get("/api/oauth/authorize")
async def oauth_authorize(service: str = Query("google_gmail")):
    """Start OAuth flow — redirects user to provider consent screen."""
    from fastapi.responses import RedirectResponse

    settings = Settings.load()

    scopes = _OAUTH_SCOPES.get(service)
    if not scopes:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    # Determine provider and credentials from service name
    if service == "spotify":
        provider = "spotify"
        client_id = settings.spotify_client_id
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Spotify Client ID not configured. Set it in Settings first.",
            )
    else:
        provider = "google"
        client_id = settings.google_oauth_client_id
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Google OAuth Client ID not configured. Set it in Settings first.",
            )

    from pocketpaw.integrations.oauth import OAuthManager

    manager = OAuthManager()
    redirect_uri = f"http://localhost:{settings.web_port}/oauth/callback"
    state = f"{provider}:{service}"

    auth_url = manager.get_auth_url(
        provider=provider,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
    )
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
):
    """OAuth callback route — exchanges auth code for tokens."""
    from fastapi.responses import HTMLResponse

    if error:
        return HTMLResponse(f"<h2>OAuth Error</h2><p>{error}</p><p>You can close this window.</p>")

    if not code:
        return HTMLResponse("<h2>Missing authorization code</h2>")

    try:
        from pocketpaw.integrations.oauth import OAuthManager
        from pocketpaw.integrations.token_store import TokenStore

        settings = Settings.load()
        manager = OAuthManager(TokenStore())

        # State encodes: "{provider}:{service}" e.g. "google:google_gmail"
        parts = state.split(":", 1)
        provider = parts[0] if parts else "google"
        service = parts[1] if len(parts) > 1 else "google_gmail"

        redirect_uri = f"http://localhost:{settings.web_port}/oauth/callback"

        scopes = _OAUTH_SCOPES.get(service, [])

        # Resolve credentials per provider
        if provider == "spotify":
            client_id = settings.spotify_client_id or ""
            client_secret = settings.spotify_client_secret or ""
        else:
            client_id = settings.google_oauth_client_id or ""
            client_secret = settings.google_oauth_client_secret or ""

        await manager.exchange_code(
            provider=provider,
            service=service,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )

        return HTMLResponse(
            "<h2>Authorization Successful</h2>"
            "<p>Tokens saved. You can close this window and return to PocketPaw.</p>"
        )

    except Exception as e:
        logger.error("OAuth callback error: %s", e)
        return HTMLResponse(f"<h2>OAuth Error</h2><p>{e}</p>")


def _static_version() -> str:
    """Generate a cache-busting version string from JS file mtimes."""
    import hashlib

    js_dir = FRONTEND_DIR / "js"
    if not js_dir.exists():
        return "0"
    mtimes = []
    for f in sorted(js_dir.rglob("*.js")):
        mtimes.append(str(int(f.stat().st_mtime)))
    return hashlib.md5("|".join(mtimes).encode()).hexdigest()[:8]


@app.get("/api/version")
async def get_version_info():
    """Return current version and update availability."""
    from importlib.metadata import version as get_version

    from pocketpaw.config import get_config_dir
    from pocketpaw.update_check import check_for_updates

    current = get_version("pocketpaw")
    info = check_for_updates(current, get_config_dir())
    return info or {"current": current, "latest": current, "update_available": False}


@app.get("/")
async def index(request: Request):
    """Serve the main dashboard page."""
    from importlib.metadata import version as get_version

    return templates.TemplateResponse(
        "base.html",
        {"request": request, "v": _static_version(), "app_version": get_version("pocketpaw")},
    )


# ==================== Auth Middleware ====================

_LOCALHOST_ADDRS = {"127.0.0.1", "localhost", "::1"}
_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for")


def _is_genuine_localhost(request_or_ws) -> bool:
    """Check if request originates from genuine localhost (not a tunneled proxy).

    When a Cloudflare tunnel is active, requests arrive from cloudflared running
    on localhost — but they carry proxy headers (Cf-Connecting-Ip / X-Forwarded-For).
    Those are NOT genuine localhost and must authenticate.

    The ``localhost_auth_bypass`` setting (default True) controls whether genuine
    localhost connections skip auth.  Set to False to require tokens everywhere.
    """
    settings = Settings.load()
    if not settings.localhost_auth_bypass:
        return False

    client_host = request_or_ws.client.host if request_or_ws.client else None
    if client_host not in _LOCALHOST_ADDRS:
        return False

    # If the tunnel is active, check for proxy headers indicating the request
    # was forwarded by cloudflared (not a genuine local browser).
    tunnel = get_tunnel_manager()
    if tunnel.get_status()["active"]:
        headers = request_or_ws.headers
        for hdr in _PROXY_HEADERS:
            if headers.get(hdr):
                return False

    return True


async def verify_token(
    request: Request,
    token: str | None = Query(None),
):
    """
    Verify access token from query param or Authorization header.
    """
    # SKIP AUTH for static files and health checks (if any)
    if request.url.path.startswith("/static") or request.url.path == "/favicon.ico":
        return True

    # Check query param
    current_token = get_access_token()

    if token == current_token:
        return True

    # Check header
    auth_header = request.headers.get("Authorization")
    if auth_header:
        if auth_header == f"Bearer {current_token}":
            return True

    # Allow genuine localhost
    if _is_genuine_localhost(request):
        return True

    raise HTTPException(status_code=401, detail="Unauthorized")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    from fastapi.responses import JSONResponse

    # Exempt routes
    exempt_paths = [
        "/static",
        "/favicon.ico",
        "/api/qr",
        "/api/auth/login",
        "/webhook/whatsapp",
        "/webhook/inbound",
        "/api/whatsapp/qr",
        "/oauth/callback",
    ]

    for path in exempt_paths:
        if request.url.path.startswith(path):
            return await call_next(request)

    # Rate limiting — pick tier based on path
    client_ip = request.client.host if request.client else "unknown"
    is_auth_path = request.url.path in ("/api/auth/session", "/api/qr")
    limiter = auth_limiter if is_auth_path else api_limiter
    if not limiter.allow(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    # Check for token in query or header
    token = request.query_params.get("token")
    auth_header = request.headers.get("Authorization")
    current_token = get_access_token()

    is_valid = False

    # 1. Check Query Param (master token or session token)
    if token:
        if token == current_token:
            is_valid = True
        elif ":" in token and verify_session_token(token, current_token):
            is_valid = True

    # 2. Check Header
    elif auth_header:
        bearer_value = (
            auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        )
        if bearer_value == current_token:
            is_valid = True
        elif ":" in bearer_value and verify_session_token(bearer_value, current_token):
            is_valid = True

    # 3. Check HTTP-only session cookie
    if not is_valid:
        cookie_token = request.cookies.get("pocketpaw_session")
        if cookie_token:
            if cookie_token == current_token:
                is_valid = True
            elif ":" in cookie_token and verify_session_token(cookie_token, current_token):
                is_valid = True

    # 4. Allow genuine localhost (not tunneled proxies)
    if not is_valid and _is_genuine_localhost(request):
        is_valid = True

    # Allow frontend assets (/, /static/*) through for SPA bootstrap.
    # Only match explicit static asset paths — never suffix-match, as that
    # would let crafted URLs like /api/secrets/steal.js bypass auth.
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        return await call_next(request)

    # API Protection
    if request.url.path.startswith("/api") or request.url.path.startswith("/ws"):
        if not is_valid:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    response = await call_next(request)
    return response


# ==================== Session Token Exchange ====================


@app.post("/api/auth/session")
async def exchange_session_token(request: Request):
    """Exchange a master access token for a time-limited session token.

    The client sends the master token in the Authorization header;
    a short-lived HMAC session token is returned.
    """
    auth_header = request.headers.get("Authorization", "")
    bearer = (
        auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    )
    master = get_access_token()
    if bearer != master:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=401, content={"detail": "Invalid master token"})

    settings = Settings.load()
    session_token = create_session_token(master, ttl_hours=settings.session_token_ttl_hours)
    return {"session_token": session_token, "expires_in_hours": settings.session_token_ttl_hours}


# ==================== Cookie-Based Login ====================


@app.post("/api/auth/login")
async def cookie_login(request: Request):
    """Validate access token and set an HTTP-only session cookie.

    Expects JSON body ``{"token": "..."}`` with the master access token.
    Returns an HMAC session token in an HTTP-only cookie so the browser
    sends it automatically on all subsequent requests (including WebSocket
    handshakes). This is more secure than localStorage because JavaScript
    cannot read the cookie value.
    """
    from fastapi.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    submitted = body.get("token", "").strip()
    master = get_access_token()

    if submitted != master:
        return JSONResponse(status_code=401, content={"detail": "Invalid access token"})

    settings = Settings.load()
    session_token = create_session_token(master, ttl_hours=settings.session_token_ttl_hours)
    max_age = settings.session_token_ttl_hours * 3600

    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key="pocketpaw_session",
        value=session_token,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=max_age,
    )
    return response


@app.post("/api/auth/logout")
async def cookie_logout():
    """Clear the session cookie."""
    from fastapi.responses import JSONResponse

    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="pocketpaw_session", path="/")
    return response


# ==================== QR Code & Token API ====================


@app.get("/api/qr")
async def get_qr_code(request: Request):
    """Generate QR login code."""
    # Logic: If tunnel is active, use tunnel URL. Else local IP.
    # For Phase 5A, simpler: Just use what the request came to, or attempt to find local IP.
    host = request.headers.get("host")

    # Check for ACTIVE tunnel first to prioritize it
    tunnel = get_tunnel_manager()
    status = tunnel.get_status()

    # Use a short-lived session token instead of the master token
    # to limit exposure in browser history, screenshots, and logs.
    qr_token = create_session_token(get_access_token(), ttl_hours=1)

    if status.get("active") and status.get("url"):
        login_url = f"{status['url']}/?token={qr_token}"
    else:
        # Fallback to current request host (localhost or network IP)
        protocol = "https" if "trycloudflare" in str(host) else "http"
        login_url = f"{protocol}://{host}/?token={qr_token}"

    img = qrcode.make(login_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")


@app.post("/api/token/regenerate")
async def regenerate_access_token():
    """Regenerate access token (invalidates old sessions)."""
    # This endpoint implies you are already authorized (middleware checks it)
    new_token = regenerate_token()
    return {"token": new_token}


# ==================== Tunnel API ====================


@app.get("/api/remote/status")
async def get_tunnel_status():
    """Get active tunnel status."""
    manager = get_tunnel_manager()
    return manager.get_status()


@app.post("/api/remote/start")
async def start_tunnel():
    """Start Cloudflare tunnel."""
    manager = get_tunnel_manager()
    try:
        url = await manager.start()
        return {"url": url, "active": True}
    except Exception as e:
        # Error handling via JSON to frontend
        return {"error": str(e), "active": False}


@app.post("/api/remote/stop")
async def stop_tunnel():
    """Stop Cloudflare tunnel."""
    manager = get_tunnel_manager()
    await manager.stop()
    return {"active": False}


# ============================================================================
# Telegram Setup API
# ============================================================================

# Global state for Telegram pairing
_telegram_pairing_state = {
    "session_secret": None,
    "paired": False,
    "user_id": None,
    "temp_bot_app": None,
}


@app.get("/api/telegram/status")
async def get_telegram_status():
    """Get current Telegram configuration status."""
    settings = Settings.load()
    return {
        "configured": bool(settings.telegram_bot_token and settings.allowed_user_id),
        "user_id": settings.allowed_user_id,
    }


@app.post("/api/telegram/setup")
async def setup_telegram(request: Request):
    """Start Telegram pairing flow."""
    import secrets

    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    data = await request.json()
    bot_token = data.get("bot_token", "").strip()

    if not bot_token:
        return {"error": "Bot token is required"}

    # Generate session secret
    session_secret = secrets.token_urlsafe(32)
    _telegram_pairing_state["session_secret"] = session_secret
    _telegram_pairing_state["paired"] = False
    _telegram_pairing_state["user_id"] = None

    # Save token to settings
    settings = Settings.load()
    settings.telegram_bot_token = bot_token
    settings.save()

    try:
        # Initialize temporary bot to verify token and get username
        builder = Application.builder().token(bot_token)
        temp_app = builder.build()

        bot_user = await temp_app.bot.get_me()
        username = bot_user.username

        # Generate Deep Link: https://t.me/<username>?start=<secret>
        deep_link = f"https://t.me/{username}?start={session_secret}"

        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(deep_link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        qr_url = f"data:image/png;base64,{qr_base64}"

        # Define pairing handler
        async def handle_pairing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.effective_user:
                return

            text = update.message.text or ""
            parts = text.split()

            if len(parts) < 2:
                await update.message.reply_text(
                    "⏳ Waiting for pairing... Please scan the QR code to start."
                )
                return

            secret = parts[1]
            if secret != _telegram_pairing_state["session_secret"]:
                await update.message.reply_text(
                    "❌ Invalid session token. Please refresh the setup page."
                )
                return

            # Success!
            user_id = update.effective_user.id
            _telegram_pairing_state["paired"] = True
            _telegram_pairing_state["user_id"] = user_id

            # Save to config
            settings = Settings.load()
            settings.allowed_user_id = user_id
            settings.save()

            await update.message.reply_text(
                "🎉 **Connected!**\n\nPocketPaw is now paired with this device.\nYou can close the browser window now.",
                parse_mode="Markdown",
            )

            logger.info(
                f"✅ Telegram paired with user: {update.effective_user.username} ({user_id})"
            )

        # Start listening for /start <secret>
        temp_app.add_handler(CommandHandler("start", handle_pairing_start))
        await temp_app.initialize()
        await temp_app.start()
        await temp_app.updater.start_polling(drop_pending_updates=True)

        # Store for cleanup later
        _telegram_pairing_state["temp_bot_app"] = temp_app

        return {"qr_url": qr_url, "deep_link": deep_link}

    except Exception as e:
        logger.error(f"Telegram setup failed: {e}")
        return {"error": f"Failed to connect to Telegram: {str(e)}"}


@app.get("/api/telegram/pairing-status")
async def get_telegram_pairing_status():
    """Check if Telegram pairing is complete."""
    paired = _telegram_pairing_state.get("paired", False)
    user_id = _telegram_pairing_state.get("user_id")

    # If paired, cleanup the temporary bot
    if paired and _telegram_pairing_state.get("temp_bot_app"):
        try:
            temp_app = _telegram_pairing_state["temp_bot_app"]
            if temp_app.updater.running:
                await temp_app.updater.stop()
            if temp_app.running:
                await temp_app.stop()
            await temp_app.shutdown()
            _telegram_pairing_state["temp_bot_app"] = None
        except Exception as e:
            logger.warning(f"Error cleaning up temp bot: {e}")

    return {"paired": paired, "user_id": user_id}


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None),
    resume_session: str | None = Query(None),
):
    """WebSocket endpoint for real-time communication.

    Auth: accepts token via query param (legacy) OR first message (preferred).
    Genuine localhost connections bypass auth.
    """
    # Rate limit WebSocket connections
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_limiter.allow(client_ip):
        await websocket.close(code=4029, reason="Too many connections")
        return

    expected_token = get_access_token()

    def _token_valid(t: str | None) -> bool:
        if not t:
            return False
        if t == expected_token:
            return True
        # Accept session tokens (format: "expires:hmac")
        if ":" in t and verify_session_token(t, expected_token):
            return True
        return False

    # Check HTTP-only session cookie
    cookie_token = websocket.cookies.get("pocketpaw_session")
    if not _token_valid(token) and _token_valid(cookie_token):
        token = cookie_token  # Use cookie token for subsequent checks

    # Allow genuine localhost bypass for WebSocket (not tunneled proxies)
    is_localhost = _is_genuine_localhost(websocket)

    if not _token_valid(token) and not is_localhost:
        await websocket.close(code=4003, reason="Unauthorized")
        return

    # Accept connection first — token can arrive via first message
    if _token_valid(token) or is_localhost:
        await websocket.accept()
    else:
        # Accept temporarily, wait for auth message
        await websocket.accept()
        try:
            first_msg = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
            if first_msg.get("action") == "authenticate" and _token_valid(first_msg.get("token")):
                pass  # Authenticated
            else:
                await websocket.close(code=4003, reason="Unauthorized")
                return
        except (TimeoutError, Exception):
            await websocket.close(code=4003, reason="Unauthorized")
            return

    # Track connection
    active_connections.append(websocket)

    # Generate session ID for bus (or resume existing)
    chat_id = str(uuid.uuid4())

    # Resume session if requested
    resumed = False
    if resume_session:
        # Parse safe_key to extract channel and raw UUID
        parts = resume_session.split("_", 1)
        if len(parts) == 2 and parts[0] == "websocket":
            raw_id = parts[1]
            session_key = f"websocket:{raw_id}"
            # Verify session file exists
            session_file = (
                Path.home() / ".pocketpaw" / "memory" / "sessions" / f"{resume_session}.json"
            )
            if session_file.exists():
                chat_id = raw_id
                resumed = True

    await ws_adapter.register_connection(websocket, chat_id)

    # Build session safe_key for frontend
    safe_key = f"websocket_{chat_id}"

    # Send welcome notification with session info
    await websocket.send_json(
        {
            "type": "connection_info",
            "content": "Connected to PocketPaw",
            "id": safe_key,
        }
    )

    # If resuming, send session history
    if resumed:
        session_key = f"websocket:{chat_id}"
        try:
            manager = get_memory_manager()
            history = await manager.get_session_history(session_key, limit=100)
            await websocket.send_json(
                {
                    "type": "session_history",
                    "session_id": safe_key,
                    "messages": history,
                }
            )
        except Exception as e:
            logger.warning("Failed to load session history for resume: %s", e)

    # Load settings
    settings = Settings.load()

    # Legacy state
    agent_active = False

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            # Handle chat via MessageBus
            if action == "chat":
                log_msg = f"⚡ Processing message with Backend: {settings.agent_backend} (Provider: {settings.llm_provider})"
                logger.warning(log_msg)  # Use WARNING to ensure it shows up
                print(log_msg)  # Force stdout just in case

                # Only if using new backend, but let's default to new backend logic eventually
                # For Phase 2 transition: We use the Bus!
                # But allow fallback to old router if 'agent_active' is toggled specifically for old behavior?
                # Actually, let's treat 'chat' as input to the Bus.
                await ws_adapter.handle_message(chat_id, data)

            # Session switching
            elif action == "switch_session":
                session_id = data.get("session_id", "")
                # Parse safe_key: "websocket_<uuid>"
                parts = session_id.split("_", 1)
                if len(parts) == 2:
                    channel_prefix = parts[0]
                    raw_id = parts[1]
                    new_session_key = f"{channel_prefix}:{raw_id}"

                    # Unregister old connection, register with new chat_id
                    await ws_adapter.unregister_connection(chat_id)
                    chat_id = raw_id
                    await ws_adapter.register_connection(websocket, chat_id)

                    # Load and send history
                    try:
                        manager = get_memory_manager()
                        history = await manager.get_session_history(new_session_key, limit=100)
                        await websocket.send_json(
                            {
                                "type": "session_history",
                                "session_id": session_id,
                                "messages": history,
                            }
                        )
                    except Exception as e:
                        logger.warning("Failed to load session history: %s", e)
                        await websocket.send_json(
                            {"type": "session_history", "session_id": session_id, "messages": []}
                        )

            # New session
            elif action == "new_session":
                await ws_adapter.unregister_connection(chat_id)
                chat_id = str(uuid.uuid4())
                await ws_adapter.register_connection(websocket, chat_id)
                safe_key = f"websocket_{chat_id}"
                await websocket.send_json({"type": "new_session", "id": safe_key})

            # Legacy/Other actions
            elif action == "tool":
                tool = data.get("tool")
                await handle_tool(websocket, tool, settings, data)

            # Handle agent toggle (Legacy router control)
            elif action == "toggle_agent":
                # For now, this just logs, as the Loop is always running in background
                # functionality-wise, but maybe we should respect this flag in the Loop?
                agent_active = data.get("active", False)
                await websocket.send_json(
                    {
                        "type": "notification",
                        "content": f"Legacy Mode: {'ON' if agent_active else 'OFF'} (Bus is always active)",
                    }
                )

            # Handle settings update
            elif action == "settings":
                async with _settings_lock:
                    settings.agent_backend = data.get("agent_backend", settings.agent_backend)
                    settings.llm_provider = data.get("llm_provider", settings.llm_provider)
                    if data.get("ollama_host"):
                        settings.ollama_host = data["ollama_host"]
                    if data.get("ollama_model"):
                        settings.ollama_model = data["ollama_model"]
                    if data.get("anthropic_model"):
                        settings.anthropic_model = data.get("anthropic_model")
                    if data.get("openai_compatible_base_url") is not None:
                        settings.openai_compatible_base_url = data["openai_compatible_base_url"]
                    if data.get("openai_compatible_api_key"):
                        settings.openai_compatible_api_key = data["openai_compatible_api_key"]
                    if data.get("openai_compatible_model") is not None:
                        settings.openai_compatible_model = data["openai_compatible_model"]
                    if "openai_compatible_max_tokens" in data:
                        val = data["openai_compatible_max_tokens"]
                        if isinstance(val, (int, float)) and 0 <= val <= 1000000:
                            settings.openai_compatible_max_tokens = int(val)
                    if data.get("gemini_model"):
                        settings.gemini_model = data["gemini_model"]
                    if "bypass_permissions" in data:
                        settings.bypass_permissions = bool(data.get("bypass_permissions"))
                    if data.get("web_search_provider"):
                        settings.web_search_provider = data["web_search_provider"]
                    if data.get("url_extract_provider"):
                        settings.url_extract_provider = data["url_extract_provider"]
                    if "injection_scan_enabled" in data:
                        settings.injection_scan_enabled = bool(data["injection_scan_enabled"])
                    if "injection_scan_llm" in data:
                        settings.injection_scan_llm = bool(data["injection_scan_llm"])
                    if data.get("tool_profile"):
                        settings.tool_profile = data["tool_profile"]
                    if "plan_mode" in data:
                        settings.plan_mode = bool(data["plan_mode"])
                    if "plan_mode_tools" in data:
                        raw = data["plan_mode_tools"]
                        if isinstance(raw, str):
                            settings.plan_mode_tools = [
                                t.strip() for t in raw.split(",") if t.strip()
                            ]
                        elif isinstance(raw, list):
                            settings.plan_mode_tools = raw
                    if "smart_routing_enabled" in data:
                        settings.smart_routing_enabled = bool(data["smart_routing_enabled"])
                    if data.get("model_tier_simple"):
                        settings.model_tier_simple = data["model_tier_simple"]
                    if data.get("model_tier_moderate"):
                        settings.model_tier_moderate = data["model_tier_moderate"]
                    if data.get("model_tier_complex"):
                        settings.model_tier_complex = data["model_tier_complex"]
                    if data.get("tts_provider"):
                        settings.tts_provider = data["tts_provider"]
                    if "tts_voice" in data:
                        settings.tts_voice = data["tts_voice"]
                    if data.get("stt_provider"):
                        settings.stt_provider = data["stt_provider"]
                    if data.get("stt_model"):
                        settings.stt_model = data["stt_model"]
                    if data.get("ocr_provider"):
                        settings.ocr_provider = data["ocr_provider"]
                    if data.get("sarvam_tts_language"):
                        settings.sarvam_tts_language = data["sarvam_tts_language"]
                    if "self_audit_enabled" in data:
                        settings.self_audit_enabled = bool(data["self_audit_enabled"])
                    if data.get("self_audit_schedule"):
                        settings.self_audit_schedule = data["self_audit_schedule"]
                    # Memory settings
                    if data.get("memory_backend"):
                        settings.memory_backend = data["memory_backend"]
                    if "mem0_auto_learn" in data:
                        settings.mem0_auto_learn = bool(data["mem0_auto_learn"])
                    if data.get("mem0_llm_provider"):
                        settings.mem0_llm_provider = data["mem0_llm_provider"]
                    if data.get("mem0_llm_model"):
                        settings.mem0_llm_model = data["mem0_llm_model"]
                    if data.get("mem0_embedder_provider"):
                        settings.mem0_embedder_provider = data["mem0_embedder_provider"]
                    if data.get("mem0_embedder_model"):
                        settings.mem0_embedder_model = data["mem0_embedder_model"]
                    if data.get("mem0_vector_store"):
                        settings.mem0_vector_store = data["mem0_vector_store"]
                    if data.get("mem0_ollama_base_url"):
                        settings.mem0_ollama_base_url = data["mem0_ollama_base_url"]
                    settings.save()

                # Reset the agent loop's router to pick up new settings
                agent_loop.reset_router()

                # Clear settings cache so memory manager picks up new values
                from pocketpaw.config import get_settings as _get_settings

                _get_settings.cache_clear()

                # Reload memory manager with fresh settings
                agent_loop.memory = get_memory_manager(force_reload=True)
                agent_loop.context_builder.memory = agent_loop.memory

                await websocket.send_json({"type": "message", "content": "⚙️ Settings updated"})

            # ... keep other handlers ... (abbreviated)

            # Handle API key save
            elif action == "save_api_key":
                provider = data.get("provider")
                key = data.get("key", "")

                async with _settings_lock:
                    if provider == "anthropic" and key:
                        settings.anthropic_api_key = key
                        settings.llm_provider = "anthropic"
                        settings.save()
                        agent_loop.reset_router()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Anthropic API key saved!"}
                        )
                    elif provider == "openai" and key:
                        settings.openai_api_key = key
                        settings.llm_provider = "openai"
                        settings.save()
                        agent_loop.reset_router()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ OpenAI API key saved!"}
                        )
                    elif provider == "google" and key:
                        settings.google_api_key = key
                        settings.llm_provider = "gemini"
                        settings.save()
                        agent_loop.reset_router()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Google API key saved!"}
                        )
                    elif provider == "tavily" and key:
                        settings.tavily_api_key = key
                        settings.save()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Tavily API key saved!"}
                        )
                    elif provider == "brave" and key:
                        settings.brave_search_api_key = key
                        settings.save()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Brave Search API key saved!"}
                        )
                    elif provider == "parallel" and key:
                        settings.parallel_api_key = key
                        settings.save()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Parallel AI API key saved!"}
                        )
                    elif provider == "elevenlabs" and key:
                        settings.elevenlabs_api_key = key
                        settings.save()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ ElevenLabs API key saved!"}
                        )
                    elif provider == "google_oauth_id" and key:
                        settings.google_oauth_client_id = key
                        settings.save()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Google OAuth Client ID saved!"}
                        )
                    elif provider == "google_oauth_secret" and key:
                        settings.google_oauth_client_secret = key
                        settings.save()
                        await websocket.send_json(
                            {
                                "type": "message",
                                "content": "✅ Google OAuth Client Secret saved!",
                            }
                        )
                    elif provider == "spotify_client_id" and key:
                        settings.spotify_client_id = key
                        settings.save()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Spotify Client ID saved!"}
                        )
                    elif provider == "spotify_client_secret" and key:
                        settings.spotify_client_secret = key
                        settings.save()
                        await websocket.send_json(
                            {
                                "type": "message",
                                "content": "✅ Spotify Client Secret saved!",
                            }
                        )
                    elif provider == "sarvam" and key:
                        settings.sarvam_api_key = key
                        settings.save()
                        await websocket.send_json(
                            {"type": "message", "content": "✅ Sarvam AI API key saved!"}
                        )
                    else:
                        await websocket.send_json(
                            {"type": "error", "content": "Invalid API key or provider"}
                        )

            # Handle get_settings - return current settings to frontend
            elif action == "get_settings":
                # Get agent status if available
                agent_status = None
                # Get agent status if available
                agent_status = {
                    "status": "running" if agent_loop._running else "stopped",
                    "backend": "AgentLoop",
                }

                await websocket.send_json(
                    {
                        "type": "settings",
                        "content": {
                            "agentBackend": settings.agent_backend,
                            "llmProvider": settings.llm_provider,
                            "ollamaHost": settings.ollama_host,
                            "ollamaModel": settings.ollama_model,
                            "anthropicModel": settings.anthropic_model,
                            "openaiCompatibleBaseUrl": settings.openai_compatible_base_url,
                            "openaiCompatibleModel": settings.openai_compatible_model,
                            "openaiCompatibleMaxTokens": settings.openai_compatible_max_tokens,
                            "hasOpenaiCompatibleKey": bool(settings.openai_compatible_api_key),
                            "geminiModel": settings.gemini_model,
                            "hasGoogleApiKey": bool(settings.google_api_key),
                            "bypassPermissions": settings.bypass_permissions,
                            "hasAnthropicKey": bool(settings.anthropic_api_key),
                            "hasOpenaiKey": bool(settings.openai_api_key),
                            "webSearchProvider": settings.web_search_provider,
                            "urlExtractProvider": settings.url_extract_provider,
                            "hasTavilyKey": bool(settings.tavily_api_key),
                            "hasBraveKey": bool(settings.brave_search_api_key),
                            "hasParallelKey": bool(settings.parallel_api_key),
                            "injectionScanEnabled": settings.injection_scan_enabled,
                            "injectionScanLlm": settings.injection_scan_llm,
                            "toolProfile": settings.tool_profile,
                            "planMode": settings.plan_mode,
                            "planModeTools": ",".join(settings.plan_mode_tools),
                            "smartRoutingEnabled": settings.smart_routing_enabled,
                            "modelTierSimple": settings.model_tier_simple,
                            "modelTierModerate": settings.model_tier_moderate,
                            "modelTierComplex": settings.model_tier_complex,
                            "ttsProvider": settings.tts_provider,
                            "ttsVoice": settings.tts_voice,
                            "sttProvider": settings.stt_provider,
                            "sttModel": settings.stt_model,
                            "ocrProvider": settings.ocr_provider,
                            "sarvamTtsLanguage": settings.sarvam_tts_language,
                            "selfAuditEnabled": settings.self_audit_enabled,
                            "selfAuditSchedule": settings.self_audit_schedule,
                            "memoryBackend": settings.memory_backend,
                            "mem0AutoLearn": settings.mem0_auto_learn,
                            "mem0LlmProvider": settings.mem0_llm_provider,
                            "mem0LlmModel": settings.mem0_llm_model,
                            "mem0EmbedderProvider": settings.mem0_embedder_provider,
                            "mem0EmbedderModel": settings.mem0_embedder_model,
                            "mem0VectorStore": settings.mem0_vector_store,
                            "mem0OllamaBaseUrl": settings.mem0_ollama_base_url,
                            "hasElevenlabsKey": bool(settings.elevenlabs_api_key),
                            "hasGoogleOAuthId": bool(settings.google_oauth_client_id),
                            "hasGoogleOAuthSecret": bool(settings.google_oauth_client_secret),
                            "hasSpotifyClientId": bool(settings.spotify_client_id),
                            "hasSpotifyClientSecret": bool(settings.spotify_client_secret),
                            "hasSarvamKey": bool(settings.sarvam_api_key),
                            "agentActive": agent_active,
                            "agentStatus": agent_status,
                        },
                    }
                )

            # Handle file navigation (legacy)
            elif action == "navigate":
                path = data.get("path", "")
                await handle_file_navigation(websocket, path, settings)

            # Health engine actions
            elif action == "get_health":
                try:
                    from pocketpaw.health import get_health_engine

                    engine = get_health_engine()
                    await websocket.send_json({"type": "health_update", "data": engine.summary})
                except Exception as e:
                    await websocket.send_json(
                        {"type": "health_update", "data": {"status": "unknown", "error": str(e)}}
                    )

            elif action == "run_health_check":
                try:
                    from pocketpaw.health import get_health_engine

                    engine = get_health_engine()
                    await engine.run_all_checks()
                    await websocket.send_json({"type": "health_update", "data": engine.summary})
                except Exception as e:
                    await websocket.send_json(
                        {"type": "health_update", "data": {"status": "unknown", "error": str(e)}}
                    )

            elif action == "get_health_errors":
                try:
                    from pocketpaw.health import get_health_engine

                    engine = get_health_engine()
                    limit = data.get("limit", 20)
                    search = data.get("search", "")
                    errors = engine.get_recent_errors(limit=limit, search=search)
                    await websocket.send_json({"type": "health_errors", "errors": errors})
                except Exception as e:
                    await websocket.send_json(
                        {"type": "health_errors", "errors": [], "error": str(e)}
                    )

            # Handle file browser
            elif action == "browse":
                path = data.get("path", "~")
                context = data.get("context")
                await handle_file_browse(websocket, path, settings, context=context)

            # Handle reminder actions
            elif action == "get_reminders":
                scheduler = get_scheduler()
                reminders = scheduler.get_reminders()
                # Add time remaining to each reminder
                for r in reminders:
                    r["time_remaining"] = scheduler.format_time_remaining(r)
                await websocket.send_json({"type": "reminders", "reminders": reminders})

            elif action == "add_reminder":
                message = data.get("message", "")
                scheduler = get_scheduler()
                reminder = scheduler.add_reminder(message)

                if reminder:
                    reminder["time_remaining"] = scheduler.format_time_remaining(reminder)
                    await websocket.send_json({"type": "reminder_added", "reminder": reminder})
                else:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "content": "Could not parse time from message. Try 'in 5 minutes' or 'at 3pm'",
                        }
                    )

            elif action == "delete_reminder":
                reminder_id = data.get("id", "")
                scheduler = get_scheduler()
                if scheduler.delete_reminder(reminder_id):
                    await websocket.send_json({"type": "reminder_deleted", "id": reminder_id})
                else:
                    await websocket.send_json({"type": "error", "content": "Reminder not found"})

            # ==================== Intentions API ====================

            elif action == "get_intentions":
                daemon = get_daemon()
                intentions = daemon.get_intentions()
                await websocket.send_json({"type": "intentions", "intentions": intentions})

            elif action == "create_intention":
                daemon = get_daemon()
                try:
                    intention = daemon.create_intention(
                        name=data.get("name", "Unnamed"),
                        prompt=data.get("prompt", ""),
                        trigger=data.get("trigger", {"type": "cron", "schedule": "0 9 * * *"}),
                        context_sources=data.get("context_sources", []),
                        enabled=data.get("enabled", True),
                    )
                    await websocket.send_json({"type": "intention_created", "intention": intention})
                except Exception as e:
                    await websocket.send_json(
                        {"type": "error", "content": f"Failed to create intention: {e}"}
                    )

            elif action == "update_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                updates = data.get("updates", {})
                intention = daemon.update_intention(intention_id, updates)
                if intention:
                    await websocket.send_json({"type": "intention_updated", "intention": intention})
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            elif action == "delete_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                if daemon.delete_intention(intention_id):
                    await websocket.send_json({"type": "intention_deleted", "id": intention_id})
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            elif action == "toggle_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                intention = daemon.toggle_intention(intention_id)
                if intention:
                    await websocket.send_json({"type": "intention_toggled", "intention": intention})
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            elif action == "run_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                intention = daemon.get_intention(intention_id)
                if intention:
                    # Run in background, results streamed via broadcast_intention
                    await websocket.send_json(
                        {
                            "type": "notification",
                            "content": f"🚀 Running intention: {intention['name']}",
                        }
                    )
                    asyncio.create_task(daemon.run_intention_now(intention_id))
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            # ==================== Plan Mode API ====================

            elif action == "approve_plan":
                from pocketpaw.agents.plan_mode import get_plan_manager

                pm = get_plan_manager()
                session_key = data.get("session_key", "")
                plan = pm.approve_plan(session_key)
                if plan:
                    await websocket.send_json({"type": "plan_approved", "session_key": session_key})
                else:
                    await websocket.send_json(
                        {"type": "error", "content": "No active plan to approve"}
                    )

            elif action == "reject_plan":
                from pocketpaw.agents.plan_mode import get_plan_manager

                pm = get_plan_manager()
                session_key = data.get("session_key", "")
                plan = pm.reject_plan(session_key)
                if plan:
                    await websocket.send_json({"type": "plan_rejected", "session_key": session_key})
                else:
                    await websocket.send_json(
                        {"type": "error", "content": "No active plan to reject"}
                    )

            # ==================== Skills API ====================

            elif action == "get_skills":
                loader = get_skill_loader()
                loader.reload()  # Refresh to catch new installs
                skills = [
                    {
                        "name": s.name,
                        "description": s.description,
                        "argument_hint": s.argument_hint,
                    }
                    for s in loader.get_invocable()
                ]
                await websocket.send_json({"type": "skills", "skills": skills})

            elif action == "run_skill":
                skill_name = data.get("name", "")
                skill_args = data.get("args", "")

                loader = get_skill_loader()
                skill = loader.get(skill_name)

                if not skill:
                    available = [s.name for s in loader.get_invocable()]
                    hint = (
                        f"Available commands: /{', /'.join(available)}"
                        if available
                        else "No skills installed yet."
                    )
                    await websocket.send_json(
                        {
                            "type": "error",
                            "content": f"Unknown command: /{skill_name}\n\n{hint}",
                        }
                    )
                else:
                    await websocket.send_json(
                        {"type": "notification", "content": f"🎯 Running skill: {skill_name}"}
                    )

                    # Execute skill through agent
                    executor = SkillExecutor(settings)
                    await websocket.send_json({"type": "stream_start"})
                    try:
                        async for chunk in executor.execute_skill(skill, skill_args):
                            await websocket.send_json(chunk)
                    finally:
                        await websocket.send_json({"type": "stream_end"})

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)
        await ws_adapter.unregister_connection(chat_id)


# ==================== Transparency APIs ====================


@app.get("/api/identity")
async def get_identity():
    """Get agent identity context (all 4 identity files)."""
    provider = DefaultBootstrapProvider(get_config_path().parent)
    context = await provider.get_context()
    return {
        "identity_file": context.identity,
        "soul_file": context.soul,
        "style_file": context.style,
        "user_file": context.user_profile,
    }


@app.put("/api/identity")
async def save_identity(request: Request):
    """Save edits to agent identity files. Changes take effect on the next message."""
    data = await request.json()
    identity_dir = get_config_path().parent / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)

    file_map = {
        "identity_file": "IDENTITY.md",
        "soul_file": "SOUL.md",
        "style_file": "STYLE.md",
        "user_file": "USER.md",
    }
    updated = []
    for key, filename in file_map.items():
        if key in data and isinstance(data[key], str):
            (identity_dir / filename).write_text(data[key])
            updated.append(filename)

    return {"ok": True, "updated": updated}


@app.get("/api/sessions")
async def list_sessions_v2(limit: int = 50):
    """List sessions using the fast session index."""
    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "_load_session_index"):
        index = store._load_session_index()
        # Sort by last_activity descending
        entries = sorted(
            index.items(),
            key=lambda kv: kv[1].get("last_activity", ""),
            reverse=True,
        )[:limit]
        sessions = []
        for safe_key, meta in entries:
            sessions.append({"id": safe_key, **meta})
        return {"sessions": sessions, "total": len(index)}

    # Fallback for non-file stores
    return {"sessions": [], "total": 0}


@app.get("/api/memory/sessions")
async def list_sessions(limit: int = 20):
    """List all available sessions with metadata (legacy endpoint)."""
    result = await list_sessions_v2(limit=limit)
    return result.get("sessions", [])


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session by ID."""
    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "delete_session"):
        deleted = await store.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"status": "ok"}

    raise HTTPException(status_code=501, detail="Store does not support session deletion")


@app.post("/api/sessions/{session_id}/title")
async def update_session_title(session_id: str, request: Request):
    """Update the title of a session."""
    data = await request.json()
    title = data.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "update_session_title"):
        updated = await store.update_session_title(session_id, title)
        if not updated:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"status": "ok"}

    raise HTTPException(status_code=501, detail="Store does not support title updates")


@app.get("/api/sessions/search")
async def search_sessions(q: str = Query(""), limit: int = 20):
    """Search sessions by content."""
    import json

    if not q.strip():
        return {"sessions": []}

    query_lower = q.lower()
    manager = get_memory_manager()
    store = manager._store

    if not hasattr(store, "sessions_path"):
        return {"sessions": []}

    results = []
    index = store._load_session_index() if hasattr(store, "_load_session_index") else {}

    for session_file in store.sessions_path.glob("*.json"):
        if session_file.name.startswith("_") or session_file.name.endswith("_compaction.json"):
            continue
        try:
            data = json.loads(session_file.read_text())
            for msg in data:
                if query_lower in msg.get("content", "").lower():
                    safe_key = session_file.stem
                    meta = index.get(safe_key, {})
                    results.append(
                        {
                            "id": safe_key,
                            "title": meta.get("title", "Untitled"),
                            "channel": meta.get("channel", "unknown"),
                            "match": msg["content"][:200],
                            "match_role": msg.get("role", ""),
                            "last_activity": meta.get("last_activity", ""),
                        }
                    )
                    break
        except (json.JSONDecodeError, OSError):
            continue
        if len(results) >= limit:
            break

    return {"sessions": results}


@app.get("/api/memory/session")
async def get_session_memory(id: str = "", limit: int = 50):
    """Get session memory."""
    if not id:
        return []
    manager = get_memory_manager()
    return await manager.get_session_history(id, limit=limit)


def _export_session_json(entries: list, session_id: str) -> str:
    """Format session entries as JSON export."""
    from datetime import UTC, datetime

    messages = []
    for e in entries:
        ts = e.created_at.isoformat() if hasattr(e.created_at, "isoformat") else str(e.created_at)
        messages.append(
            {
                "id": e.id,
                "role": e.role or "user",
                "content": e.content,
                "timestamp": ts,
                "metadata": e.metadata,
            }
        )

    return json.dumps(
        {
            "export_version": "1.0",
            "exported_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "message_count": len(messages),
            "messages": messages,
        },
        indent=2,
        default=str,
    )


def _export_session_markdown(entries: list, session_id: str) -> str:
    """Format session entries as readable Markdown."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Conversation Export",
        f"**Session**: `{session_id}` | **Messages**: {len(entries)} | **Exported**: {now}",
        "",
        "---",
    ]

    for e in entries:
        role = (e.role or "user").capitalize()
        ts = ""
        if hasattr(e.created_at, "strftime"):
            ts = e.created_at.strftime("%H:%M")

        lines.append("")
        lines.append(f"**{role}** ({ts}):" if ts else f"**{role}**:")
        lines.append(e.content)
        lines.append("")
        lines.append("---")

    return "\n".join(lines)


@app.get("/api/memory/session/export")
async def export_session(id: str = "", format: str = "json"):
    """Export a session as downloadable JSON or Markdown."""
    if not id:
        raise HTTPException(status_code=400, detail="Missing required parameter: id")

    if format not in ("json", "md"):
        raise HTTPException(status_code=400, detail="Format must be 'json' or 'md'")

    manager = get_memory_manager()
    entries = await manager._store.get_session(id)

    if not entries:
        raise HTTPException(status_code=404, detail=f"Session not found: {id}")

    if format == "json":
        content = _export_session_json(entries, id)
        media_type = "application/json"
        ext = "json"
    else:
        content = _export_session_markdown(entries, id)
        media_type = "text/markdown"
        ext = "md"

    filename = f"pocketpaw-session-{id[:20]}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/memory/long_term")
async def get_long_term_memory(limit: int = 50):
    """Get long-term memories."""
    manager = get_memory_manager()
    # Access store directly for filtered query, or use get_by_type if exposed
    # Manager doesn't expose get_by_type publically in facade (it used _store.get_by_type in get_context_for_agent)
    # So we use filtered search or we should expose it.
    # For now, let's use _store hack or add method to manager?
    # I'll rely on a new Manager method or _store for now to keep it simple.
    items = await manager._store.get_by_type(MemoryType.LONG_TERM, limit=limit)
    return [
        {
            "id": item.id,
            "content": item.content,
            "timestamp": item.created_at.isoformat(),
            "tags": item.tags,
        }
        for item in items
    ]


@app.delete("/api/memory/long_term/{entry_id}")
async def delete_long_term_memory(entry_id: str):
    """Delete a long-term memory entry by ID."""
    manager = get_memory_manager()
    deleted = await manager._store.delete(entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"ok": True}


@app.get("/api/audit")
async def get_audit_log(limit: int = 100):
    """Get audit logs."""
    logger = get_audit_logger()
    if not logger.log_path.exists():
        return []

    logs: list[dict] = []
    try:
        with open(logger.log_path) as f:
            lines = f.readlines()

        for line in reversed(lines):
            if len(logs) >= limit:
                break
            try:
                logs.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []

    return logs


@app.delete("/api/audit")
async def clear_audit_log():
    """Clear the audit log file."""
    logger = get_audit_logger()
    try:
        if logger.log_path.exists():
            logger.log_path.write_text("")
        return {"ok": True}
    except Exception as e:
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/security-audit")
async def run_security_audit_endpoint():
    """Run security audit checks and return results."""
    from pocketpaw.security.audit_cli import (
        _check_audit_log,
        _check_bypass_permissions,
        _check_config_permissions,
        _check_file_jail,
        _check_guardian_reachable,
        _check_plaintext_api_keys,
        _check_tool_profile,
    )

    checks = [
        ("Config file permissions", _check_config_permissions),
        ("Plaintext API keys", _check_plaintext_api_keys),
        ("Audit log", _check_audit_log),
        ("Guardian agent", _check_guardian_reachable),
        ("File jail", _check_file_jail),
        ("Tool profile", _check_tool_profile),
        ("Bypass permissions", _check_bypass_permissions),
    ]

    results = []
    issues = 0
    for label, fn in checks:
        try:
            ok, message, fixable = fn()
            results.append(
                {
                    "check": label,
                    "passed": ok,
                    "message": message,
                    "fixable": fixable,
                }
            )
            if not ok:
                issues += 1
        except Exception as e:
            results.append(
                {
                    "check": label,
                    "passed": False,
                    "message": str(e),
                    "fixable": False,
                }
            )
            issues += 1

    total = len(results)
    return {"total": total, "passed": total - issues, "issues": issues, "results": results}


@app.get("/api/self-audit/reports")
async def get_self_audit_reports():
    """List recent self-audit reports."""
    from pocketpaw.config import get_config_dir

    reports_dir = get_config_dir() / "audit_reports"
    if not reports_dir.exists():
        return []

    import json

    reports = []
    for f in sorted(reports_dir.glob("*.json"), reverse=True)[:20]:
        try:
            data = json.loads(f.read_text())
            reports.append(
                {
                    "date": f.stem,
                    "total": data.get("total_checks", 0),
                    "passed": data.get("passed", 0),
                    "issues": data.get("issues", 0),
                }
            )
        except Exception:
            pass
    return reports


@app.get("/api/self-audit/reports/{date}")
async def get_self_audit_report(date: str):
    """Get a specific self-audit report by date."""
    import json

    from pocketpaw.config import get_config_dir

    report_path = get_config_dir() / "audit_reports" / f"{date}.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return json.loads(report_path.read_text())


@app.post("/api/self-audit/run")
async def run_self_audit_endpoint():
    """Trigger a self-audit run and return the report."""
    from pocketpaw.daemon.self_audit import run_self_audit

    report = await run_self_audit()
    return report


# ==================== Health Engine API ====================


@app.get("/api/health")
async def get_health_status():
    """Get current health engine summary."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        return engine.summary
    except Exception as e:
        return {"status": "unknown", "check_count": 0, "issues": [], "error": str(e)}


@app.get("/api/health/errors")
async def get_health_errors(limit: int = 20, search: str = ""):
    """Get recent errors from the persistent error log."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        return engine.get_recent_errors(limit=limit, search=search)
    except Exception:
        return []


@app.delete("/api/health/errors")
async def clear_health_errors():
    """Clear the persistent error log."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        engine.error_store.clear()
        return {"cleared": True}
    except Exception as e:
        return {"cleared": False, "error": str(e)}


@app.post("/api/health/check")
async def trigger_health_check():
    """Run all health checks (startup + connectivity) and return results."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        await engine.run_all_checks()
        summary = engine.summary
        # Broadcast to all connected clients
        await _broadcast_health_update(summary)
        return summary
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


async def handle_tool(websocket: WebSocket, tool: str, settings: Settings, data: dict):
    """Handle tool execution."""

    if tool == "status":
        # Run blocking status check in thread pool to avoid freezing websocket
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        from pocketpaw.tools.status import get_system_status

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            status = await loop.run_in_executor(pool, get_system_status)
        await websocket.send_json({"type": "status", "content": status})

    elif tool == "screenshot":
        from pocketpaw.tools.screenshot import take_screenshot

        result = take_screenshot()  # sync function

        if isinstance(result, bytes):
            await websocket.send_json(
                {"type": "screenshot", "image": base64.b64encode(result).decode()}
            )
        else:
            await websocket.send_json({"type": "error", "content": result})

    elif tool == "fetch":
        from pocketpaw.tools.fetch import list_directory

        path = data.get("path") or str(Path.home())
        result = list_directory(path, settings.file_jail_path)  # sync function
        await websocket.send_json({"type": "message", "content": result})

    elif tool == "panic":
        await websocket.send_json(
            {"type": "message", "content": "🛑 PANIC: All agent processes stopped!"}
        )
        # TODO: Actually stop agent processes

    else:
        await websocket.send_json({"type": "error", "content": f"Unknown tool: {tool}"})


async def handle_file_navigation(websocket: WebSocket, path: str, settings: Settings):
    """Handle file browser navigation."""
    from pocketpaw.tools.fetch import list_directory

    result = list_directory(path, settings.file_jail_path)  # sync function
    await websocket.send_json({"type": "message", "content": result})


async def handle_file_browse(
    websocket: WebSocket, path: str, settings: Settings, *, context: str | None = None
):
    """Handle file browser - returns structured JSON for the modal.

    If an optional ``context`` string is provided it is echoed back in the
    response so the frontend can route sidebar vs modal file responses.
    """
    from pocketpaw.tools.fetch import is_safe_path

    def _resp(payload: dict) -> dict:
        """Attach context to every response so frontend can route sidebar vs modal."""
        if context:
            payload["context"] = context
        return payload

    # Resolve ~ to home directory
    if path == "~" or path == "":
        resolved_path = Path.home()
    else:
        # Handle relative paths from home
        if not path.startswith("/"):
            resolved_path = Path.home() / path
        else:
            resolved_path = Path(path)

    resolved_path = resolved_path.resolve()
    jail = settings.file_jail_path.resolve()

    # Security check
    if not is_safe_path(resolved_path, jail):
        await websocket.send_json(
            _resp({"type": "files", "error": "Access denied: path outside allowed directory"})
        )
        return

    if not resolved_path.exists():
        await websocket.send_json(_resp({"type": "files", "error": "Path does not exist"}))
        return

    if not resolved_path.is_dir():
        await websocket.send_json(_resp({"type": "files", "error": "Not a directory"}))
        return

    # Build file list
    files = []
    try:
        items = sorted(resolved_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        # Filter hidden files BEFORE applying the limit so dotfiles don't consume quota
        visible_items = [item for item in items if not item.name.startswith(".")]

        for item in visible_items[:50]:  # Limit to 50 visible items
            file_info = {"name": item.name, "isDir": item.is_dir()}

            if not item.is_dir():
                try:
                    size = item.stat().st_size
                    if size < 1024:
                        file_info["size"] = f"{size} B"
                    elif size < 1024 * 1024:
                        file_info["size"] = f"{size / 1024:.1f} KB"
                    else:
                        file_info["size"] = f"{size / (1024 * 1024):.1f} MB"
                except Exception:
                    file_info["size"] = "?"

            files.append(file_info)

    except PermissionError:
        await websocket.send_json(_resp({"type": "files", "error": "Permission denied"}))
        return

    # Calculate relative path from home for display
    try:
        rel_path = resolved_path.relative_to(Path.home())
        display_path = str(rel_path) if str(rel_path) != "." else "~"
    except ValueError:
        display_path = str(resolved_path)

    await websocket.send_json(_resp({"type": "files", "path": display_path, "files": files}))


# =========================================================================
# Memory Settings API
# =========================================================================

_MEMORY_CONFIG_KEYS = {
    "memory_backend": "memory_backend",
    "memory_use_inference": "memory_use_inference",
    "mem0_llm_provider": "mem0_llm_provider",
    "mem0_llm_model": "mem0_llm_model",
    "mem0_embedder_provider": "mem0_embedder_provider",
    "mem0_embedder_model": "mem0_embedder_model",
    "mem0_vector_store": "mem0_vector_store",
    "mem0_ollama_base_url": "mem0_ollama_base_url",
    "mem0_auto_learn": "mem0_auto_learn",
}


@app.get("/api/memory/settings")
async def get_memory_settings():
    """Get current memory backend configuration."""
    settings = Settings.load()
    return {
        "memory_backend": settings.memory_backend,
        "memory_use_inference": settings.memory_use_inference,
        "mem0_llm_provider": settings.mem0_llm_provider,
        "mem0_llm_model": settings.mem0_llm_model,
        "mem0_embedder_provider": settings.mem0_embedder_provider,
        "mem0_embedder_model": settings.mem0_embedder_model,
        "mem0_vector_store": settings.mem0_vector_store,
        "mem0_ollama_base_url": settings.mem0_ollama_base_url,
        "mem0_auto_learn": settings.mem0_auto_learn,
    }


@app.post("/api/memory/settings")
async def save_memory_settings(request: Request):
    """Save memory backend configuration."""
    data = await request.json()
    settings = Settings.load()

    for key, value in data.items():
        settings_field = _MEMORY_CONFIG_KEYS.get(key)
        if settings_field:
            setattr(settings, settings_field, value)

    settings.save()

    # Clear settings cache so memory manager picks up new values
    from pocketpaw.config import get_settings as _get_settings

    _get_settings.cache_clear()

    # Force reload the memory manager with fresh settings
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager(force_reload=True)
    agent_loop.memory = manager
    agent_loop.context_builder.memory = manager

    return {"status": "ok"}


@app.get("/api/memory/stats")
async def get_memory_stats():
    """Get memory backend statistics."""
    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "get_memory_stats"):
        return await store.get_memory_stats()

    # File backend basic stats
    return {
        "backend": "file",
        "total_memories": "N/A (use mem0 for stats)",
    }


def run_dashboard(host: str = "127.0.0.1", port: int = 8888, open_browser: bool = True):
    """Run the dashboard server."""
    global _open_browser_url

    print("\n" + "=" * 50)
    print("🐾 POCKETPAW WEB DASHBOARD")
    print("=" * 50)
    if host == "0.0.0.0":
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "<your-server-ip>"
        print(f"\n🌐 Open http://{local_ip}:{port} in your browser")
        print(f"   (listening on all interfaces — {host}:{port})\n")
    else:
        print(f"\n🌐 Open http://localhost:{port} in your browser\n")

    if open_browser:
        _open_browser_url = f"http://localhost:{port}"

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_dashboard()
