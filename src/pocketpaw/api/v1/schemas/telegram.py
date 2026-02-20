# Telegram schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class TelegramStatusResponse(BaseModel):
    """Telegram pairing status."""

    configured: bool = False
    user_id: int | None = None


class TelegramSetupRequest(BaseModel):
    """Start Telegram pairing."""

    bot_token: str = Field(..., min_length=1)


class TelegramSetupResponse(BaseModel):
    """QR code and deep link for Telegram pairing."""

    qr_url: str = ""
    deep_link: str = ""
    error: str | None = None


class TelegramPairingStatusResponse(BaseModel):
    """Check if Telegram pairing is complete."""

    paired: bool = False
    user_id: int | None = None
