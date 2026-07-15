# dto.py — Request/response DTOs for the Code Mode Project registry (CM-2a).
# Created 2026-07-16 (feat/code-mode): distinct <Op>Request and <Entity>Response
# classes per ee/cloud Rule 4. The write surface accepts ONLY what a client may
# set — repo (+ optional name/provider). Server-owned runtime state
# (snapshot_file_id, current_sandbox_id, timestamps) is NEVER accepted on the
# wire, the same discipline that closed the WebSandbox cross-tenant hole. Wire
# shape is camelCase to match the rest of the cloud surface.
#
# The ``open`` endpoint does not have its own request/response here — it takes the
# project id from the path and returns a ``WebSandboxResponse`` (the ready
# runtime sandbox to connect to), reusing the websandbox contract.
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    """Client-facing create body — a repo (+ optional name/provider) ONLY.

    Idempotent per (workspace, user, provider, repo): a second create for the
    same repo returns the existing project. Server-owned fields (the snapshot
    pointer, the current sandbox, timestamps) are never accepted here.
    """

    repo: str = Field(..., min_length=1, max_length=1024)
    name: str | None = Field(default=None, max_length=200)
    provider: str = Field(default="github", max_length=32)


class CodeProjectResponse(BaseModel):
    id: str
    workspaceId: str
    userId: str
    name: str
    provider: str
    repo: str
    snapshotFileId: str | None = None
    currentSandboxId: str | None = None
    lastOpenedAt: str | None = None  # ISO-8601 UTC, or null
    createdAt: str  # ISO-8601 UTC
    updatedAt: str  # ISO-8601 UTC


class CodeProjectListResponse(BaseModel):
    items: list[CodeProjectResponse]


__all__ = [
    "CodeProjectListResponse",
    "CodeProjectResponse",
    "CreateProjectRequest",
]
