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

    # Sync user_display_name into USER.md so the agent knows the user's name
    if "user_display_name" in settings_data and settings_data["user_display_name"]:
        try:
            from pocketpaw.config import get_config_dir

            user_file = get_config_dir() / "identity" / "USER.md"
            user_file.parent.mkdir(parents=True, exist_ok=True)
            import re as _re

            # Sanitize display name: strip newlines and limit to safe characters
            raw_name = settings_data["user_display_name"]
            display_name = _re.sub(r"[^\w\s\-.,'\u0080-\uffff]", "", raw_name).strip()[:100]
            if not display_name:
                display_name = "User"
            if user_file.exists():
                content = user_file.read_text(encoding="utf-8")
                import re

                updated = re.sub(
                    r"^Name:\s*.*$",
                    f"Name: {display_name}",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                if updated == content and "Name:" not in content:
                    # No Name: line found, prepend it
                    updated = f"# User Profile\nName: {display_name}\n\n{content}"
                user_file.write_text(updated, encoding="utf-8")
            else:
                user_file.write_text(
                    f"# User Profile\nName: {display_name}\n",
                    encoding="utf-8",
                )
            # Invalidate the identity file cache so changes are picked up immediately
            from pocketpaw.bootstrap.default_provider import _identity_file_cache

            cache_key = str(user_file)
            _identity_file_cache.pop(cache_key, None)
            logger.info("Synced user_display_name '%s' to USER.md", display_name)
        except Exception:
            logger.debug("Could not sync user_display_name to USER.md", exc_info=True)

    # Apply runtime side-effects so changes take effect without restart
    try:
        from pocketpaw.dashboard_state import agent_loop

        agent_loop.reset_router()
        logger.info("Agent router reset after settings update")
    except Exception:
        logger.debug("Could not reset agent router", exc_info=True)

    try:
        from pocketpaw.memory import get_memory_manager

        manager = get_memory_manager()
        if hasattr(manager, "reload"):
            await manager.reload()
    except Exception:
        logger.debug("Could not reload memory manager", exc_info=True)

    result: dict = {"status": "ok"}
    if warnings:
        result["warnings"] = warnings
    return result
