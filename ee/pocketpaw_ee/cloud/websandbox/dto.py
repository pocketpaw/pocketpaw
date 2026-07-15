# dto.py — Request/response DTOs for the Web Cursor Sandbox Registry.
# Created 2026-07-15 (WC-1, feat/websandbox-registry): distinct <Op>Request
# and <Entity>Response classes per ee/cloud Rule 4 — one model is never reused
# for both input and output, so a server-owned field (id, timestamps) can't
# leak into the write surface. Wire shape is camelCase to match the rest of
# the cloud surface.
#
# Changed 2026-07-15 (WC-2, feat/websandbox-vm-provision): added the
# cold-provision + file-tree DTOs — ``OpenSandboxRequest`` (the repo URL to open,
# optional branch), ``TreeEntryResponse`` (one node in the cloned repo's file
# tree), and ``SandboxTreeResponse`` (the tree wrapper carrying the bound Daytona
# id). Same Rule-4 discipline: the open surface accepts only a repo + branch and
# never a server-owned id/status.
#
# Changed 2026-07-15 (WC-S3, feat/websandbox-s3-durability): surfaced the durable
# snapshot pointer on the wire (``WebSandboxResponse.snapshotFileId``) and added
# ``SnapshotResponse`` — the ``{fileId}`` the snapshot endpoint returns after
# landing the workspace tarball in the tenant's blob storage.
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
    snapshotFileId: str | None = None
    createdAt: str  # ISO-8601 UTC
    updatedAt: str  # ISO-8601 UTC


class WebSandboxListResponse(BaseModel):
    items: list[WebSandboxResponse]


# ---------------------------------------------------------------------------
# WC-2 — cold-provision + file tree.
# ---------------------------------------------------------------------------


class OpenSandboxRequest(BaseModel):
    """Open a sandbox against a public repo — cold-provision a VM and clone it.

    Only the repo URL (and an optional branch) crosses the wire; the Daytona
    ``sandbox_id`` and lifecycle ``status`` are server-owned and set by the
    provisioner as the VM boots (Rule 4 — the write surface never accepts them).
    """

    repo: str = Field(..., min_length=1, max_length=1024)
    branch: str | None = Field(default=None, max_length=256)


class TreeEntryResponse(BaseModel):
    """One node in the cloned repo's file tree (a single directory level)."""

    name: str
    isDir: bool
    size: int = 0


class SandboxTreeResponse(BaseModel):
    """The file tree of a ready sandbox, keyed to its bound Daytona id."""

    id: str
    sandboxId: str
    path: str
    entries: list[TreeEntryResponse]


# ---------------------------------------------------------------------------
# WC-S3 — workspace durability (snapshot / restore).
# ---------------------------------------------------------------------------


class SnapshotResponse(BaseModel):
    """The durable pointer minted by a snapshot — the blob-storage FileRecord id."""

    fileId: str


__all__ = [
    "CreateSandboxRequest",
    "OpenSandboxRequest",
    "SandboxTreeResponse",
    "SnapshotResponse",
    "TreeEntryResponse",
    "UpdateStatusRequest",
    "WebSandboxListResponse",
    "WebSandboxResponse",
]
