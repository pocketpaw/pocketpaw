# ee/pocketpaw_ee/cloud/partners/domain.py — frozen value objects for Paw Partners.
# Created 2026-10-01 (feat/partners-foundation, PH-1). Tenancy (workspace_id) is
# required with no default, per ee/cloud rule 3.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PartnerClient(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    workspace_id: str
    name: str
    whatsapp: str
    whatsapp_opt_in_at: datetime | None
    gstin: str | None
    notes: str
    created_at: datetime | None
    updated_at: datetime | None
