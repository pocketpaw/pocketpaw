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


class WriteFileRequest(BaseModel):
    """Request to overwrite (or create) a file's content."""

    path: str
    content: str


class CreateFileRequest(BaseModel):
    """Request to create a new file with optional initial content."""

    path: str
    content: str = ""


class MkdirRequest(BaseModel):
    """Request to create a directory."""

    path: str
    parents: bool = False


class RenameRequest(BaseModel):
    """Request to rename or move a file or directory."""

    path: str
    new_path: str


class DeleteRequest(BaseModel):
    """Request to delete a file or directory."""

    path: str
    recursive: bool = False


class FileActionResponse(BaseModel):
    """Generic response for file mutation operations."""

    ok: bool
    path: str | None = None
    error: str | None = None
