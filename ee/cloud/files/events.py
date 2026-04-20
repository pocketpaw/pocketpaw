"""Domain events published by providers on file mutations.

Subscribers (realtime bridge in Phase 4) consume these via the
ee.cloud.realtime bus. Phase 1-2 only defines the shapes.
"""
from __future__ import annotations

from pydantic import BaseModel

from ee.cloud.files.schemas import FileEntry


class FileAdded(BaseModel):
    entry: FileEntry


class FileUpdated(BaseModel):
    entry: FileEntry


class FileRemoved(BaseModel):
    id: str
    workspace_id: str | None
    provider_id: str


class FileMoved(BaseModel):
    entry: FileEntry
    old_path: str
