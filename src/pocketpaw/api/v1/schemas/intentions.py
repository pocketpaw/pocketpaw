# Intention schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class IntentionTrigger(BaseModel):
    """Trigger configuration for an intention."""

    type: str = "cron"
    schedule: str = ""


class IntentionInfo(BaseModel):
    """A single intention entry."""

    id: str
    name: str
    prompt: str
    trigger: IntentionTrigger | dict = {}
    context_sources: list[str] = []
    enabled: bool = True
    created_at: str = ""
    last_run: str | None = None
    next_run: str | None = None


class IntentionListResponse(BaseModel):
    """List of intentions."""

    intentions: list[IntentionInfo]


class CreateIntentionRequest(BaseModel):
    """Create a new intention."""

    name: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=10000)
    trigger: dict = {}
    context_sources: list[str] = []
    enabled: bool = True


class UpdateIntentionRequest(BaseModel):
    """Partial update of an intention."""

    name: str | None = None
    prompt: str | None = None
    trigger: dict | None = None
    context_sources: list[str] | None = None
    enabled: bool | None = None


class IntentionResponse(BaseModel):
    """Single intention in response."""

    intention: IntentionInfo


class DeleteIntentionResponse(BaseModel):
    """Confirmation of deleted intention."""

    id: str
    deleted: bool = True


class RunIntentionResponse(BaseModel):
    """Acknowledgement that intention was triggered."""

    id: str
    status: str = "running"
    message: str = ""
