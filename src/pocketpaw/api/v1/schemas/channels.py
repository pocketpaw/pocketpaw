# Channel schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class ChannelInfo(BaseModel):
    configured: bool = False
    running: bool = False
    autostart: bool = False


class ChannelStatusResponse(BaseModel):
    """Status of all channel adapters."""

    discord: ChannelInfo = ChannelInfo()
    slack: ChannelInfo = ChannelInfo()
    whatsapp: ChannelInfo = ChannelInfo()
    telegram: ChannelInfo = ChannelInfo()
    signal: ChannelInfo = ChannelInfo()
    matrix: ChannelInfo = ChannelInfo()
    teams: ChannelInfo = ChannelInfo()
    google_chat: ChannelInfo = ChannelInfo()


class ChannelSaveRequest(BaseModel):
    channel: str
    config: dict


class ChannelToggleRequest(BaseModel):
    channel: str
    action: str = Field(..., pattern="^(start|stop)$")
