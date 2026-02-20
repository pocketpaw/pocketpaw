# Webhook schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class WebhookSlot(BaseModel):
    name: str
    description: str = ""
    secret: str = ""
    sync_timeout: int = 30
    url: str = ""


class WebhookAddRequest(BaseModel):
    name: str = Field(..., min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = ""
    sync_timeout: int | None = None


class WebhookNameRequest(BaseModel):
    name: str
