# ee/pocketpaw_ee/cloud/growth/dto.py — request/response DTOs for the prospect
# entity. Distinct Request and Response shapes per the ee/cloud rule — never
# reuse a model for input and output. ``domain`` (the dedupe key) is normalised
# to a bare lowercase hostname at the DTO boundary so every caller — router,
# upsert, later ingestion slices — dedupes on the same canonical form.
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth — the prospect
# store. Domain → DTO mapping lives in ``service.py`` as private helpers.

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from pocketpaw_ee.cloud.growth.domain import ProspectSource, ProspectStatus, ProspectTier


def _normalise_domain(v: str) -> str:
    """Canonicalise a company-website domain for the dedupe key.

    Lowercases, strips whitespace, drops an ``http(s)://`` scheme, a ``www.``
    prefix, and any path/port suffix — so ``https://www.Acme.com/about`` and
    ``acme.com`` dedupe to the same row.
    """
    v = v.strip().lower()
    for scheme in ("https://", "http://"):
        if v.startswith(scheme):
            v = v[len(scheme) :]
            break
    v = v.split("/", 1)[0].split(":", 1)[0]
    if v.startswith("www."):
        v = v[len("www.") :]
    return v


class CreateProspectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=253)
    source: ProspectSource
    tier: ProspectTier = "unqualified"
    research_brief: str = ""
    emails: list[str] = Field(default_factory=list)
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool = False
    status: ProspectStatus = "new"

    @field_validator("domain")
    @classmethod
    def _clean_domain(cls, v: str) -> str:
        cleaned = _normalise_domain(v)
        if not cleaned:
            raise ValueError("domain must contain a hostname")
        return cleaned


class UpdateProspectRequest(BaseModel):
    """Partial update — every field optional; ``None`` means "leave as-is".

    ``domain`` and ``source`` are deliberately NOT updatable: the domain is
    the dedupe identity (changing it is a delete+create, not an edit) and the
    source records provenance at capture time.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    company: str | None = Field(default=None, min_length=1, max_length=200)
    tier: ProspectTier | None = None
    research_brief: str | None = None
    emails: list[str] | None = None
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool | None = None
    status: ProspectStatus | None = None


class ProspectResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    company: str
    domain: str
    source: str
    tier: str
    research_brief: str
    emails: list[str]
    linkedin_url: str | None
    whatsapp_number: str | None
    opted_in: bool
    status: str
    created_at: str | None
    updated_at: str | None


__all__ = [
    "CreateProspectRequest",
    "ProspectResponse",
    "UpdateProspectRequest",
]
