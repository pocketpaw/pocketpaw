# Settings router — GET/PUT settings (REST alternative to WS-only).
# Created: 2026-02-20
# Updated: 2026-04-09 — GET /settings now filters SECRET_FIELDS so API keys
# and tokens are never returned over REST, matching the mask applied by
# Settings.to_safe_dict() and the WS settings_get handler.

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from pocketpaw.api.deps import require_scope
from pocketpaw.credentials import SECRET_FIELDS

# Security-critical fields that MUST NOT be modified via the REST API.
# These fields control file-system boundaries, permission checks, prompt-injection
# scanning, and other safety guardrails.  They can only be changed by editing the
# config file or environment variables directly.
_IMMUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "file_jail_path",
        "bypass_permissions",
        "trust_level",
        "injection_scan_enabled",
        "guardian_enabled",
        "localhost_auth_bypass",
        "pii_scan_enabled",
        # Gates the PTY terminal routes. Immutable here for the same reason
        # the routes carry require_scope("admin"): if the settings API could
        # flip it, any admin-scoped key could re-enable a host shell over
        # HTTP, which is the hole one level up.
        "terminal_enabled",
    }
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Settings"])

# Protects settings read-modify-write from concurrent clients
_settings_lock = asyncio.Lock()


@router.get("/settings", dependencies=[Depends(require_scope("settings:read", "settings:write"))])
async def get_settings():
    """Get current settings (non-secret fields).

    Secret fields (API keys, tokens, passwords — see ``SECRET_FIELDS``) are
    never returned over REST. Clients that need to know whether a key is
    configured should check the corresponding ``*_configured`` boolean on
    the health or status endpoint instead.
    """
    from pathlib import Path

    from pocketpaw.config import Settings

    settings = Settings.load()
    data: dict = {}
    for field_name in settings.model_fields:
        # Skip dunder/internal fields
        if field_name.startswith("_"):
            continue
        # Never return secrets over REST
        if field_name in SECRET_FIELDS:
            continue
        val = getattr(settings, field_name, None)
        if isinstance(val, Path):
            val = str(val)
        data[field_name] = val
    return data


def _refuse_global_write_in_cloud(request: Request) -> None:
    """404 the settings write when more than one tenant lives on this box.

    ``Settings.load()`` / ``.save()`` is the single on-disk config for the whole
    PROCESS. There is no workspace anywhere in this handler, and the update is a
    blind ``setattr`` loop over whatever keys the body carries, bounded only by
    ``hasattr`` and ``_IMMUTABLE_FIELDS``. So one caller who passes the gate
    rewrites model routing, provider keys, channel wiring and budget for EVERY
    tenant on the deployment — while the product presents this as workspace
    settings.

    The gate in front of it is currently sound: ``require_scope`` fails closed,
    and the EE bridge grants ``full_access`` only to ``is_superuser``, so a
    workspace owner cannot reach it. This is not a fix for a broken gate. It is
    removing the reason one narrow superuser check is the only thing standing
    between a self-service tenant and everyone else's provider keys.

    HOW "CLOUD" IS DETECTED FROM THE OSS PACKAGE

    This module is in ``pocketpaw``, which must never import ``pocketpaw_ee`` —
    an import-linter contract enforces it — so ``is_multi_tenant_cloud()`` is
    out of reach. ``request.state.workspace_id`` is the seam: the EE auth bridge
    stamps it from the caller's own JWT, and nothing else sets it. If it is
    present, a cloud session reached this route, which means this deployment has
    cloud mounted.

    ``POCKETPAW_ALLOW_GLOBAL_SETTINGS_WRITE=1`` re-opens it for an operator who
    genuinely needs to drive a single-tenant cloud install through this route.

    404 rather than 403: a surface a deployment has turned off should not
    announce itself.

    Per-workspace settings is the real fix and a real project. This is the
    launch-week posture, not the end state.
    """
    import os

    if not getattr(request.state, "workspace_id", None):
        return
    if os.environ.get("POCKETPAW_ALLOW_GLOBAL_SETTINGS_WRITE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        logger.warning(
            "POCKETPAW_ALLOW_GLOBAL_SETTINGS_WRITE is set — honouring a write to "
            "the PROCESS-WIDE settings from a cloud session. This changes config "
            "for every tenant on this deployment."
        )
        return
    raise HTTPException(status_code=404, detail="Not Found")


@router.put("/settings", dependencies=[Depends(require_scope("settings:write"))])
async def update_settings(request: Request):
    """Update settings fields. Only provided fields are changed."""
    from pocketpaw.config import Settings, get_settings, validate_api_key

    _refuse_global_write_in_cloud(request)

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

    # Block writes to security-critical fields
    blocked = _IMMUTABLE_FIELDS.intersection(settings_data)
    if blocked:
        raise HTTPException(
            status_code=403,
            detail=f"Field(s) {', '.join(sorted(blocked))} cannot be modified via the API",
        )

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
