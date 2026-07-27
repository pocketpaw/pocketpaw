# ee/pocketpaw_ee/cloud/growth/domain.py — frozen value objects + pure
# constants for the /growth outbound engine. Domain enforces tenancy at
# construction (``workspace_id`` required, no default) per the cloud 4-file
# rules. Pure Python — no Beanie / Pydantic / FastAPI imports — so the service
# can be unit-tested without the ODM and the import-linter contract can depend
# on it freely. Also home of ``GROWTH_QUEUE_NAME``, the dedicated arq queue the
# growth worker seam listens on (later slices enqueue ingestion / draft / send
# jobs there).
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth — the prospect
# store. Later slices add ingestion, drafts, and Instinct-gated sends.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# Dedicated arq queue for growth jobs. Unlike workspace jobs (which ride arq's
# default queue on the shared chat-runs worker — see ``jobs/domain.py``), growth
# gets its OWN queue + worker seam (``growth/worker.py``) so a burst of outbound
# work can never starve interactive chat runs. arq's enqueue selector for this
# is ``_queue_name`` (underscore-prefixed; a bare ``queue=`` kwarg would be
# forwarded to the job function and crash it — see the jobs/domain.py history).
GROWTH_QUEUE_NAME = "growth"

# Where the prospect came from.
ProspectSource = Literal["clay", "directory", "manual"]

# Qualification tier. ``unqualified`` until research triages it.
ProspectTier = Literal["a", "b", "c", "unqualified"]

# Outbound lifecycle. Later slices move prospects along this chain.
ProspectStatus = Literal["new", "qualified", "drafted", "in_sequence", "replied", "dead"]


@dataclass(frozen=True)
class Prospect:
    """Prospect value object — one company/contact target in a workspace.

    ``domain`` is the company website domain and the tenant-local dedupe key
    (the service lowercases it at entry; ``upsert_by_domain`` keys on it).
    """

    id: str
    workspace_id: str
    name: str
    company: str
    domain: str
    source: str  # ProspectSource — validated at the DTO boundary
    tier: str = "unqualified"  # ProspectTier
    research_brief: str = ""
    emails: tuple[str, ...] = ()
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool = False
    status: str = "new"  # ProspectStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "GROWTH_QUEUE_NAME",
    "Prospect",
    "ProspectSource",
    "ProspectStatus",
    "ProspectTier",
]
