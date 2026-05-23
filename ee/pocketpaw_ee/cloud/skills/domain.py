# domain.py — Domain value objects for the cloud Skills entity.
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — a frozen
# value object carrying the tenancy context + spec bytes for an
# API-doc skill install. Tenancy fields are required (no defaults) so
# constructing one without a workspace is a type error — Rule 3.
# Updated: 2026-05-23 — add ``ApiDocFromUrlInstall`` for the URL-fetch
# variant; same tenancy posture, ``url`` instead of ``spec_bytes``.
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiDocInstall(BaseModel):
    """An API-doc skill install request from a multipart upload.

    Frozen value object — multi-tenancy is enforced at construction:
    ``workspace_id`` and ``user_id`` are required, no defaults. The
    ``spec_bytes`` carry the uploaded OpenAPI / Swagger document; the
    service parses and installs it. ``name`` is the optional backend
    display name used to derive the skill slug when the spec itself
    names no server.
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    user_id: str
    filename: str
    spec_bytes: bytes
    name: str | None = None


class ApiDocFromUrlInstall(BaseModel):
    """An API-doc skill install request from a public spec URL.

    Same tenancy posture as :class:`ApiDocInstall` — ``workspace_id``
    and ``user_id`` are required. ``url`` is the spec URL the service
    will fetch *after* the SSRF guard runs against its resolved IPs;
    ``name`` is the optional display name used to derive the skill
    slug when the fetched spec names no server.
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    user_id: str
    url: str
    name: str | None = None


__all__ = ["ApiDocFromUrlInstall", "ApiDocInstall"]
