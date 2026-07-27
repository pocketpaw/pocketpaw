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
# Updated 2026-07-27 (feat/growth-g3): ``Draft`` — per-channel outreach copy
# attached to a prospect, with the enforced status machine
# (``DRAFT_TRANSITIONS``): draft→proposed→approved→sent, sent→replied, any
# non-terminal→rejected. The transition table lives here (pure data) so the
# service stays a dumb enforcer and G-4 can wire Instinct proposals on top.
# Updated 2026-07-27 (feat/growth-g4): the Instinct send gate —
# ``GATE_OWNED_TARGETS`` marks the statuses only the gate machinery may set
# (``approved`` via an approved ``_growth_send`` proposal, ``sent`` via the
# dispatch worker), and ``GROWTH_DISPATCH_JOB_NAME`` names the arq job the
# approve path enqueues on the ``growth`` queue.

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


# Outreach channel a draft targets. ``subject`` only applies to email.
DraftChannel = Literal["email", "linkedin", "whatsapp"]

# Which touch in the sequence the copy is written for.
DraftVariant = Literal["first_touch", "follow_up"]

# Draft lifecycle. ``replied`` and ``rejected`` are terminal.
DraftStatus = Literal["draft", "proposed", "approved", "sent", "replied", "rejected"]

# The enforced status machine: draft→proposed→approved→sent (the happy chain),
# sent→replied (the prospect answered), and any NON-terminal status→rejected.
# ``replied`` / ``rejected`` are terminal — absent keys, so nothing leaves them.
# The service raises ``draft.illegal_transition`` (422) for any pair not here.
DRAFT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"proposed", "rejected"}),
    "proposed": frozenset({"approved", "rejected"}),
    "approved": frozenset({"sent", "rejected"}),
    "sent": frozenset({"replied", "rejected"}),
}

# G-4 — the Instinct send gate owns these edges. ``approved`` is only reachable
# through an approved ``_growth_send`` proposal (the instinct router's approve
# paths → ``growth.executor``), and ``sent`` only through the dispatch worker
# (G-5/G-6). The PUBLIC status route refuses these targets with a 403
# ``draft.gate_required`` even when the move is legal per ``DRAFT_TRANSITIONS``
# — structural, like /ship's destroy gate: NOTHING can send without an approved
# proposal. The gate machinery uses ``service.gate_transition`` (same legality
# table, no public-route restriction).
GATE_OWNED_TARGETS: frozenset[str] = frozenset({"approved", "sent"})

# The arq job the approve path enqueues on ``GROWTH_QUEUE_NAME``. Registered in
# ``growth/worker.py`` under this explicit name (``arq.worker.func``) so the
# dotted name survives refactors of the Python function.
GROWTH_DISPATCH_JOB_NAME = "growth.dispatch"


@dataclass(frozen=True)
class Draft:
    """Draft value object — one channel's outreach copy for a prospect.

    ``subject`` is email-only (``None`` on linkedin / whatsapp — the DTO
    boundary enforces it). ``body`` is the message copy and is never empty.
    """

    id: str
    workspace_id: str
    prospect_id: str
    channel: str  # DraftChannel — validated at the DTO boundary
    body: str
    subject: str | None = None  # email only
    variant: str = "first_touch"  # DraftVariant
    status: str = "draft"  # DraftStatus
    demo_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "DRAFT_TRANSITIONS",
    "GATE_OWNED_TARGETS",
    "GROWTH_DISPATCH_JOB_NAME",
    "GROWTH_QUEUE_NAME",
    "Draft",
    "DraftChannel",
    "DraftStatus",
    "DraftVariant",
    "Prospect",
    "ProspectSource",
    "ProspectStatus",
    "ProspectTier",
]
