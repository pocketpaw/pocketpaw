# MCP schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class MCPServerAddRequest(BaseModel):
    name: str = Field(..., min_length=1)
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    url: str = ""
    env: dict[str, str] = {}
    enabled: bool = True


class MCPServerNameRequest(BaseModel):
    name: str


class MCPTestRequest(BaseModel):
    name: str = "test"
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    url: str = ""
    env: dict[str, str] = {}


class MCPPresetInstallRequest(BaseModel):
    preset_id: str
    env: dict[str, str] = {}
    extra_args: list[str] | None = None
