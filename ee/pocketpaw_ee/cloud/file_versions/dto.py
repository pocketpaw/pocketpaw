# dto.py — request/response schemas for the file-version write API.
# Created: 2026-06-26 (ART-1) — ported from dewani12's origin/feature/files
#   (ee/cloud/file_versions/dto.py). KEEPS the file-write/version core only:
#   WriteFile{Request,Response}, UpdateFileContent{Request,Response}, and the
#   version list/get DTOs. DROPPED the Slice-D editor transport DTOs
#   (DiffResponse, AiEditRequest, AiEditResponse) — deferred, not ported.
# Updated: 2026-06-26 (ART-1 quality fix loop, I2) — WriteFileRequest gained an
#   optional `mime`; when omitted the service guesses from the filename
#   extension instead of hardcoding application/json.
# Updated: 2026-07-03 (FL-2, port of #1193) — restored DiffResponse (the
#   unified-diff transport DTO ART-1 deferred), now that the diff/revert history
#   helpers land in the service.
# Updated: 2026-07-03 (FL-5, port of #1193) — restored the document editor
#   transport DTOs ART-1 deferred: ``AiEditRequest`` / ``AiEditResponse`` (the
#   POST /files/{id}/ai-edit body + reply) and ``SyncEditingContextRequest`` (the
#   PUT /files/{id}/editing-context body), now that the ai-edit + editing-context
#   routes land on the router. The slides/spreadsheet edit DTOs already live in
#   ``slides_dto`` / ``spreadsheet_dto`` (brought by FL-2). Pure transport, no
#   Beanie — the "FileVersions" import-linter contract still holds.
"""FileVersions DTOs — request/response schemas for the file-version API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WriteFileRequest(BaseModel):
    """Request body for POST /files/write — create or overwrite a file."""

    path: str = Field(min_length=1)  # file id or path
    content: str = Field(min_length=0)  # full file content
    filename: str | None = None  # display name (defaults to path if omitted)
    # Explicit mime. When omitted the service guesses from the filename
    # extension, defaulting to text/plain for extension-less paths.
    mime: str | None = None


class WriteFileResponse(BaseModel):
    """Returned after POST /files/write."""

    file_id: str = Field(alias="fileId")
    version: int
    size_bytes: int = Field(alias="sizeBytes")

    model_config = {"populate_by_name": True}


class UpdateFileContentRequest(BaseModel):
    """Request body for PUT /files/{id}. Content is the full new text."""

    content: str = Field(min_length=0)
    # Expected version for If-Match. Omitted -> force-overwrite (escape hatch).
    expected_version: int | None = Field(default=None, alias="expectedVersion")

    model_config = {"populate_by_name": True}


class UpdateFileContentResponse(BaseModel):
    """Returned after a successful PUT /files/{id}."""

    file_id: str = Field(alias="fileId")
    new_version: int = Field(alias="newVersion")
    size_bytes: int = Field(alias="sizeBytes")
    content_hash: str = Field(alias="contentHash")

    model_config = {"populate_by_name": True}


class FileVersionListItem(BaseModel):
    """One row in the version picker dropdown (content omitted)."""

    id: str
    file_id: str = Field(alias="fileId")
    version_number: int = Field(alias="versionNumber")
    size_bytes: int = Field(alias="sizeBytes")
    editor_kind: str = Field(alias="editorKind")
    editor_id: str = Field(alias="editorId")
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class DiffResponse(BaseModel):
    """Unified diff between two archived versions (GET .../diff/{v2})."""

    from_version: int = Field(alias="fromVersion")
    to_version: int = Field(alias="toVersion")
    diff: str

    model_config = {"populate_by_name": True}


class FileVersionResponse(BaseModel):
    """Full version payload including content (for revert / diff)."""

    id: str
    file_id: str = Field(alias="fileId")
    version_number: int = Field(alias="versionNumber")
    content: str
    content_hash: str = Field(alias="contentHash")
    size_bytes: int = Field(alias="sizeBytes")
    editor_kind: str = Field(alias="editorKind")
    editor_id: str = Field(alias="editorId")
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class AiEditRequest(BaseModel):
    """Request body for POST /files/{id}/ai-edit (Editor.js document)."""

    content: str = Field(min_length=0)  # full document content (Editor.js JSON)
    prompt: str = Field(min_length=1)  # what the user wants the AI to do
    selected_block_id: str | None = Field(default=None, alias="selectedBlockId")
    available_tools: list[str] = Field(default_factory=list, alias="availableTools")

    model_config = {"populate_by_name": True}


class AiEditResponse(BaseModel):
    """AI-edited document blocks returned to the frontend."""

    blocks: list[dict]  # updated Editor.js blocks array
    summary: str = ""  # human-readable summary of changes


class SyncEditingContextRequest(BaseModel):
    """Request body for PUT /files/{id}/editing-context."""

    blocks: list[dict]
