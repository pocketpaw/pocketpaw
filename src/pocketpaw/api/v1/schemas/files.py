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
