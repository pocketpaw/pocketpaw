# Webhooks router — CRUD + regenerate secret.
# Created: 2026-02-20

from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.common import StatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"], dependencies=[Depends(require_scope("admin"))])


@router.get("/webhooks")
async def list_webhooks(request: Request):
    """List all configured webhook slots with generated URLs."""
    from pocketpaw.config import Settings

    settings = Settings.load()
    host = request.headers.get("host", f"localhost:{settings.web_port}")
    protocol = "https" if "trycloudflare" in host else "http"

    slots = []
    for cfg in settings.webhook_configs:
        name = cfg.get("name", "")
        secret = cfg.get("secret", "")
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


@router.post("/webhooks/add")
async def add_webhook(request: Request):
    """Create a new webhook slot (auto-generates secret)."""
    from pocketpaw.config import Settings

    data = await request.json()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Webhook name is required")

    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise HTTPException(
            status_code=400,
            detail="Webhook name must be alphanumeric (hyphens and underscores allowed)",
        )

    settings = Settings.load()
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


@router.post("/webhooks/remove", response_model=StatusResponse)
async def remove_webhook(request: Request):
    """Remove a webhook slot by name."""
    from pocketpaw.config import Settings

    data = await request.json()
    name = data.get("name", "")

    settings = Settings.load()
    original_len = len(settings.webhook_configs)
    settings.webhook_configs = [c for c in settings.webhook_configs if c.get("name") != name]

    if len(settings.webhook_configs) == original_len:
        raise HTTPException(status_code=404, detail=f"Webhook '{name}' not found")

    settings.save()
    return StatusResponse()


@router.post("/webhooks/regenerate-secret")
async def regenerate_webhook_secret(request: Request):
    """Regenerate a webhook slot's secret."""
    from pocketpaw.config import Settings

    data = await request.json()
    name = data.get("name", "")

    settings = Settings.load()
    for cfg in settings.webhook_configs:
        if cfg.get("name") == name:
            cfg["secret"] = secrets.token_urlsafe(32)
            settings.save()
            return {"status": "ok", "secret": cfg["secret"]}

    raise HTTPException(status_code=404, detail=f"Webhook '{name}' not found")
