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
