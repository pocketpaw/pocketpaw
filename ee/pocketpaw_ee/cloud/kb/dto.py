# dto.py — Wire DTOs for the knowledge base REST API.
# Updated: 2026-08-04 — Living-wiki API: added ReingestRequest and
# ReingestUploadRequest for the new POST /knowledge/reingest and
# POST /knowledge/reingest-upload routes.
"""Wire DTOs for the knowledge base REST API.

Renamed from ``schemas.py`` to ``dto.py`` in Phase 6b for naming
consistency with the rest of the cloud modules. KB is intentionally
NOT refactored to the full hexagonal layout (domain/repositories/
service) because it's an adapter to the external ``kb-go`` binary,
not a domain in its own right; ``backend_adapter.py`` and
``workspace_aggregator.py`` already isolate that concern.
"""

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


class ReingestRequest(BaseModel):
    """POST /knowledge/reingest — re-run an article's raw doc through the funnel."""

    article_id: str = Field(min_length=1)
    scope: str | None = None  # None = caller's active workspace


class ReingestUploadRequest(BaseModel):
    """POST /knowledge/reingest-upload — extract + ingest one uploaded file."""

    upload_id: str = Field(min_length=1)
    scope: str | None = None  # None = caller's active workspace
