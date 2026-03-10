# File browser schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class FileEntry(BaseModel):
    """A single file or directory entry."""

    name: str
    isDir: bool = False
    size: str = ""


class BrowseResponse(BaseModel):
    """File browser listing."""

    path: str
    files: list[FileEntry] = []
    error: str | None = None


class OpenPathRequest(BaseModel):
    """Request to open a file or folder in the client explorer."""

    path: str
    action: str = "navigate"  # "navigate" or "view"


class OpenPathResponse(BaseModel):
    """Response for open-path request."""

    ok: bool
    error: str | None = None


class RecentFileEntry(BaseModel):
    """A recently accessed file from agent tool usage."""

    path: str
    name: str
    is_dir: bool = False
    extension: str = ""
    timestamp: float = 0
    tool: str = ""


class RecentFilesResponse(BaseModel):
    """List of recently accessed files."""

    files: list[RecentFileEntry] = []
