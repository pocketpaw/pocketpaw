# Created: Request/response schemas for the knowledge base REST API.
# SearchRequest, IngestTextRequest, IngestUrlRequest, LintRequest.
"""Knowledge base domain — request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    scope: str | None = None  # Override workspace scope (optional)
    limit: int = Field(default=10, ge=1, le=100)


class IngestTextRequest(BaseModel):
    text: str = Field(min_length=1)
    source: str = "manual"
    scope: str | None = None


class IngestUrlRequest(BaseModel):
    url: str = Field(min_length=1)
    scope: str | None = None


class LintRequest(BaseModel):
    scope: str | None = None
