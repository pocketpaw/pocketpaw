# ee/pocketpaw_ee/cloud/growth/dto.py — request/response DTOs for the prospect
# entity. Distinct Request and Response shapes per the ee/cloud rule — never
# reuse a model for input and output. ``domain`` (the dedupe key) is normalised
# to a bare lowercase hostname at the DTO boundary so every caller — router,
# upsert, later ingestion slices — dedupes on the same canonical form.
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth — the prospect
# store. Domain → DTO mapping lives in ``service.py`` as private helpers.
# Updated 2026-07-27 (feat/growth-g2): bulk-ingestion DTOs — BulkIngestRequest
# (raw rows, 500-row cap enforced at the DTO boundary so an oversized payload
# 422s before any row is touched), BulkRowError, BulkIngestResponse. Rows are
# deliberately ``dict`` (not CreateProspectRequest) so one invalid row becomes
# a per-row error entry in the response instead of failing the whole payload.
# Updated 2026-07-27 (feat/growth-g3): draft DTOs — CreateDraftRequest (subject
# is email-only, enforced here at the boundary; body non-empty),
# TransitionDraftRequest (the target status; legality is the SERVICE's job —
# the DTO only checks it's a known status), DraftResponse.
# Updated 2026-07-27 (feat/growth-g4): ProposeSendResponse — the Instinct
# proposal id + the flipped draft returned by POST /growth/drafts/{id}/propose.
# Updated 2026-07-28 (feat/growth-api-scale): ProspectPageResponse — the
# cursor-paginated envelope GET /growth/prospects now returns
# ({items, next_cursor, total}) in place of the bare array. Breaking on
# purpose: "n of m" needs a filter-scoped total and reaching row 3,000 needs a
# resume key, and neither fits in a naked list. ProspectFacetsResponse — the
# per-tier / per-status / per-source counts behind the filter chips.
# ProposeBatchRequest / ProposeBatchError / ProposeBatchResponse — proposing a
# selection of drafts in one call, with per-draft error entries in the
# bulk-ingest style (100-id cap enforced at the boundary).
# Updated 2026-07-28 (feat/growth-mcp): UpdateDraftRequest — a partial edit of a
# draft's COPY (subject / body / demo_url), the shape the agent surface needs to
# revise a draft it wrote. No ``status`` field, deliberately: the lifecycle moves
# through the transition route and the Instinct gate, never through an edit.
# Updated 2026-07-27 (feat/growth-g8): LinkedInQueueItemResponse — one row of
# the manual LinkedIn send queue: the draft envelope joined with the prospect
# context the captain needs to send by hand (name, company, profile URL,
# research brief, tier). Response-only; the queue has no request DTO.
# Updated 2026-07-28 (feat/growth-projects): a prospect may be JUST A DOMAIN.
# ``name`` and ``company`` on CreateProspectRequest lost their ``min_length=1``
# and now default to ``""`` — "not yet known", the shape a pasted domain list
# actually arrives in (bulk rows validate through this same model, so they
# relax with it). ``domain`` stays required and still normalises: it is the
# dedupe identity, and a row without one is not a prospect. The LinkedIn queue
# row gained ``prospect_domain`` so an export can still title a section for a
# prospect whose name nobody has filled in yet.
# Updated 2026-07-29 (feat/growth-discovery): the ICP shapes — CreateIcpRequest
# (name + criteria required, everything else optional; ``cadence`` defaults to
# ``off`` and ``max_per_run`` is capped at the boundary), UpdateIcpRequest
# (partial, ``project_id`` three-valued like the prospect update), IcpResponse.
# Plus ``icp_id`` / ``source_urls`` on the prospect create + response: the
# provenance of a row nobody typed. The DTO cannot enforce the observed-email
# rule — by the time an address reaches ``emails`` it is a plain string — so
# that filter lives one layer up, in ``domain.recordable_emails``, which the
# discovery run is the only caller of.
# Updated 2026-07-28 (feat/growth-projects): ``project_id`` on the create,
# update and response shapes — the client a prospect belongs to. On the UPDATE
# it is three-valued the way ``tasks`` does it (None = leave alone, an id =
# reassign, "" = clear); on the CREATE it is a plain optional. Neither can
# validate the id itself: only the service knows the caller's workspace.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from pocketpaw_ee.cloud.growth.domain import (
    DEFAULT_ICP_MAX_PER_RUN,
    MAX_ICP_MAX_PER_RUN,
    DraftChannel,
    DraftStatus,
    DraftVariant,
    IcpCadence,
    IcpStatus,
    ProspectSource,
    ProspectStatus,
    ProspectTier,
    normalise_domain,
)

# The domain canonicaliser moved to ``domain.py`` in T-7 (it canonicalises the
# DEDUPE KEY, which the key logic there needs too, and since T-7 the key can be
# a whole email address — see ``normalise_domain`` on why an email passes
# through the URL surgery untouched). Re-exported under the old private name so
# existing importers keep resolving.
_normalise_domain = normalise_domain


class CreateProspectRequest(BaseModel):
    """A prospect at capture time. Only ``domain`` is required.

    ``name`` and ``company`` default to ``""`` meaning NOT YET KNOWN, because
    that is the honest shape an import arrives in: a list of bare domains
    pasted out of a directory, with the rest filled in later by research. An
    empty string here is a fact ("we haven't looked yet"), never a placeholder
    — nothing downstream may render it as the literal word "unknown".

    The AGENT surface is stricter on purpose: ``growth_upsert_prospect``
    refuses to CREATE a row without a name and a company, because an agent
    that researched a company and still can't name it has failed at the job.
    A human pasting a domain list is a different caller with a different
    truth, and that guard lives in the MCP handler rather than here.
    """

    name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    domain: str = Field(min_length=1, max_length=253)
    source: ProspectSource
    # The client this prospect belongs to. The SERVICE validates it against
    # the caller's workspace (a project from another tenant is a 404) — the
    # DTO can only check the shape, since it has no tenancy context.
    project_id: str | None = None
    tier: ProspectTier = "unqualified"
    research_brief: str = ""
    emails: list[str] = Field(default_factory=list)
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool = False
    status: ProspectStatus = "new"
    # Discovery provenance. Both are written by the discovery run and left at
    # their defaults by every other caller. ``emails`` reaching this model has
    # ALREADY passed the observed-only filter (``domain.recordable_emails``) —
    # the DTO cannot re-check it, because by here an address is just a string.
    icp_id: str | None = None
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    # Inbound provenance (T-7): the site-form submission this row came from.
    # Written only by the leads bridge; every other caller leaves it None.
    lead_id: str | None = None

    @field_validator("domain")
    @classmethod
    def _clean_domain(cls, v: str) -> str:
        cleaned = _normalise_domain(v)
        if not cleaned:
            raise ValueError("domain must contain a hostname")
        return cleaned


class UpdateProspectRequest(BaseModel):
    """Partial update — every field optional; ``None`` means "leave as-is".

    ``domain`` and ``source`` are deliberately NOT updatable: the domain is
    the dedupe identity (changing it is a delete+create, not an edit) and the
    source records provenance at capture time.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    company: str | None = Field(default=None, min_length=1, max_length=200)
    # Three-valued, mirroring ``tasks``: ``None`` leaves the assignment alone,
    # an id reassigns (validated against the workspace), and ``""`` clears it.
    # Without the empty-string case there would be no way to un-assign a
    # prospect from a client once assigned.
    project_id: str | None = None
    tier: ProspectTier | None = None
    research_brief: str | None = None
    emails: list[str] | None = None
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool | None = None
    status: ProspectStatus | None = None


class BulkIngestRequest(BaseModel):
    """Bulk-ingestion payload: up to 500 CreateProspectRequest-shaped rows.

    Rows stay ``dict`` on purpose — the service validates each row
    individually so a bad row records an error entry while the rest proceed
    (no all-or-nothing abort). The 500-row cap IS enforced here: an oversized
    payload is a 422 before any row is processed.
    """

    rows: list[dict[str, Any]] = Field(max_length=500)


class BulkRowError(BaseModel):
    """One failed row in a bulk ingest: its position and why it was skipped."""

    index: int
    code: str
    message: str


class BulkIngestResponse(BaseModel):
    created: int
    updated: int
    errors: list[BulkRowError]


class ProspectResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    company: str
    domain: str
    source: str
    project_id: str | None = None
    tier: str
    research_brief: str
    emails: list[str]
    linkedin_url: str | None
    whatsapp_number: str | None
    opted_in: bool
    status: str
    icp_id: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    # T-7 — the submission that created this row, so /growth can link a
    # prospect back to what the visitor actually typed.
    lead_id: str | None = None
    created_at: str | None
    updated_at: str | None


class ProspectPageResponse(BaseModel):
    """One cursor-paginated page of prospects (G-10a).

    Replaces the bare array the list route used to return — a BREAKING change,
    made deliberately: without ``total`` the UI cannot say "n of m", and
    without ``next_cursor`` it cannot reach row 3,000. ``total`` counts every
    row matching the CURRENT filters (not the page), so it is stable while
    paging and is what the count reads from.
    """

    items: list[ProspectResponse]
    next_cursor: str | None = None
    total: int


class ProspectFacetsResponse(BaseModel):
    """Counts per tier / status / source for the filter chips (G-10a).

    Each block is ``{value: count}`` covering EVERY legal value, zeros
    included — the chip row keeps a stable shape as the user filters instead
    of chips appearing and vanishing. Each block respects the OTHER active
    filters but not its own, so the tier counts stay meaningful while a status
    filter is on (that is the whole point of a facet).
    """

    tier: dict[str, int]
    status: dict[str, int]
    source: dict[str, int]


# ---------------------------------------------------------------------------
# ICPs (feat/growth-discovery)
# ---------------------------------------------------------------------------


class CreateIcpRequest(BaseModel):
    """A standing description of who the workspace wants.

    ``name`` and ``criteria`` are the only required fields — an ICP with no
    criteria describes nobody, and the research would have nothing to read.
    ``cadence`` defaults to ``off``: writing down who you want is free, going
    looking for them on a schedule is a recurring spend, so the schedule is an
    explicit act rather than the shape a new ICP arrives in.
    """

    name: str = Field(min_length=1, max_length=200)
    criteria: str = Field(min_length=1, max_length=4000)
    # The client this ICP prospects for. The SERVICE validates it against the
    # caller's workspace (a project from another tenant is a 404) — the DTO has
    # no tenancy context, exactly like ``CreateProspectRequest``.
    project_id: str | None = None
    geography: str = Field(default="", max_length=500)
    exclusions: str = Field(default="", max_length=2000)
    cadence: IcpCadence = "off"
    max_per_run: int = Field(default=DEFAULT_ICP_MAX_PER_RUN, ge=1, le=MAX_ICP_MAX_PER_RUN)
    status: IcpStatus = "active"

    @field_validator("name", "criteria")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class UpdateIcpRequest(BaseModel):
    """Partial update — every field optional, ``None`` means "leave as-is".

    Editing ``criteria`` deliberately does NOT re-run anything: the next
    scheduled tick (or a preview) picks the new text up. An edit that
    immediately spent an LLM budget would make tuning an ICP expensive, and
    tuning is the main thing anyone does with one.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    criteria: str | None = Field(default=None, min_length=1, max_length=4000)
    # Three-valued like ``UpdateProspectRequest``: ``None`` leaves the
    # assignment alone, an id reassigns, ``""`` clears it.
    project_id: str | None = None
    geography: str | None = Field(default=None, max_length=500)
    exclusions: str | None = Field(default=None, max_length=2000)
    cadence: IcpCadence | None = None
    max_per_run: int | None = Field(default=None, ge=1, le=MAX_ICP_MAX_PER_RUN)
    status: IcpStatus | None = None

    @field_validator("name", "criteria")
    @classmethod
    def _non_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("must not be blank")
        return v


class IcpResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    criteria: str
    project_id: str | None
    geography: str
    exclusions: str
    cadence: str
    max_per_run: int
    status: str
    last_run_at: str | None
    created_at: str | None
    updated_at: str | None


class PreviewedProspectResponse(BaseModel):
    """One company as a run WOULD file it.

    ``emails`` has already been through the observed-only filter, so this is
    exactly what would be stored — never the raw evidence, which would
    advertise addresses the engine refuses to keep.
    """

    domain: str
    name: str
    company: str
    research_brief: str
    source_urls: list[str]
    emails: list[str]
    # A run would SKIP this one — the workspace already has it. Shown rather
    # than filtered out, because a preview full of these is the useful signal
    # that the criteria describe people you already know.
    already_known: bool


class IcpPreviewResponse(BaseModel):
    """The result of a dry run: what would land, and why it might be short.

    ``error`` is populated when the research itself failed — a preview that
    returned nothing because the search provider was down must not be
    indistinguishable from an ICP that describes nobody.
    """

    icp_id: str
    items: list[PreviewedProspectResponse]
    notes: str = ""
    error: str = ""


class CreateDraftRequest(BaseModel):
    """One channel's outreach copy for a prospect.

    A draft is always born in ``status="draft"`` — there is no status field
    here; lifecycle moves happen only through the transition route.
    """

    channel: DraftChannel
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)
    variant: DraftVariant = "first_touch"
    demo_url: str | None = Field(default=None, max_length=2048)

    @field_validator("body")
    @classmethod
    def _non_blank_body(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v

    @model_validator(mode="after")
    def _subject_is_email_only(self) -> CreateDraftRequest:
        if self.channel != "email" and self.subject is not None:
            raise ValueError("subject is only valid on the email channel")
        return self


class UpdateDraftRequest(BaseModel):
    """Partial edit of a draft's COPY — every field optional, ``None`` means
    "leave as-is".

    There is no ``status`` field and there never will be: a status move is a
    lifecycle event that goes through the transition route (and, for the
    gate-owned targets, through an approved Instinct proposal). Letting an edit
    carry a status would be a second, unreviewed road to ``approved``.

    ``channel`` and ``prospect_id`` are likewise not editable — a draft for a
    different channel or a different prospect is a different draft.
    ``subject`` stays email-only, but that check needs the stored draft's
    channel, so it lives in the service rather than here.
    """

    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=10_000)
    demo_url: str | None = Field(default=None, max_length=2048)

    @field_validator("body")
    @classmethod
    def _non_blank_body(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("body must not be blank")
        return v

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateDraftRequest:
        if self.subject is None and self.body is None and self.demo_url is None:
            raise ValueError("provide at least one of subject / body / demo_url")
        return self


class TransitionDraftRequest(BaseModel):
    """Target status for a lifecycle move. Whether the move is LEGAL from the
    draft's current status is the service's call (``draft.illegal_transition``,
    422) — this DTO only rejects unknown status names."""

    status: DraftStatus


class DraftResponse(BaseModel):
    id: str
    workspace_id: str
    prospect_id: str
    channel: str
    subject: str | None
    body: str
    variant: str
    status: str
    demo_url: str | None
    created_at: str | None
    updated_at: str | None


class ProposeSendResponse(BaseModel):
    """Result of proposing a draft for sending (G-4): the Instinct proposal id
    a human approves/rejects in the Tray, plus the draft (now ``proposed``)."""

    proposal_id: str
    draft: DraftResponse


class ProposeBatchRequest(BaseModel):
    """Draft ids to propose in one call — up to 100 (G-10a).

    The cap IS enforced here, so an oversized payload 422s before a single
    proposal is filed. 100 rather than bulk-ingest's 500 because each id costs
    an Instinct proposal a human then has to triage in the Tray: a batch that
    outruns the reviewer defeats the gate.
    """

    draft_ids: list[str] = Field(max_length=100)


class ProposeBatchError(BaseModel):
    """One draft that could not be proposed: where it was and why.

    Same shape as ``BulkRowError`` plus the id, because the caller holds ids
    here rather than opaque rows and needs to know WHICH draft failed without
    counting back into its own array.
    """

    index: int
    draft_id: str
    code: str
    message: str


class ProposeBatchResponse(BaseModel):
    """How many drafts were proposed, and the per-draft failures."""

    proposed: int
    failed: list[ProposeBatchError]


class LinkedInQueueItemResponse(BaseModel):
    """One row of the manual LinkedIn send queue.

    The draft envelope plus the prospect context needed to send it by hand —
    the queue exists so the captain can copy-paste; there is no LinkedIn API
    integration by design (account-ban avoidance).
    """

    draft: DraftResponse
    prospect_name: str
    prospect_company: str
    # Always present, unlike name / company — the export titles a section with
    # it when nobody has filled the rest in yet.
    prospect_domain: str = ""
    linkedin_url: str | None
    research_brief: str
    tier: str


__all__ = [
    "BulkIngestRequest",
    "BulkIngestResponse",
    "BulkRowError",
    "CreateDraftRequest",
    "CreateIcpRequest",
    "CreateProspectRequest",
    "DraftResponse",
    "IcpPreviewResponse",
    "IcpResponse",
    "LinkedInQueueItemResponse",
    "PreviewedProspectResponse",
    "ProposeBatchError",
    "ProposeBatchRequest",
    "ProposeBatchResponse",
    "ProposeSendResponse",
    "ProspectFacetsResponse",
    "ProspectPageResponse",
    "ProspectResponse",
    "TransitionDraftRequest",
    "UpdateDraftRequest",
    "UpdateIcpRequest",
    "UpdateProspectRequest",
]
