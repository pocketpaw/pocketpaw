# ee/pocketpaw_ee/cloud/leads/domain.py — frozen value object for a captured
# lead. Tenancy is enforced at construction (workspace_id required, no default)
# per the cloud 4-file rules. Pure Python, so the service is testable without
# Beanie; service.py owns the Beanie <-> domain conversion and flattens
# ``LeadSource`` onto this (submitter_ref, origin, kind, conversation_ref).
# ``origin`` / ``origin_unrecognized`` are surfaced because origin enforcement is
# opt-in: the owner must be able to see where an unexpected lead came from.

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
