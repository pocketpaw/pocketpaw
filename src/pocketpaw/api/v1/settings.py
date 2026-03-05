# Settings router — GET/PUT settings (REST alternative to WS-only).
# Created: 2026-02-20

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request

from pocketpaw.api.deps import require_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Settings"])

# Protects settings read-modify-write from concurrent clients
_settings_lock = asyncio.Lock()


@router.get("/settings", dependencies=[Depends(require_scope("settings:read", "settings:write"))])
async def get_settings():
    """Get current settings (non-secret fields)."""
    from pocketpaw.config import Settings

    settings = Settings.load()
    # Return all non-secret fields as a dict
    data = {}
    for field_name in settings.model_fields:
        val = getattr(settings, field_name, None)
        # Skip internal/secret fields
        if field_name.startswith("_"):
            continue
        # Convert Path objects to strings
        from pathlib import Path

        if isinstance(val, Path):
            val = str(val)
        data[field_name] = val
    return data


@router.put("/settings", dependencies=[Depends(require_scope("settings:write"))])
async def update_settings(request: Request):
    """Update settings fields. Only provided fields are changed."""
    from pocketpaw.config import Settings, get_settings, validate_api_key

    data = await request.json()
    settings_data = data.get("settings", data)

    # Validate API keys — collect warnings but never block save
    warnings = []
    api_key_fields = [
        "anthropic_api_key",
        "openai_api_key",
        "telegram_bot_token",
    ]

    for field in api_key_fields:
        if field in settings_data:
            value = settings_data[field]
            if value:  # Only validate non-empty values
                is_valid, warning = validate_api_key(field, value)
                if not is_valid:
                    warnings.append(warning)

    async with _settings_lock:
        settings = Settings.load()
        for key, value in settings_data.items():
            if hasattr(settings, key) and not key.startswith("_"):
                setattr(settings, key, value)
        settings.save()
        get_settings.cache_clear()

    result: dict = {"status": "ok"}
    if warnings:
        result["warnings"] = warnings
    return result
