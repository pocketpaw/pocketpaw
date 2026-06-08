"""Slides DTOs -- request/response schemas for AI editing + context sync."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SlidesEditRequest(BaseModel):
    """Request body for POST /files/{id}/slides-edit."""

    content: dict[str, Any]  # full slides deck JSON
    prompt: str = Field(min_length=1)  # what the user wants the AI to do
    selected_slide_id: str | None = Field(default=None, alias="selectedSlideId")
    available_tools: list[str] = Field(default_factory=list, alias="availableTools")

    model_config = {"populate_by_name": True}


class SlidesEditResponse(BaseModel):
    """AI-edited slides deck returned to the frontend."""

    content: dict[str, Any]  # updated slides deck JSON
    summary: str = ""  # human-readable summary of changes


class SyncSlidesContextRequest(BaseModel):
    """Request body for PUT /files/{id}/slides-context."""

    content: dict[str, Any]
    selected_slide_id: str | None = Field(default=None, alias="selectedSlideId")

    model_config = {"populate_by_name": True}
