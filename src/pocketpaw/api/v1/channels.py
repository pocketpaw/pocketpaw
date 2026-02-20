# Channels router — status, save, toggle + extras check/install.
# Created: 2026-02-20

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.common import StatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Channels"], dependencies=[Depends(require_scope("channels"))])


@router.get("/channels/status")
async def get_channels_status():
    """Get status of all channel adapters."""
    from pocketpaw.config import Settings
    from pocketpaw.dashboard import (
        _channel_autostart_enabled,
        _channel_is_configured,
        _channel_is_running,
    )

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
    result["whatsapp"]["mode"] = settings.whatsapp_mode
    return result


@router.post("/channels/save", response_model=StatusResponse)
async def save_channel_config(request: Request):
    """Save token/config for a channel."""
    from pocketpaw.config import Settings
    from pocketpaw.dashboard import _CHANNEL_CONFIG_KEYS

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
    return StatusResponse()


@router.post("/channels/toggle")
async def toggle_channel(request: Request):
    """Start or stop a channel adapter dynamically."""
    from pocketpaw.config import Settings
    from pocketpaw.dashboard import (
        _CHANNEL_CONFIG_KEYS,
        _CHANNEL_DEPS,
        _channel_is_configured,
        _channel_is_running,
        _start_channel_adapter,
        _stop_channel_adapter,
    )

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
        except ImportError:
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
        except Exception as e:
            return {"error": f"Failed to stop {channel}: {e}"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    return {
        "channel": channel,
        "configured": _channel_is_configured(channel, settings),
        "running": _channel_is_running(channel),
    }


@router.get("/extras/check")
async def check_extras(channel: str = Query(...)):
    """Check whether a channel's optional dependency is installed."""
    from pocketpaw.dashboard import _CHANNEL_DEPS, _is_module_importable

    dep = _CHANNEL_DEPS.get(channel)
    if dep is None:
        return {"installed": True, "extra": channel, "package": "", "pip_spec": ""}
    import_mod, package, pip_spec = dep
    installed = _is_module_importable(import_mod)
    return {"installed": installed, "extra": channel, "package": package, "pip_spec": pip_spec}


@router.post("/extras/install")
async def install_extras(request: Request):
    """Install a channel's optional dependency."""
    import asyncio

    from pocketpaw.dashboard import _CHANNEL_DEPS, _is_module_importable

    data = await request.json()
    extra = data.get("extra", "")

    dep = _CHANNEL_DEPS.get(extra)
    if dep is None:
        raise HTTPException(status_code=400, detail=f"Unknown extra: {extra}")

    import_mod, _package, _pip_spec = dep
    if _is_module_importable(import_mod):
        return {"status": "ok"}

    from pocketpaw.bus.adapters import auto_install

    extra_name = "whatsapp-personal" if extra == "whatsapp" else extra
    try:
        await asyncio.to_thread(auto_install, extra_name, import_mod)
    except RuntimeError as exc:
        return {"error": str(exc)}

    import sys

    adapter_modules = [k for k in sys.modules if k.startswith("pocketpaw.bus.adapters.")]
    for mod in adapter_modules:
        del sys.modules[mod]

    return {"status": "ok"}
