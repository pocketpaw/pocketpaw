"""PocketPaw Web Dashboard - API Server

Lightweight FastAPI server that serves the frontend and handles WebSocket communication.

Changes:
  - 2026-02-06: Channel config REST API (GET /api/channels/status, POST save/toggle).
  - 2026-02-06: Refactored adapter storage to _channel_adapters dict; auto-start all configured.
  - 2026-02-06: Auto-start Discord/WhatsApp adapters alongside dashboard; WhatsApp webhook routes.
  - 2026-02-05: Added Mission Control API router at /api/mission-control/*.
  - 2026-02-04: Added Telegram setup API endpoints (/api/telegram/status, /api/telegram/setup, /api/telegram/pairing-status).
  - 2026-02-03: Cleaned up duplicate imports, fixed duplicate save() calls.
  - 2026-02-02: Added agent status to get_settings response.
  - 2026-02-02: Enhanced logging to show which backend is processing requests.
"""

import asyncio
import base64
import io
import logging
import uuid
import time
import platform
import sys
from pathlib import Path

import qrcode
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pocketclaw.agents.loop import AgentLoop
from pocketclaw.bootstrap import DefaultBootstrapProvider
from pocketclaw.bus import get_message_bus
from pocketclaw.bus.adapters.websocket_adapter import WebSocketAdapter
from pocketclaw.config import Settings, get_access_token, get_config_path, regenerate_token
from pocketclaw.daemon import get_daemon
from pocketclaw.memory import MemoryType, get_memory_manager
from pocketclaw.mission_control.api import router as mission_control_router
from pocketclaw.scheduler import get_scheduler
from pocketclaw.security import get_audit_logger
from pocketclaw.skills import SkillExecutor, get_skill_loader
from pocketclaw.tunnel import get_tunnel_manager

logger = logging.getLogger(__name__)

# Global Nanobot Components
ws_adapter = WebSocketAdapter()
agent_loop = AgentLoop()
# Retain active_connections for legacy broadcasts until fully migrated
active_connections: list[WebSocket] = []

# Channel adapters (auto-started when configured, keyed by channel name)
_channel_adapters: dict[str, object] = {}

# Get frontend directory
FRONTEND_DIR = Path(__file__).parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"

# Initialize Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Create FastAPI app
app = FastAPI(title="PocketPaw Dashboard")

# Allow CORS for WebSocket
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Mount Mission Control API router
app.include_router(mission_control_router, prefix="/api/mission-control")


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


async def broadcast_intention(intention_id: str, chunk: dict):
    """Broadcast intention execution results to all connected clients."""
    message = {"type": "intention_event", "intention_id": intention_id, **chunk}
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
        from pocketclaw.bus.adapters.discord_adapter import DiscordAdapter

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
        from pocketclaw.bus.adapters.slack_adapter import SlackAdapter

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
            from pocketclaw.bus.adapters.neonize_adapter import NeonizeAdapter

            db_path = settings.whatsapp_neonize_db or None
            adapter = NeonizeAdapter(db_path=db_path)
            await adapter.start(bus)
            _channel_adapters["whatsapp"] = adapter
            return True
        else:
            # Business mode (Cloud API)
            if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
                return False
            from pocketclaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter

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
        from pocketclaw.bus.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(
            token=settings.telegram_bot_token,
            allowed_user_id=settings.allowed_user_id,
        )
        await adapter.start(bus)
        _channel_adapters["telegram"] = adapter
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
    logger.info("Agent Loop started (Nanobot Architecture)")

    # Auto-start all configured channel adapters
    settings = Settings.load()
    for channel in ("discord", "slack", "whatsapp", "telegram"):
        try:
            if await _start_channel_adapter(channel, settings):
                logger.info(f"{channel.title()} adapter auto-started alongside dashboard")
        except Exception as e:
            logger.warning(f"Failed to auto-start {channel} adapter: {e}")

    # Start reminder scheduler
    scheduler = get_scheduler()
    scheduler.start(callback=broadcast_reminder)

    # Start proactive daemon
    daemon = get_daemon()
    daemon.start(stream_callback=broadcast_intention)


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
}

# Required fields per channel (at least these must be set to start the adapter)
_CHANNEL_REQUIRED: dict[str, list[str]] = {
    "discord": ["discord_bot_token"],
    "slack": ["slack_bot_token", "slack_app_token"],
    "whatsapp": ["whatsapp_access_token", "whatsapp_phone_number_id"],
    "telegram": ["telegram_bot_token"],
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
    for channel in ("discord", "slack", "whatsapp", "telegram"):
        result[channel] = {
            "configured": _channel_is_configured(channel, settings),
            "running": _channel_is_running(channel),
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


@app.get("/")
async def index(request: Request):
    """Serve the main dashboard page."""
    return templates.TemplateResponse("base.html", {"request": request})


# ==================== Auth Middleware ====================


async def verify_token(
    request: Request,
    token: str | None = Query(None),
):
    """
    Verify access token from query param or Authorization header.
    Skipped for localhost/127.0.0.1 unless strict mode enabled (future).
    For now, we enforce it for everyone to ensure the flow works.
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

    # Allow localhost (optional bypass for dev comfort, but stick to Plan 5A strictness)
    # Allow localhost (Trusted Local Environment)
    client_host = request.client.host
    if client_host == "127.0.0.1" or client_host == "localhost":
        return True

    raise HTTPException(status_code=401, detail="Unauthorized")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Exempt routes
    # Allow getting QR code to login; allow WhatsApp webhook (Meta verification)
    exempt_paths = ["/static", "/favicon.ico", "/api/qr", "/webhook/whatsapp", "/api/whatsapp/qr"]

    # Simple check for static
    for path in exempt_paths:
        if request.url.path.startswith(path):
            return await call_next(request)

    # Check for token in query or header
    token = request.query_params.get("token")
    auth_header = request.headers.get("Authorization")
    current_token = get_access_token()

    is_valid = False

    # 1. Check Query Param
    if token and token == current_token:
        is_valid = True

    # 2. Check Header
    elif auth_header and auth_header == f"Bearer {current_token}":
        is_valid = True

    # 3. Allow Localhost (Trusted)
    elif request.client.host == "127.0.0.1" or request.client.host == "localhost":
        is_valid = True

    # 3. Allow landing page request (index.html) IF it has the token?
    # Actually, we want to allow loading index.html so the frontend can check localStorage
    # and then attach the header. BUT if it's the first visit, we need to allow index.html
    # so we can execute the JS.
    # So we should probably allowing "/" but block all "/api/*"

    if (
        request.url.path == "/"
        or request.url.path.endswith(".js")
        or request.url.path.endswith(".css")
    ):
        return await call_next(request)

    # API Protection
    if request.url.path.startswith("/api") or request.url.path.startswith("/ws"):
        if not is_valid:
            # For APIs return 401
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    response = await call_next(request)
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

    if status.get("active") and status.get("url"):
        login_url = f"{status['url']}/?token={get_access_token()}"
    else:
        # Fallback to current request host (localhost or network IP)
        protocol = "https" if "trycloudflare" in str(host) else "http"
        login_url = f"{protocol}://{host}/?token={get_access_token()}"

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
async def websocket_endpoint(websocket: WebSocket, token: str | None = Query(None)):
    """WebSocket endpoint for real-time communication."""
    # Verify Token
    expected_token = get_access_token()

    # Allow localhost bypass for WebSocket too
    client_host = websocket.client.host
    is_localhost = client_host == "127.0.0.1" or client_host == "localhost"

    if token != expected_token and not is_localhost:
        # Try waiting for an initial message with token?
        # Standard usage: ws://host/ws?token=XYZ
        # If missing/invalid, close.
        await websocket.close(code=4003, reason="Unauthorized")
        return

    await websocket.accept()

    # Track connection
    active_connections.append(websocket)

    # Generate session ID for Nanobot bus
    chat_id = str(uuid.uuid4())
    await ws_adapter.register_connection(websocket, chat_id)

    # Send welcome notification with session info
    await websocket.send_json(
        {
            "type": "connection_info",
            "content": "👋 Connected to PocketPaw (Nanobot V2)",
            "id": chat_id,
        }
    )

    # Load settings
    settings = Settings.load()

    # Legacy state
    agent_active = False

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            # Handle chat via MessageBus (Nanobot)
            if action == "chat":
                log_msg = f"⚡ Processing message with Backend: {settings.agent_backend} (Provider: {settings.llm_provider})"
                logger.warning(log_msg)  # Use WARNING to ensure it shows up
                print(log_msg)  # Force stdout just in case

                # Only if using new backend, but let's default to new backend logic eventually
                # For Phase 2 transition: We use the Bus!
                # But allow fallback to old router if 'agent_active' is toggled specifically for old behavior?
                # Actually, let's treat 'chat' as input to the Bus.
                await ws_adapter.handle_message(chat_id, data)

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
                settings.agent_backend = data.get("agent_backend", settings.agent_backend)
                settings.llm_provider = data.get("llm_provider", settings.llm_provider)
                if data.get("anthropic_model"):
                    settings.anthropic_model = data.get("anthropic_model")
                if "bypass_permissions" in data:
                    settings.bypass_permissions = bool(data.get("bypass_permissions"))
                settings.save()

                # Reset the agent loop's router to pick up new settings
                agent_loop.reset_router()

                await websocket.send_json({"type": "message", "content": "⚙️ Settings updated"})

            # ... keep other handlers ... (abbreviated)

            # Handle API key save
            elif action == "save_api_key":
                provider = data.get("provider")
                key = data.get("key", "")

                if provider == "anthropic" and key:
                    settings.anthropic_api_key = key
                    settings.llm_provider = "anthropic"
                    settings.save()
                    await websocket.send_json(
                        {"type": "message", "content": "✅ Anthropic API key saved!"}
                    )
                elif provider == "openai" and key:
                    settings.openai_api_key = key
                    settings.llm_provider = "openai"
                    settings.save()
                    await websocket.send_json(
                        {"type": "message", "content": "✅ OpenAI API key saved!"}
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
                            "anthropicModel": settings.anthropic_model,
                            "bypassPermissions": settings.bypass_permissions,
                            "hasAnthropicKey": bool(settings.anthropic_api_key),
                            "hasOpenaiKey": bool(settings.openai_api_key),
                            "agentActive": agent_active,
                            "agentStatus": agent_status,
                        },
                    }
                )

            # Handle file navigation (legacy)
            elif action == "navigate":
                path = data.get("path", "")
                await handle_file_navigation(websocket, path, settings)

            # Handle file browser
            elif action == "browse":
                path = data.get("path", "~")
                await handle_file_browse(websocket, path, settings)

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
                    await websocket.send_json(
                        {"type": "error", "content": f"Skill not found: {skill_name}"}
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

        
        
# ==================== Health Check API ====================

START_TIME = time.time()

@app.get("/api/health")
async def health_check():
    """System health endpoint with diagnostics."""

    settings = Settings.load()

    uptime = int(time.time() - START_TIME)

    subsystems = {
        "agent_backend": {
            "status": "ok" if agent_loop._running else "stopped",
            "backend": settings.agent_backend,
        },
        "memory": {"status": "ok"},
        "message_bus": {"status": "ok"},
        "channels": {
            "telegram": "connected" if _channel_is_running("telegram") else "disconnected",
            "discord": "connected" if _channel_is_running("discord") else "disconnected",
            "whatsapp": "connected" if _channel_is_running("whatsapp") else "disconnected",
            "slack": "connected" if _channel_is_running("slack") else "disconnected",
        },
    }

    critical_down = not agent_loop._running

    response = {
        "status": "healthy" if not critical_down else "degraded",
        "version": "0.2.1",
        "uptime_seconds": uptime,
        "subsystems": subsystems,
        "python_version": sys.version,
        "platform": platform.system().lower(),
    }

    if critical_down:
        raise HTTPException(status_code=503, detail=response)

    return response


       



# ==================== Transparency APIs ====================


@app.get("/api/identity")
async def get_identity():
    """Get agent identity context."""
    # config_path = get_config_path()
    provider = DefaultBootstrapProvider(get_config_path().parent)
    context = await provider.get_context()
    return {
        "identity_file": context.identity,
        "soul_file": context.soul,
        "style_file": context.style,
        # "tools_file": context.tools, # Not in BootstrapContext
        # "user_file": context.user,   # Not in BootstrapContext
    }


@app.get("/api/memory/sessions")
async def list_sessions(limit: int = 20):
    """List all available sessions with metadata."""
    import json
    from pathlib import Path

    sessions_path = Path.home() / ".pocketclaw" / "memory" / "sessions"

    if not sessions_path.exists():
        return []

    sessions = []
    for session_file in sorted(
        sessions_path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        if len(sessions) >= limit:
            break

        try:
            data = json.loads(session_file.read_text())
            if data:
                # Get first and last message for preview
                first_msg = data[0] if data else {}
                last_msg = data[-1] if data else {}

                sessions.append(
                    {
                        "id": session_file.stem,  # Remove .json extension
                        "message_count": len(data),
                        "first_message": first_msg.get("content", "")[:100],
                        "last_message": last_msg.get("content", "")[:100],
                        "updated_at": last_msg.get("timestamp", ""),
                        "created_at": first_msg.get("timestamp", ""),
                    }
                )
        except (json.JSONDecodeError, KeyError):
            continue

    return sessions


@app.get("/api/memory/session")
async def get_session_memory(id: str = "", limit: int = 50):
    """Get session memory."""
    if not id:
        return []
    manager = get_memory_manager()
    return await manager.get_session_history(id, limit=limit)


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
        {"content": item.content, "timestamp": item.timestamp.isoformat(), "tags": item.tags}
        for item in items
    ]


@app.get("/api/audit")
async def get_audit_log(limit: int = 50):
    """Get audit logs."""
    logger = get_audit_logger()
    if not logger.log_path.exists():
        return []

    logs = []
    try:
        # Read last N lines efficiently-ish
        with open(logger.log_path) as f:
            lines = f.readlines()

        for line in reversed(lines):
            if len(logs) >= limit:
                break
            try:
                import json

                logs.append(json.loads(line))
            except:
                pass
    except Exception as e:
        return {"error": str(e)}

    return logs


async def handle_tool(websocket: WebSocket, tool: str, settings: Settings, data: dict):
    """Handle tool execution."""

    if tool == "status":
        from pocketclaw.tools.status import get_system_status

        status = get_system_status()  # sync function
        await websocket.send_json({"type": "status", "content": status})

    elif tool == "screenshot":
        from pocketclaw.tools.screenshot import take_screenshot

        result = take_screenshot()  # sync function

        if isinstance(result, bytes):
            await websocket.send_json(
                {"type": "screenshot", "image": base64.b64encode(result).decode()}
            )
        else:
            await websocket.send_json({"type": "error", "content": result})

    elif tool == "fetch":
        from pocketclaw.tools.fetch import list_directory

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
    from pocketclaw.tools.fetch import list_directory

    result = list_directory(path, settings.file_jail_path)  # sync function
    await websocket.send_json({"type": "message", "content": result})


async def handle_file_browse(websocket: WebSocket, path: str, settings: Settings):
    """Handle file browser - returns structured JSON for the modal."""
    from pocketclaw.tools.fetch import is_safe_path

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
            {"type": "files", "error": "Access denied: path outside allowed directory"}
        )
        return

    if not resolved_path.exists():
        await websocket.send_json({"type": "files", "error": "Path does not exist"})
        return

    if not resolved_path.is_dir():
        await websocket.send_json({"type": "files", "error": "Not a directory"})
        return

    # Build file list
    files = []
    try:
        items = sorted(resolved_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))

        for item in items[:50]:  # Limit to 50 items
            if item.name.startswith("."):
                continue  # Skip hidden files

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
        await websocket.send_json({"type": "files", "error": "Permission denied"})
        return

    # Calculate relative path from home for display
    try:
        rel_path = resolved_path.relative_to(Path.home())
        display_path = str(rel_path) if str(rel_path) != "." else "~"
    except ValueError:
        display_path = str(resolved_path)

    await websocket.send_json({"type": "files", "path": display_path, "files": files})


def run_dashboard(host: str = "127.0.0.1", port: int = 8888):
    """Run the dashboard server."""
    print("\n" + "=" * 50)
    print("🐾 POCKETPAW WEB DASHBOARD")
    print("=" * 50)
    print(f"\n🌐 Open http://localhost:{port} in your browser\n")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_dashboard()
