"""Cycles domain — request and response schemas.

Pydantic models that cross the HTTP boundary. Distinct Request and
Response shapes per the ee/cloud rule — never reuse a model for input
and output (fields leak silently).

Domain → DTO mapping lives in ``service.py`` as private helpers. The
``Response`` shapes here are deliberately denormalized into wire-friendly
ISO strings so the frontend doesn't have to parse timezones.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CycleStatusLiteral = Literal["active", "upcoming", "completed"]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateCycleRequest(BaseModel):
    """Body for ``POST /cycles``.

    ``status`` defaults to ``upcoming``; the service promotes a cycle to
    ``active`` only when explicitly created with that status (and the
    overlap rule passes) or via the snapshot job once ``start`` arrives.
    """

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    pocket_id: str | None = None
    project_id: str | None = None
    start: date
    end: date
    status: CycleStatusLiteral = "upcoming"

    @model_validator(mode="after")
    def _check_dates(self) -> CreateCycleRequest:
        if self.start >= self.end:
            raise ValueError("cycle.invalid_range: start must be before end")
        return self


class UpdateCycleRequest(BaseModel):
    """Body for ``PATCH /cycles/{id}``.

    Per spec only ``upcoming`` cycles can have their name / description /
    dates edited — the router enforces the status check via the service.
    Status transitions go through ``POST /cycles/{id}/close`` (or the
    snapshot job's auto-promote) rather than this PATCH endpoint.

    ``project_id`` is editable on any status — moving an in-flight cycle
    between projects is a low-risk operation (just a grouping change).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    start: date | None = None
    end: date | None = None
    project_id: str | None = None

    @model_validator(mode="after")
    def _check_dates(self) -> UpdateCycleRequest:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("cycle.invalid_range: start must be before end")
        return self


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class CycleDailyPointResponse(BaseModel):
    """One point on the burnup chart's daily series."""

    date: str  # ISO date — "2026-05-13"
    scope: int
    started: int
    completed: int
    is_weekend: bool


class CycleListItemResponse(BaseModel):
    """Lighter shape for ``GET /cycles`` (no daily series).

    The Cycles tab's left list renders dozens of these — keeping the
    daily array off the wire here is a ~10x payload reduction per row on
    a long engagement (90+ snapshot points).
    """

    id: str
    workspace_id: str
    name: str
    description: str
    pocket_id: str | None
    project_id: str | None = None
    start: str  # ISO date
    end: str  # ISO date
    status: CycleStatusLiteral
    scope: int
    started: int
    completed: int
    created_by: str
    created_at: str | None
    updated_at: str | None


class CycleResponse(CycleListItemResponse):
    """Full cycle detail including the daily snapshot series.

    ``GET /cycles/{id}`` returns this; the burnup chart consumes
    ``daily`` directly. The base fields are inherited from
    ``CycleListItemResponse`` to keep the shape consistent across the
    two endpoints — any new metadata added to one shows up on both.
    """

    daily: list[CycleDailyPointResponse] = Field(default_factory=list)


__all__ = [
    "CreateCycleRequest",
    "CycleDailyPointResponse",
    "CycleListItemResponse",
    "CycleResponse",
    "CycleStatusLiteral",
    "UpdateCycleRequest",
]
