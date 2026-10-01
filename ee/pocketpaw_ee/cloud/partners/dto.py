# ee/pocketpaw_ee/cloud/partners/dto.py — request/response shapes for Paw Partners.
# Created 2026-10-01 (feat/partners-foundation, PH-1). Requests and responses are
# separate classes (ee/cloud rule 4). Client wire shape unchanged by the Fabric
# move; ``whatsapp_opt_in_at`` is stored as an ISO string and parsed back here.
# Updated 2026-10-02: the profile write body moved to ``cloud/platform/partners.py``;
# GSTIN is upper-cased and pattern-validated.

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

E164 = r"^\+[1-9]\d{7,14}$"
GSTIN = r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"


def _upper(v: Any) -> Any:
    return v.strip().upper() if isinstance(v, str) else v


# Indian GST number: upper-cased first, then the 15-char structural pattern.
Gstin = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=GSTIN)]


class PartnerProfileOut(BaseModel):
    status: str
    tier: str
    footer_name: str
    billing_country: str
    founding: bool
    joined_at: datetime


class PartnerClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    whatsapp: str = Field(pattern=E164)
    whatsapp_opt_in_at: datetime | None = None
    gstin: Gstin | None = None
    notes: str = Field(default="", max_length=5000)


class PartnerClientUpdateRequest(BaseModel):
    """PATCH: only the fields sent are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    whatsapp: str | None = Field(default=None, pattern=E164)
    whatsapp_opt_in_at: datetime | None = None
    gstin: Gstin | None = None
    notes: str | None = Field(default=None, max_length=5000)


class PartnerClientOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    whatsapp: str
    whatsapp_opt_in_at: datetime | None
    gstin: str | None
    notes: str
    created_at: datetime | None
    updated_at: datetime | None
