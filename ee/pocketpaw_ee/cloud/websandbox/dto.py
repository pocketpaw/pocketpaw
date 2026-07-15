# dto.py — Request/response DTOs for the Web Cursor Sandbox Registry.
# Created 2026-07-15 (WC-1, feat/websandbox-registry): distinct <Op>Request
# and <Entity>Response classes per ee/cloud Rule 4 — one model is never reused
# for both input and output, so a server-owned field (id, timestamps) can't
# leak into the write surface. Wire shape is camelCase to match the rest of
# the cloud surface.
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateSandboxRequest(BaseModel):
    """Register (or re-register) a sandbox for a (workspace, user, repo).

    Only ``repo`` is required; the rest are set by the provisioner as the
    sandbox moves through its lifecycle. Creating is idempotent per
    (workspace, user, repo) — see ``service.create_sandbox``.
    """

    repo: str = Field(..., min_length=1, max_length=1024)
    sandbox_id: str | None = Field(default=None, max_length=256)
    status: str = Field(default="pending", max_length=32)
    installation_id: str | None = Field(default=None, max_length=256)


class UpdateStatusRequest(BaseModel):
    """Advance a sandbox's lifecycle state (and optionally bind its id)."""

    status: str = Field(..., max_length=32)
    sandbox_id: str | None = Field(default=None, max_length=256)


class WebSandboxResponse(BaseModel):
    id: str
    workspaceId: str
    userId: str
    repo: str
    status: str
    sandboxId: str | None = None
    installationId: str | None = None
    createdAt: str  # ISO-8601 UTC
    updatedAt: str  # ISO-8601 UTC


class WebSandboxListResponse(BaseModel):
    items: list[WebSandboxResponse]


__all__ = [
    "CreateSandboxRequest",
    "UpdateStatusRequest",
    "WebSandboxListResponse",
    "WebSandboxResponse",
]
