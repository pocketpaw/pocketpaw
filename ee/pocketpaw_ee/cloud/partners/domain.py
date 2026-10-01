# ee/pocketpaw_ee/cloud/partners/domain.py — value objects for Paw Partners.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1). Updated the same day: a
# client is now a Fabric ``Customer`` object (journal-backed, workspace-scoped),
# not a Beanie document, so ``PartnerClient`` mirrors that object's property bag
# the way ``people.domain.Person`` mirrors a Fabric ``Person``.
#
# Type id: bare ``"customer"``. It cannot collide with a workspace-authored
# "Customer" type — those are ``ObjectType`` rows whose ids are minted
# (``ot_<hex>``, fabric/models.py) and scoped per workspace (SZD-2). Another
# journal writer COULD use the literal ``"customer"`` someday, so reads also
# filter on ``source_connector == SOURCE_PAW_PARTNERS``; a foreign "customer"
# object is never listed, edited or archived through the partner routes.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

CUSTOMER_TYPE_ID = "customer"
CUSTOMER_TYPE_NAME = "Customer"
SOURCE_PAW_PARTNERS = "paw-partners"


@dataclass(frozen=True)
class PartnerClient:
    id: str
    workspace_id: str
    name: str
    whatsapp: str
    whatsapp_opt_in_at: str | None  # ISO-8601
    gstin: str | None
    notes: str
    created_at: datetime | None
    updated_at: datetime | None
    source: str = SOURCE_PAW_PARTNERS

    def to_properties(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "whatsapp": self.whatsapp,
            "whatsapp_opt_in_at": self.whatsapp_opt_in_at,
            "gstin": self.gstin,
            "notes": self.notes,
            "source": self.source,
        }
