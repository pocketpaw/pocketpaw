"""Wire DTOs for the per-session listing/revoke endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class SessionOut(BaseModel):
    id: str
    jti: str
    ip: str | None
    device_label: str
    issued_at: str | None
    last_seen_at: str | None
    is_current: bool

    model_config = {"populate_by_name": True}


class RevokeOthersResponse(BaseModel):
    revoked: int


__all__ = ["RevokeOthersResponse", "SessionOut"]
