# Channels router — status, save, toggle + extras check/install.
# Created: 2026-02-20
# Updated: 2026-02-25 — Fix WhatsApp QR import path, revert install_extras to
#   return error JSON (200) instead of HTTP 500 to avoid breaking dashboard JS.

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
        _is_module_importable,
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
        except ImportError as exc:
            dep = _CHANNEL_DEPS.get(channel)
            if dep:
                _mod, package, pip_spec = dep
                # Check if the main dep is actually installed — the ImportError
                # might be from a sub-module or transitive dependency.
                installed = _is_module_importable(_mod)
                return {
                    "missing_dep": not installed,
                    "channel": channel,
                    "package": package,
                    "pip_spec": pip_spec,
                    "error": str(exc) if installed else None,
                }
            return {"error": f"Failed to start {channel}: {exc}"}
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


@router.get("/whatsapp/qr")
async def get_whatsapp_qr():
    """Get current WhatsApp QR code for neonize pairing."""
    from pocketpaw.dashboard_state import _channel_adapters

    adapter = _channel_adapters.get("whatsapp")
    if adapter is None or not hasattr(adapter, "_qr_data"):
        return {"qr": None, "connected": False}
    return {
        "qr": getattr(adapter, "_qr_data", None),
        "connected": getattr(adapter, "_connected", False),
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
        return {"status": "noop", "reason": f"No optional dependency for '{extra}'"}

    import_mod, _package, _pip_spec = dep
    if _is_module_importable(import_mod):
        return {"status": "ok"}

    from pocketpaw.bus.adapters import auto_install

    extra_name = "whatsapp-personal" if extra == "whatsapp" else extra
    try:
        await asyncio.to_thread(auto_install, extra_name, import_mod)
    except RuntimeError as exc:
        return {"error": str(exc)}

    import importlib
    import sys

    # Clear adapter module cache so the next import picks up the new dep
    adapter_modules = [k for k in sys.modules if k.startswith("pocketpaw.bus.adapters.")]
    for mod in adapter_modules:
        del sys.modules[mod]

    # Also clear the installed module itself from sys.modules cache
    top_pkg = import_mod.split(".")[0]
    for key in list(sys.modules):
        if key == top_pkg or key.startswith(top_pkg + "."):
            del sys.modules[key]
    importlib.invalidate_caches()

    # Verify the module is actually importable now
    if not _is_module_importable(import_mod):
        raise HTTPException(
            status_code=500,
            detail=f"Installed but cannot import '{import_mod}'. You may need to restart.",
        )

    return {"status": "ok"}
