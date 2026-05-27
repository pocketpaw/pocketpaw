# dto.py — Request/response DTOs for the cloud Skills entity.
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — distinct
# request and response models for POST /skills/api-doc (Rule 4). The
# request carries the optional backend name; the multipart file itself
# is bound by FastAPI's UploadFile in the router. The response carries
# the installed skill slug.
# Updated: 2026-05-23 — add ``InstallApiDocFromUrlRequest`` for the
# JSON-body POST /skills/api-doc-from-url surface; well-known APIs
# publish their OpenAPI at a stable URL so the user pastes that
# instead of downloading + re-uploading a JSON file.
from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class InstallApiDocRequest(BaseModel):
    """Non-file fields of a POST /skills/api-doc multipart request.

    The ``file`` part is bound separately by the router as an
    ``UploadFile``; this DTO carries only the optional backend display
    ``name`` so the service can re-validate at entry (Rule 6).
    """

    name: str | None = Field(default=None, max_length=200)


class InstallApiDocFromUrlRequest(BaseModel):
    """Body of a POST /skills/api-doc-from-url JSON request.

    ``url`` is the public OpenAPI / Swagger URL the cloud server
    fetches. ``HttpUrl`` enforces a well-formed http(s) URL at parse
    time — the service layer additionally enforces https-only and the
    SSRF guard against private / loopback / link-local addresses
    (mirrors the read-source executor's posture in pockets/).
    """

    url: HttpUrl
    name: str | None = Field(default=None, max_length=200)


class InstallApiDocResponse(BaseModel):
    """Result of an API-doc skill install."""

    ok: bool = True
    slug: str = Field(..., description="Slug of the installed skill, e.g. api-example-com.")


__all__ = [
    "InstallApiDocFromUrlRequest",
    "InstallApiDocRequest",
    "InstallApiDocResponse",
]
