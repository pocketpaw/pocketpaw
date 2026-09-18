# ee/pocketpaw_ee/cloud/entitlements/domain.py — the frozen, framework-free value
# objects the entitlements resolvers return (BC-6, the Entitlement primitive).
#
# Two scopes live here and they are deliberately separate classes, not one class
# with optional halves:
#
#   * ``Entitlements`` — what a WORKSPACE may do, derived from ``Workspace.plan``
#     plus the billing plan catalog, then overlaid with any platform-operator
#     override. Credit allotment, the monthly credit ceiling, the SMB resource
#     ceilings, the included-sites allowance, and the site-source capability.
#   * ``SiteEntitlements`` — what ONE site may do, derived from that site's own
#     ``plan_tier`` + ``subscription_status``. Sites are the only thing billed
#     per-object, so the workspace plan cannot answer it.
#
# They resolve from different sources on different cadences, and a caller that
# wants one almost never wants the other.
#
# INVARIANTS a reader must not break:
#   * No framework type may appear here — no Beanie, no FastAPI, no pydantic. The
#     service builds these and the DTO layer maps them without either reaching
#     into the other.
#   * Every ceiling FAILS CLOSED. A workspace with no/unknown plan resolves to the
#     Free values, never ``None``/uncapped; ``None`` means uncapped and is only
#     ever reached from a tier that genuinely is.
#   * A capability must never be granted by forgetting it. The one field carrying
#     a default (``Entitlements.site_source_visible``) defaults to WITHHELD;
#     everything else has no default, so an omission fails loudly.
#   * A field that would answer the same thing forever does not belong here — it
#     reads as implemented. See ``SiteEntitlements``'s note on the three it omits.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entitlements:
    """What a workspace is entitled to, resolved from its current plan.

    ``plan`` is the resolved tier key (matches ``Workspace.plan`` /
    ``PLAN_FEATURES`` — a workspace with no/unknown plan resolves to the base
    ``free`` tier). ``features`` is that tier's feature set (the same set
    ``PLAN_FEATURES`` and the policy gate use — one source of truth).
    ``monthly_credit_allotment`` is integer credits (1 credit == $0.01) granted
    per renewal for the tier. ``monthly_ceiling`` is the per-plan monthly credit
    CAP (integer credits, or None = uncapped) credit-quota enforcement caps spend
    against; a workspace with no/unknown plan resolves to the Free ceiling (the
    fail-closed trial cap), never None/uncapped. ``max_seats`` / ``max_pockets`` /
    ``max_connectors`` are the SMB resource ceilings (integer, or None = uncapped
    for Enterprise) the seat / pocket-create / connector-enable gates enforce at
    create time; a no/unknown-plan workspace resolves to the Free values
    (fail-closed), never None/uncapped. ``max_call_seconds_per_day`` is the daily
    LiveKit CALL-TIME budget in seconds (integer, or None = uncapped for
    Enterprise) the LiveKit room-create gate enforces at call-start time; a
    no/unknown-plan workspace resolves to the Free value (0 = no calls),
    fail-closed, never None/uncapped. ``max_storage_bytes`` is the workspace S3
    STORAGE cap in bytes (integer, or None = uncapped for Enterprise) the
    uploads pipeline enforces at upload time; a no/unknown-plan workspace
    resolves to the Free value (5 GB), fail-closed, never None/uncapped.

    ``included_sites`` is how many published Paw Sites this plan CARRIES at
    ``staff`` quality — custom domain, no attribution badge, visitor concierge —
    for no extra money and no credit debit (Free 0, Go 1, Pro 3, Pro Max 10,
    Enterprise None = uncapped). It is unlike every other ceiling here: the rest
    cap something the workspace is billed for anyway, while this one decides
    whether a site is billed AT ALL. Sites beyond it fall back to the per-site
    ladder and are bought from the credit wallet as before.

    The concierge allowance that comes with those sites is NOT a field here. A
    carried site is a ``staff`` site, and ``staff`` sells 200 conversations a month
    to the site it covers — counted per widget by
    ``billing.enforcement.concierge_conversation_quota_exceeded``. Three carried
    sites are three private blocks of 200, not one shared pool, so there is no
    workspace-level number to resolve.

    ``site_source_visible`` is "may this account read the SOURCE CODE of the sites
    it owns" — the generated markup, styles and scripts behind a published Paw
    Site. It is the only boolean on this class, and the only field here with a
    default, so both facts are worth stating:

    It is a WORKSPACE capability rather than a per-site one, and that is not a
    convenience. Source is a field on the POCKET, and pockets are workspace-scoped
    — a site does not own the thing being read. Worse, ``create_draft_site`` sets
    neither ``plan_tier`` nor ``subscription_status``, so every pocket's Site row
    sits on the free floor from pocket-create until its first publish; a per-site
    gate would therefore withhold source on every DRAFT, from paying customers,
    for exactly as long as they were authoring it.

    It defaults to ``False`` because ``features`` above it already carries a
    default, so dataclass ordering leaves no choice — and ``False`` is the only
    safe default to have been forced into. A construction that forgets this field
    WITHHOLDS source; it cannot leak code by omission. Do not "fix" the default to
    ``True`` to spare a caller an argument.
    """

    workspace_id: str
    plan: str
    monthly_credit_allotment: int
    monthly_ceiling: int | None
    max_seats: int | None
    max_pockets: int | None
    max_connectors: int | None
    max_call_seconds_per_day: int | None
    max_storage_bytes: int | None
    included_sites: int | None
    features: frozenset[str] = field(default_factory=frozenset)
    # Fail closed: an omitted field withholds source rather than granting it.
    site_source_visible: bool = False


@dataclass(frozen=True)
class SiteEntitlements:
    """What ONE site is entitled to, resolved from its own per-site plan.

    A second scope beside ``Entitlements``. Sites are the only thing this system
    bills per-object, so "may THIS site drop its badge" is not answerable from the
    workspace plan — it depends on ``Site.plan_tier`` and, critically, on whether
    that site's own subscription is actually paying.

    ``subscription_active`` is the load-bearing field and the reason this class
    exists. A cancelled per-site subscription sets ``subscription_status`` and
    LEAVES ``plan_tier`` on the paid key — nothing resets it — so a resolver that
    reads the tier alone hands a cancelled site every paid capability forever.
    The same hole is open wider today: with no Dodo product configured, a paid
    publish records its intended tier with NO live charge and
    ``subscription_status="none"``. Every paid capability below is therefore
    gated on the tier granting it AND the subscription being active.

    Only "pending" is worth a note among the inactive states: a pending site is
    not deployed yet (the charge-first flow deploys on activation), so failing
    closed on it cannot badge a live paying site.

    ``max_domained_sites`` is the odd one out and worth reading twice. Every other
    capability here is a PAID grant, gated on an active subscription. This one is a
    FLOOR grant: the base tier confers 1 with no subscription, because free now
    includes a custom domain. An active paid subscription replaces the floor with
    the tier's own value (None = uncapped). A LAPSED paid site therefore falls back
    to the floor's 1 rather than to 0 — it keeps what free would have given it.
    The unit is the SITE: how many hostnames sit on one site is a separate cap,
    enforced at the attach seam, not here.

    ``custom_domain`` is derived from it (``!= 0``) rather than stored separately.
    It answers "may this site have a custom domain at all"; whether the WORKSPACE
    has room for another is a count the resolver cannot answer, because counting
    needs the site collection and ``entitlements`` may not import ``models.site``.

    Deliberately ABSENT: ``conv_allowance``, ``conv_rate_usd`` and
    ``white_label``. The first two wait on which meter owns a concierge run, and
    the third on an org entity that does not exist. A field that always returns
    0/False reads as implemented, which is worse than its absence.

    ``analytics`` is a PAID grant and deliberately not derived here: it is read
    off ``site_analytics_entitled``, the one predicate the publish seam and the read
    endpoint already share. A fourth expression of the same rule is how a site ends
    up counting visitors it may not be shown, or being shown a blank chart it is
    paying for. It has no default for the same reason nothing else here does —
    every construction must state the answer, so a capability cannot be granted by
    forgetting it.

    ``concierge_enabled`` and ``concierge_entitled`` are two different questions
    and are deliberately NOT folded into one boolean. The first is the owner's own
    kill switch, echoed unchanged; the second is whether the site's plan sells the
    concierge at all. Collapsing them would make "off" unattributable — support
    could not tell an owner who switched it off from an owner whose subscription
    lapsed, and the dashboard could not offer the right remedy. The public seams
    refuse identically on either (see ``concierge_available``); only the reason
    differs, and the reason is what a human needs.
    """

    site_id: str
    workspace_id: str
    plan_tier: str
    subscription_active: bool
    badge_required: bool
    custom_domain: bool
    max_domained_sites: int | None
    analytics: bool
    concierge_enabled: bool
    concierge_entitled: bool

    @property
    def concierge_available(self) -> bool:
        """May this site actually serve its concierge right now?

        The AND of the owner's intent and the plan's permission — the single
        question every public paw-bar seam asks, so no caller has to remember to
        check both. A caller that reads only ``concierge_enabled`` (as every seam
        did before the billing gate existed) serves a free site's concierge, which
        is the hole this property closes.
        """
        return self.concierge_enabled and self.concierge_entitled
