"""Channel adapter management for PocketPaw dashboard.

Extracted from dashboard.py — contains _start_channel_adapter(),
_stop_channel_adapter(), and all channel-related REST endpoints:
  - /webhook/whatsapp (GET/POST), /api/whatsapp/qr
  - /webhook/inbound/{webhook_name}
  - /api/webhooks, /api/webhooks/add, /api/webhooks/remove, /api/webhooks/regenerate-secret
  - /api/extras/check, /api/extras/install
  - /api/channels/status, /api/channels/save, /api/channels/toggle
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request

from pocketpaw.bus import get_message_bus
from pocketpaw.config import Settings
from pocketpaw.dashboard_state import (
    _CHANNEL_CONFIG_KEYS,
    _CHANNEL_DEPS,
    _channel_adapters,
    _channel_autostart_enabled,
    _channel_is_configured,
    _channel_is_running,
    _is_module_importable,
)

logger = logging.getLogger(__name__)

channels_router = APIRouter()


# ─── Adapter Lifecycle ───────────────────────────────────────────


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

        if not mode:
            # No WhatsApp mode selected — skip
            return False
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


# ─── WhatsApp Webhook Routes ────────────────────────────────────


@channels_router.get("/webhook/whatsapp")
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


@channels_router.post("/webhook/whatsapp")
async def whatsapp_incoming(request: Request):
    """Incoming WhatsApp messages via webhook."""
    wa = _channel_adapters.get("whatsapp")
    if wa is None:
        return {"status": "not configured"}
    payload = await request.json()
    await wa.handle_webhook_message(payload)
    return {"status": "ok"}


@channels_router.get("/api/whatsapp/qr")
async def get_whatsapp_qr():
    """Get current WhatsApp QR code for neonize pairing."""
    adapter = _channel_adapters.get("whatsapp")
    if adapter is None or not hasattr(adapter, "_qr_data"):
        return {"qr": None, "connected": False}
    return {
        "qr": getattr(adapter, "_qr_data", None),
        "connected": getattr(adapter, "_connected", False),
    }


# ─── Generic Inbound Webhook API ────────────────────────────────


@channels_router.post("/webhook/inbound/{webhook_name}")
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


@channels_router.get("/api/webhooks")
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


@channels_router.post("/api/webhooks/add")
async def add_webhook(request: Request):
    """Create a new webhook slot (auto-generates secret)."""
    import re
    import secrets

    data = await request.json()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Webhook name is required")

    # Validate name: alphanumeric, hyphens, underscores only
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


@channels_router.post("/api/webhooks/remove")
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


@channels_router.post("/api/webhooks/regenerate-secret")
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


# ─── Extras (Optional Dependencies) ─────────────────────────────


@channels_router.get("/api/extras/check")
async def check_extras(channel: str = Query(...)):
    """Check whether a channel's optional dependency is installed."""
    dep = _CHANNEL_DEPS.get(channel)
    if dep is None:
        # Channel has no optional dep (e.g. signal) — always installed
        return {"installed": True, "extra": channel, "package": "", "pip_spec": ""}
    import_mod, package, pip_spec = dep
    installed = _is_module_importable(import_mod)
    return {
        "installed": installed,
        "extra": channel,
        "package": package,
        "pip_spec": pip_spec,
    }


@channels_router.post("/api/extras/install")
async def install_extras(request: Request):
    """Install a channel's optional dependency."""
    data = await request.json()
    extra = data.get("extra", "")

    dep = _CHANNEL_DEPS.get(extra)
    if dep is None:
        raise HTTPException(status_code=400, detail=f"Unknown extra: {extra}")

    import_mod, _package, _pip_spec = dep

    # Already installed?
    if _is_module_importable(import_mod):
        return {"status": "ok"}

    from pocketpaw.bus.adapters import auto_install

    # Map channel name → pip extra name (most match, except whatsapp → whatsapp-personal)
    extra_name = "whatsapp-personal" if extra == "whatsapp" else extra
    try:
        await asyncio.to_thread(auto_install, extra_name, import_mod)
    except RuntimeError as exc:
        return {"error": str(exc)}

    # Clear cached adapter module so _start_channel_adapter can re-import fresh
    import sys

    adapter_modules = [k for k in sys.modules if k.startswith("pocketpaw.bus.adapters.")]
    for mod in adapter_modules:
        del sys.modules[mod]

    return {"status": "ok"}


# ─── Channel Status / Save / Toggle ─────────────────────────────


@channels_router.get("/api/channels/status")
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
            "autostart": _channel_autostart_enabled(ch, settings),
        }
    # Add WhatsApp mode info
    result["whatsapp"]["mode"] = settings.whatsapp_mode
    return result


@channels_router.post("/api/channels/save")
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
        if frontend_key == "autostart":
            settings.channel_autostart[channel] = bool(value)
            continue
        settings_field = key_map.get(frontend_key)
        if settings_field:
            setattr(settings, settings_field, value)

    settings.save()
    return {"status": "ok"}


@channels_router.post("/api/channels/toggle")
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
        except ImportError:
            # Missing optional dependency — return structured response so the
            # frontend can show the install modal instead of a generic error.
            dep = _CHANNEL_DEPS.get(channel)
            if dep:
                _mod, package, pip_spec = dep
                return {
                    "missing_dep": True,
                    "channel": channel,
                    "package": package,
                    "pip_spec": pip_spec,
                }
            return {"error": f"Failed to start {channel}: missing dependency"}
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
