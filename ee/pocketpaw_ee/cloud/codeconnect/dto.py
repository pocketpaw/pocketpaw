# dto.py — Request/response DTOs for the Code Mode GitHub connect flow (CM-3).
# Created 2026-07-16 (feat/code-mode): distinct request/response models per Rule 4.
# The connect flow is read-mostly from the client's side (install-url, list
# connections, list repos); the connection is WRITTEN by the GitHub callback, not
# a client body, so there's no CreateConnectionRequest — the callback carries the
# installation id + signed state as query params, validated in the router.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class InstallUrlResponse(BaseModel):
    """The GitHub App install URL the frontend opens to start the connect flow."""

    url: str


class CodeConnectionResponse(BaseModel):
    """One persisted GitHub connection, camelCase for the wire."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    workspaceId: str
    userId: str
    provider: str
    installationId: str
    accountLogin: str | None = None
    avatarUrl: str | None = None
    createdAt: str
    updatedAt: str


class CodeConnectionListResponse(BaseModel):
    """The caller's GitHub connections (for the "connected as" UI state)."""

    connections: list[CodeConnectionResponse]


class RepoResponse(BaseModel):
    """One repo a connection can reach — the shape the repo picker renders."""

    model_config = ConfigDict(populate_by_name=True)

    fullName: str
    private: bool
    defaultBranch: str
    cloneUrl: str


class RepoListResponse(BaseModel):
    """The merged repo listing across the caller's connections."""

    repos: list[RepoResponse]


__all__ = [
    "CodeConnectionListResponse",
    "CodeConnectionResponse",
    "InstallUrlResponse",
    "RepoListResponse",
    "RepoResponse",
]
