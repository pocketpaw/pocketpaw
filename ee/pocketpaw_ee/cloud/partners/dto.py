# ee/pocketpaw_ee/cloud/partners/dto.py — request/response shapes for Paw Partners.
# Created 2026-10-01 (feat/partners-foundation, PH-1). Requests and responses are
# separate classes (ee/cloud rule 4). Client wire shape unchanged by the Fabric
# move; ``whatsapp_opt_in_at`` is stored as an ISO string and parsed back here.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

E164 = r"^\+[1-9]\d{7,14}$"


class PartnerProfileIn(BaseModel):
    """Admin write body. ``joined_at`` defaults to now when omitted."""

    status: Literal["applied", "active", "suspended"]
    tier: str = "bronze"
    footer_name: str = Field(min_length=1, max_length=120)
    billing_country: str = "IN"
    founding: bool = False
    joined_at: datetime | None = None

    @field_validator("billing_country")
    @classmethod
    def _iso2(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 2 or not v.isalpha():
            raise ValueError("billing_country must be an ISO-3166 alpha-2 code")
        return v


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
    gstin: str | None = Field(default=None, max_length=15)
    notes: str = Field(default="", max_length=5000)


class PartnerClientUpdateRequest(BaseModel):
    """PATCH: only the fields sent are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    whatsapp: str | None = Field(default=None, pattern=E164)
    whatsapp_opt_in_at: datetime | None = None
    gstin: str | None = Field(default=None, max_length=15)
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
