# ee/pocketpaw_ee/cloud/partners/domain.py — value objects and vocabulary for Paw Partners.
#
# ``PartnerClient`` mirrors a Fabric ``Customer`` object (journal-backed,
# workspace-scoped) the way ``people.domain.Person`` mirrors a Fabric ``Person``.
# Type id: bare ``"customer"``. It cannot collide with a workspace-authored
# "Customer" type (those are ``ObjectType`` rows with minted ``ot_<hex>`` ids,
# scoped per workspace). Another journal writer COULD use the literal
# ``"customer"`` someday, so reads also filter on
# ``source_connector == SOURCE_PAW_PARTNERS``; a foreign "customer" object is
# never listed, edited or archived through the partner routes.
#
# The public-profile vocabulary (slug pattern, reserved slugs, the closed set of
# services) lives here so ``models.workspace`` (storage) and ``partners.dto``
# (wire) validate against one definition; dto may not import the model.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

CUSTOMER_TYPE_ID = "customer"
CUSTOMER_TYPE_NAME = "Customer"
SOURCE_PAW_PARTNERS = "paw-partners"

# Public profile (PW-7).
PartnerService = Literal["print", "design", "web", "marketing", "photo"]
PARTNER_SLUG_PATTERN = r"^[a-z0-9-]{3,60}$"
# Fixed segments under /partners on the API and on the public site
# (/partners/find): a slug equal to one would be shadowed by, or shadow, a route.
PARTNER_SLUG_RESERVED = frozenset(
    {
        "me",
        "clients",
        "offers",
        "sell",
        "pay-link",
        "sites",
        "summary",
        "earnings",
        "rewards",
        "directory",
        "apply",
        "profile",
        "find",
    }
)


def validate_partner_slug(v: str) -> str:
    if v in PARTNER_SLUG_RESERVED:
        raise ValueError(f"'{v}' is reserved")
    return v


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
