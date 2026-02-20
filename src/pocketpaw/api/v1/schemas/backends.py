# Backend schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class BackendInfo(BaseModel):
    name: str
    displayName: str
    available: bool
    capabilities: list[str] = []
    builtinTools: list[str] = []
    requiredKeys: list[str] = []
    supportedProviders: list[str] = []
    installHint: dict = {}
    beta: bool = False


class BackendInstallRequest(BaseModel):
    backend: str
