# spreadsheet_dto.py — request/response schemas for spreadsheet AI-edit + sync.
# Created: 2026-07-03 (FL-2, port of #1193) — ported from dewani12's
#   origin/feature/files (ee/cloud/file_versions/spreadsheet_dto.py). Pure
#   transport DTOs (no Beanie, no I/O); the spreadsheet AI-edit routes that
#   consume them land in FL-5, but the schemas are cohesive with this package so
#   they're brought now. Import-linter "FileVersions" contract lists this module
#   as a Beanie-free source.
"""Spreadsheet DTOs — request/response schemas for AI editing + context sync."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SpreadsheetEditRequest(BaseModel):
    """Request body for POST /files/{id}/spreadsheet-edit."""

    content: str = Field(min_length=0)  # full workbook snapshot JSON
    prompt: str = Field(min_length=1)  # what the user wants the AI to do
    selected_sheet: str | None = Field(default=None, alias="selectedSheet")
    available_tools: list[str] = Field(default_factory=list, alias="availableTools")

    model_config = {"populate_by_name": True}


class SpreadsheetEditResponse(BaseModel):
    """AI-edited workbook snapshot returned to the frontend."""

    snapshot: dict[str, Any]  # updated IWorkbookData snapshot
    summary: str = ""  # human-readable summary of changes


class SyncSpreadsheetContextRequest(BaseModel):
    """Request body for PUT /files/{id}/spreadsheet-context."""

    snapshot: dict[str, Any]
    selected_sheet: str | None = Field(default=None, alias="selectedSheet")

    model_config = {"populate_by_name": True}
