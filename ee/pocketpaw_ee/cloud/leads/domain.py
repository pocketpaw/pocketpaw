# ee/pocketpaw_ee/cloud/leads/domain.py — frozen value object for a captured
# lead. Tenancy is enforced at construction (workspace_id required, no default)
# per the cloud 4-file rules. Pure Python, so the service is testable without
# Beanie; service.py owns the Beanie <-> domain conversion and flattens
# ``LeadSource`` onto this (submitter_ref, origin, kind, conversation_ref).
# ``origin`` / ``origin_unrecognized`` are surfaced because origin enforcement is
# opt-in: the owner must be able to see where an unexpected lead came from.
# ``lead_site_id`` is the one rule for which site id a lead is filed under.

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
    origin: str = ""
    origin_unrecognized: bool = False
    # form | concierge | handoff | booking (models/lead.py LeadKind).
    source_kind: str = "form"
    conversation_ref: str = ""
    # new | contacted | won | lost | booked; read_at None means unread.
    status: str = "new"
    read_at: datetime | None = None
    created_at: datetime | None = None


def lead_site_id(site: Any) -> str:
    """The id a site's leads are filed under, and the id its public capture
    routes accept: the ``script_name`` for a built site, the object id for a
    connected (``foreign_origin``) site, which never gets a script name. ""
    for anything else (a draft), which has no capture endpoint."""
    script = str(getattr(site, "script_name", "") or "")
    if script:
        return script
    if getattr(site, "foreign_origin", False) and getattr(site, "id", None) is not None:
        return str(site.id)
    return ""
