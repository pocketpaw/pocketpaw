"""FileVersions DTOs — request/response schemas for the inline-edit API.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UpdateFileContentRequest(BaseModel):
    """Request body for PUT /files/{id}. Content is the full new text."""

    content: str = Field(min_length=0)
    # Expected version for If-Match. Omitted → force-overwrite (escape hatch).
    expected_version: int | None = Field(default=None, alias="expectedVersion")

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


class UpdateFileContentResponse(BaseModel):
    """Returned after a successful PUT /files/{id}."""

    file_id: str = Field(alias="fileId")
    new_version: int = Field(alias="newVersion")
    size_bytes: int = Field(alias="sizeBytes")
    content_hash: str = Field(alias="contentHash")

    model_config = {"populate_by_name": True}


class DiffResponse(BaseModel):
    """Unified diff between two versions."""

    from_version: int = Field(alias="fromVersion")
    to_version: int = Field(alias="toVersion")
    diff: str


class WriteFileRequest(BaseModel):
    """Request body for POST /files/write — create or overwrite a file."""

    path: str = Field(min_length=1)  # file id or path
    content: str = Field(min_length=0)  # full file content
    filename: str | None = None  # display name (defaults to path if omitted)


class WriteFileResponse(BaseModel):
    """Returned after POST /files/write."""

    file_id: str = Field(alias="fileId")
    version: int
    size_bytes: int = Field(alias="sizeBytes")

    model_config = {"populate_by_name": True}


class AiEditRequest(BaseModel):
    """Request body for POST /files/{id}/ai-edit."""

    content: str = Field(min_length=0)  # full document content (Editor.js JSON)
    prompt: str = Field(min_length=1)  # what the user wants the AI to do
    selected_block_id: str | None = Field(default=None, alias="selectedBlockId")
    available_tools: list[str] = Field(default_factory=list, alias="availableTools")

    model_config = {"populate_by_name": True}


class AiEditResponse(BaseModel):
    """AI-edited document blocks returned to the frontend."""

    blocks: list[dict]  # updated Editor.js blocks array
    summary: str = ""  # human-readable summary of changes

