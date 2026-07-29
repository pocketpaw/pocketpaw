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
# Updated 2026-07-27 (feat/growth-g5): ``MessageLog`` — the audit value object
# for ONE outbound delivery attempt (``MESSAGE_LOG_OUTCOMES``: sent | failed).
# One row per attempt, not per draft, so a retried send keeps both records.
# Updated 2026-07-27 (integration/growth-v1): the outcome vocabulary absorbs
# G-6's WhatsApp send record — ``sending`` (written before the provider call)
# and ``blocked`` (a guard refused; no provider call happened) join
# ``MESSAGE_LOG_OUTCOMES``, and ``PROVIDER_REACHED_OUTCOMES`` names the subset
# the WhatsApp rate cap counts.
# Updated 2026-07-28 (feat/growth-api-scale): the prospect list's scale
# vocabulary — ``ProspectSort`` (the four ordering modes the UI offers),
# ``TIER_SORT_ORDER``, the DECLARED qualification rank a→b→c→unqualified, and
# ``PROSPECT_STATUS_ORDER`` / ``PROSPECT_SOURCE_ORDER``, the facet display
# orders derived from the Literals. The rank is data here rather than a
# lexicographic accident in the query layer, so renaming a tier can't silently
# reorder the list.
# Updated 2026-07-28 (feat/growth-projects): ``Prospect.project_id`` — the
# client a prospect belongs to, following the ``tasks`` / ``cycles`` consumer
# pattern exactly (nullable, validated against the workspace at entry, an
# optional filter on the reads). An agency runs one outbound pipeline per
# client; the project primitive already models that container, so growth
# consumes it rather than inventing a second scoping concept.
# Updated 2026-07-29 (feat/growth-discovery): the ICP — a STANDING description
# of who a workspace wants, plus a cadence. ``Icp`` (the value object),
# ``IcpCadence`` / ``IcpStatus`` and their orders, and ``ProspectSource`` gains
# ``discovery``. Plus the provenance vocabulary the discovery engine writes and
# the UI reads: ``EmailEvidence`` (an address, how we know it, and WHERE it was
# seen) and ``EMAIL_CONFIDENCE``. The email fields are the load-bearing part —
# see ``EmailEvidence`` for why a guessed address is worse than an empty one.
# Updated 2026-07-27 (feat/growth-g4): the Instinct send gate —
# ``GATE_OWNED_TARGETS`` marks the statuses only the gate machinery may set
# (``approved`` via an approved ``_growth_send`` proposal, ``sent`` via the
# dispatch worker), and ``GROWTH_DISPATCH_JOB_NAME`` names the arq job the
# approve path enqueues on the ``growth`` queue.

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

# Dedicated arq queue for growth jobs. Unlike workspace jobs (which ride arq's
# default queue on the shared chat-runs worker — see ``jobs/domain.py``), growth
# gets its OWN queue + worker seam (``growth/worker.py``) so a burst of outbound
# work can never starve interactive chat runs. arq's enqueue selector for this
# is ``_queue_name`` (underscore-prefixed; a bare ``queue=`` kwarg would be
# forwarded to the job function and crash it — see the jobs/domain.py history).
GROWTH_QUEUE_NAME = "growth"

# Where the prospect came from. ``discovery`` is the ICP research engine — a
# row nobody typed and nobody imported, found by a standing description of who
# the workspace wants. It is its own source (not folded into ``directory``)
# because provenance is the whole question a human asks about an auto-found
# row: "who put this here, and can I see where it came from".
ProspectSource = Literal["clay", "directory", "discovery", "manual"]

# Qualification tier. ``unqualified`` until research triages it.
ProspectTier = Literal["a", "b", "c", "unqualified"]

# Outbound lifecycle. Later slices move prospects along this chain.
ProspectStatus = Literal["new", "qualified", "drafted", "in_sequence", "replied", "dead"]

# G-10a — the prospect list's ordering modes. ``newest`` is the default and is
# byte-for-byte the pre-G-10a behaviour.
ProspectSort = Literal["newest", "oldest", "company", "tier"]

# Qualification rank, best first. This is DECLARED, not derived: a lexicographic
# sort over the current tier names happens to produce the same order, which is
# luck — rename ``unqualified`` to ``untriaged`` or add a ``d`` tier and the
# accident breaks silently. The list query walks these buckets in this order, so
# the ordering survives any rename of the values above.
TIER_SORT_ORDER: tuple[str, ...] = ("a", "b", "c", "unqualified")

# Display order for the facet counts. Derived from the Literals above rather
# than re-typed, so a new status or source can never go missing from the chip
# row. ``TIER_SORT_ORDER`` stays hand-written because it is a RANK, which is a
# different claim than "the set of legal values".
PROSPECT_STATUS_ORDER: tuple[str, ...] = get_args(ProspectStatus)
PROSPECT_SOURCE_ORDER: tuple[str, ...] = get_args(ProspectSource)


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
    # The client this prospect belongs to, when the workspace uses projects.
    # Nullable throughout: a solo operator never sees a project and nothing
    # about their pipeline changes. Validated against the workspace at entry —
    # a project from another tenant is a hard error, not a silent null.
    project_id: str | None = None
    tier: str = "unqualified"  # ProspectTier
    research_brief: str = ""
    emails: tuple[str, ...] = ()
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool = False
    status: str = "new"  # ProspectStatus
    # The standing ICP that found this row, when discovery did. Nullable
    # everywhere: a typed or imported prospect has no ICP, and that is a fact
    # rather than a gap.
    icp_id: str | None = None
    # WHERE the row came from — the pages the research actually read. This is
    # the audit trail for an auto-found prospect: a human reviewing a row
    # nobody typed needs to open the source and check the claim. Empty on a
    # manually created prospect, which needs no such trail.
    source_urls: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# The ICP — a standing description of who a workspace wants (feat/growth-
# discovery)
# ---------------------------------------------------------------------------

# How often the discovery cron runs an ICP. ``off`` is the DEFAULT and means
# "this ICP only runs when a human asks" (the preview route, or a manual run).
# A standing description of who you want is a useful artifact on its own; it
# becomes a recurring spend the moment a cadence is switched on, so the switch
# is an explicit act rather than the shape a new ICP arrives in.
IcpCadence = Literal["off", "daily", "weekly"]

# Whether the ICP is live. ``paused`` keeps the definition (and its history)
# while stopping the cron — deleting is for an ICP that was a mistake, pausing
# is for one that did its job.
IcpStatus = Literal["active", "paused"]

ICP_CADENCE_ORDER: tuple[str, ...] = get_args(IcpCadence)
ICP_STATUS_ORDER: tuple[str, ...] = get_args(IcpStatus)

# Cadences the cron actually schedules. ``off`` is absent by construction, so
# the sweep's due-check can never accidentally include it.
SCHEDULED_CADENCES: frozenset[str] = frozenset({"daily", "weekly"})

# The weekday a ``weekly`` ICP runs on (Monday, ``date.weekday() == 0``).
# FIXED rather than per-ICP: "weekly" has to mean a predictable day or an
# operator cannot tell a skipped run from a run that was never due. Monday puts
# the week's new prospects in the list before the week's outreach is decided.
WEEKLY_DISCOVERY_WEEKDAY = 0

# Default ceiling on how many prospects ONE run of an ICP may file. Small on
# purpose: the first thing an operator does with a new ICP is read the rows and
# decide whether the criteria describe who they actually want, and a run that
# files 200 rows makes that impossible.
DEFAULT_ICP_MAX_PER_RUN = 10

# Hard upper bound on ``max_per_run``, enforced at the DTO boundary. A run is
# an LLM research pass, not a database scan: past this the right tool is a
# second ICP with narrower criteria, not a bigger batch.
MAX_ICP_MAX_PER_RUN = 100

# The arq job the discovery cron registers under on ``GROWTH_QUEUE_NAME``.
# Explicit + dotted like the other two, so the wire name survives a rename of
# the Python function.
GROWTH_DISCOVERY_SWEEP_JOB_NAME = "growth.discovery_sweep"


@dataclass(frozen=True)
class Icp:
    """An Ideal Customer Profile — a STANDING description of who a workspace
    wants, plus how often to go looking.

    ``criteria`` is deliberately free text, not a filter tree: it is what the
    research READS, and the thing that makes a good ICP good ("dental practices
    with 2-6 chairs that still book by phone") does not decompose into enum
    columns without losing exactly the part that was useful. ``geography`` and
    ``exclusions`` are separated out of it only because both are answers the
    research must apply as HARD constraints rather than weigh — a company
    outside the geography or on the exclusion list is not a weaker match, it is
    not a match.
    """

    id: str
    workspace_id: str
    name: str
    criteria: str
    project_id: str | None = None
    geography: str = ""
    exclusions: str = ""
    cadence: str = "off"  # IcpCadence
    max_per_run: int = DEFAULT_ICP_MAX_PER_RUN
    status: str = "active"  # IcpStatus
    last_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Email provenance — the one hard constraint of the discovery engine
# ---------------------------------------------------------------------------

# HOW we know an address. This is the load-bearing vocabulary of the whole
# discovery slice, so it is worth being explicit about why it exists.
#
# Clay and Apollo return an email because they ran a VERIFICATION WATERFALL:
# several independent providers, an SMTP or catch-all probe, a confidence
# score. An LLM has none of that. What it CAN do — and will, fluently, unless
# the type system stops it — is produce ``firstname@company.com`` because that
# is what such an address looks like. That guess is not a cheaper email; it is
# a different and much worse object. It bounces, the bounce lands on the
# SENDING domain's reputation (see ``GROWTH_SENDING_DOMAIN`` in CLAUDE.md), and
# it poisons the list with rows that look contactable and are not.
#
# So the engine records an address ONLY when it was actually observed as text
# on a page it read, together with the URL where it was seen. Everything else
# stays empty, and an empty ``emails`` is a perfectly good prospect — a domain
# with a research brief is exactly what a human or a real verification provider
# picks up from.
#
#   ``observed`` — the address appeared, as text, on the page at ``seen_at_url``.
#   ``claimed``  — something asserted it (an aggregator, the model's own
#                  memory) without us reading a page carrying it.
#   ``guessed``  — constructed from a pattern. The default, because a piece of
#                  evidence that does not say how it was obtained must be
#                  treated as the least trustworthy kind, not the most.
EmailConfidence = Literal["observed", "claimed", "guessed"]

EMAIL_CONFIDENCE: tuple[str, ...] = get_args(EmailConfidence)

# The ONLY confidence that may become a stored address. A frozenset rather than
# a bare ``== "observed"`` comparison so the rule has a name and one place to
# change if a real verification provider is ever wired in (that provider would
# add its own value here — an LLM never will).
RECORDABLE_EMAIL_CONFIDENCE: frozenset[str] = frozenset({"observed"})


@dataclass(frozen=True)
class EmailEvidence:
    """One candidate address, how we know it, and WHERE it was seen.

    The research layer returns these instead of bare strings, so "we found an
    email" and "we can prove where it was" are the same statement. An evidence
    object that cannot answer the second question cannot produce an address —
    see ``recordable_emails``.
    """

    address: str
    # Defaults to the LEAST trustworthy value on purpose: evidence that forgot
    # to say how it was obtained is a guess until it proves otherwise. A
    # research implementation that omits the field gets fail-closed behaviour
    # rather than a silent promotion to "observed".
    confidence: str = "guessed"  # EmailConfidence
    # The page the address was read from. Required in practice — an
    # ``observed`` claim with no URL is unfalsifiable, so it is not recordable.
    seen_at_url: str = ""

    @property
    def is_recordable(self) -> bool:
        """True only for an address we can point at on a page we read."""
        return bool(
            self.address.strip()
            and self.confidence in RECORDABLE_EMAIL_CONFIDENCE
            and self.seen_at_url.strip()
        )


def recordable_emails(evidence: Iterable[EmailEvidence]) -> tuple[str, ...]:
    """The addresses from ``evidence`` that may actually be stored.

    THE door — nothing else in the discovery path turns evidence into an
    address, so "never invent an email" is enforced in one auditable function
    rather than asked of a prompt. Anything guessed, claimed, or missing its
    source URL is dropped silently: the caller gets a prospect with no email,
    which is the honest result. Order is preserved and duplicates collapse (the
    same address on two pages is one address).
    """
    seen: dict[str, None] = {}
    for item in evidence:
        if item.is_recordable:
            seen.setdefault(item.address.strip().lower(), None)
    return tuple(seen)


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


# G-5 — outcome of one outbound delivery ATTEMPT. ``sent`` means the provider
# accepted the message; ``failed`` means it did not, and the draft deliberately
# stays ``approved`` so the attempt is retryable without a second approval.
#
# Integration (growth-v1) — ``sending`` and ``blocked`` come from G-6's
# unified-in WhatsApp path. ``sending`` is the row written BEFORE the provider
# call, so an attempt that crashes mid-flight still leaves a trace and the
# rate-cap window counts committed attempts rather than only completed ones.
# ``blocked`` is a guard refusal: NO provider call was made, and the row exists
# precisely to prove that (see ``blocked_reason``).
MessageOutcome = Literal["sending", "sent", "failed", "blocked"]

MESSAGE_LOG_OUTCOMES: frozenset[str] = frozenset({"sending", "sent", "failed", "blocked"})

# Outcomes that mean the attempt REACHED the provider. The WhatsApp rate cap
# counts these and excludes ``blocked`` on purpose: a refused attempt never
# touched Meta, so it must not consume the quality-rating budget the cap
# protects.
PROVIDER_REACHED_OUTCOMES: frozenset[str] = frozenset({"sending", "sent", "failed"})


@dataclass(frozen=True)
class MessageLog:
    """One outbound delivery attempt for a draft — the audit record.

    Tenancy is required at construction (``workspace_id``, no default), like
    every other growth value object. Carries the PROVIDER's identity and
    message id, never its credential; ``error`` is a sanitised, human-readable
    reason produced by the channel connector.
    """

    id: str
    workspace_id: str
    draft_id: str
    prospect_id: str
    channel: str  # DraftChannel
    provider: str  # e.g. "mailtrap" (email) or "msg91" (whatsapp)
    to_address: str
    outcome: str  # MessageOutcome
    provider_message_id: str | None = None
    sent_at: datetime | None = None
    error: str | None = None
    created_at: datetime | None = None
    # Why a ``blocked`` attempt was refused; empty on every other outcome.
    blocked_reason: str = ""
    error_code: str = ""
    # The opt-in fact as of the attempt — the WhatsApp compliance claim.
    opted_in_at_attempt: bool = False


__all__ = [
    "DEFAULT_ICP_MAX_PER_RUN",
    "DRAFT_TRANSITIONS",
    "EMAIL_CONFIDENCE",
    "GATE_OWNED_TARGETS",
    "GROWTH_DISCOVERY_SWEEP_JOB_NAME",
    "GROWTH_DISPATCH_JOB_NAME",
    "GROWTH_QUEUE_NAME",
    "ICP_CADENCE_ORDER",
    "ICP_STATUS_ORDER",
    "MAX_ICP_MAX_PER_RUN",
    "MESSAGE_LOG_OUTCOMES",
    "PROSPECT_SOURCE_ORDER",
    "PROSPECT_STATUS_ORDER",
    "PROVIDER_REACHED_OUTCOMES",
    "RECORDABLE_EMAIL_CONFIDENCE",
    "SCHEDULED_CADENCES",
    "TIER_SORT_ORDER",
    "WEEKLY_DISCOVERY_WEEKDAY",
    "Draft",
    "DraftChannel",
    "DraftStatus",
    "DraftVariant",
    "EmailConfidence",
    "EmailEvidence",
    "Icp",
    "IcpCadence",
    "IcpStatus",
    "MessageLog",
    "MessageOutcome",
    "Prospect",
    "ProspectSort",
    "ProspectSource",
    "ProspectStatus",
    "ProspectTier",
    "recordable_emails",
]
