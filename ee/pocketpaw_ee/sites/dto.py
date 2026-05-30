# ee/pocketpaw_ee/sites/dto.py — request/response DTOs for the Sites control
# plane. Distinct request and response shapes per the cloud 4-file rules.
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).

from __future__ import annotations

from pydantic import BaseModel


class PublishRequest(BaseModel):
    pocket_id: str


class SiteResponse(BaseModel):
    id: str
    pocket_id: str
    name: str
    script_name: str
    deployed: bool
    signed_key: str


class DomainRequest(BaseModel):
    hostname: str


class DomainStatusResponse(BaseModel):
    hostname: str
    cname_target: str
    status: str  # pending | verifying | live | error
