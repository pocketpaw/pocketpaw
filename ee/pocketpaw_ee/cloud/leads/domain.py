# ee/pocketpaw_ee/cloud/leads/domain.py — frozen value objects for captured
# leads. Domain enforces tenancy at construction (workspace_id required, no
# default) per the cloud 4-file rules.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.3): new Lead domain
# value object. Pure Python so the leads service can be unit-tested without
# Beanie; service.py owns the Beanie ↔ domain conversion.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Lead:
    id: str
    workspace_id: str
    site_id: str
    form_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    submitter_ref: str = ""
    # Flattened off ``LeadSource``, the same way ``submitter_ref`` is. Surfaced
    # because ``Site.enforce_origin`` defaults OFF: a submission from an
    # unrecognized host is now accepted rather than 403'd, so the only thing that
    # keeps that trade honest is the owner being able to SEE where a lead came
    # from. A signal nobody can read is not a signal.
    origin: str = ""
    origin_unrecognized: bool = False
    created_at: datetime | None = None
