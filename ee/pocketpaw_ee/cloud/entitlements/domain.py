# ee/pocketpaw_ee/cloud/entitlements/domain.py — the frozen, framework-free
# value object the entitlements resolver returns (BC-6, the Entitlement
# primitive).
#
# ``Entitlements`` is the normalized "what is this workspace entitled to" shape:
# its resolved plan key, the plan's feature set, its monthly credit allotment, and
# its monthly credit ceiling (the quota cap). It carries no framework type (no
# Beanie, no FastAPI) so the service can build it and the DTO layer can map it
# without either reaching into the other. It is derived from the EXISTING
# ``Workspace.plan`` field + the billing plan catalog — there is no event
# projection here.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.
# Updated 2026-06-30 (feat/billing-quota-enforcement, chunk 1): added
#   ``monthly_ceiling: int | None`` next to ``monthly_credit_allotment`` — the
#   per-plan monthly credit CAP (None = uncapped) the resolver populates from the
#   plan catalog and later quota chunks enforce against.
# Updated 2026-07-08 (feat/billing-smb-caps): added ``max_seats`` / ``max_pockets``
#   / ``max_connectors`` (all ``int | None``, None = uncapped) beside
#   ``monthly_ceiling`` — the SMB resource ceilings the resolver populates from the
#   plan catalog and the seat / pocket-create / connector-enable gates enforce
#   against at create time. Fail-closed to the Free value on the fallback path.
# Updated 2026-08-08 (feat/billing-rbac-member-caps): added
#   ``max_call_seconds_per_day`` (``int | None``, None = uncapped) — the daily
#   LiveKit CALL-TIME budget in seconds the LiveKit room-create gate enforces
#   at call-start time. Fail-closed to the Free value (0 = no calls) on the
#   fallback path.
# Updated 2026-08-08 (feat/billing-storage-caps): added ``max_storage_bytes``
#   (``int | None``, None = uncapped) — the workspace S3 STORAGE cap in bytes the
#   uploads pipeline enforces at upload time. Fail-closed to the Free value
#   (5 GB) on the fallback path.
# Updated 2026-08-15 (feat/sites-concierge-entitlement): added
#   ``SiteEntitlements.concierge_entitled`` + the ``concierge_available`` property.
#   ``concierge_enabled`` was a PASS-THROUGH of the owner's toggle that consulted no
#   plan at all, so a free site served a concierge indefinitely. The two questions
#   stay separate fields on purpose — see the class docstring.
# Updated 2026-08-21 (feat/site-free-custom-domain, PW-1): added
#   ``SiteEntitlements.max_domained_sites`` (``int | None``, None = uncapped) — how
#   many SITES in the workspace may carry a custom domain, counted in SITES rather
#   than hostnames so apex + ``www`` on one site spend one. Unlike every other
#   field here it is a FLOOR GRANT: the base tier's 1 resolves with no subscription
#   at all, because free now includes a custom domain. ``custom_domain`` stays, now
#   derived as ``max_domained_sites != 0`` — it answers "may this site have one at
#   all", which is a different question from "has the workspace room for another".
# Updated 2026-08-13 (feat/sites-site-entitlements): added ``SiteEntitlements`` —
#   the PER-SITE shape, a second scope beside the workspace one. Sites are the
#   only thing billed per-object (``Site.plan_tier`` + ``subscription_status``),
#   so "what may this SITE do" cannot be answered by the workspace resolver.
#   Deliberately a separate frozen class rather than fields bolted onto
#   ``Entitlements``: they resolve from different sources, on different cadences,
#   and a caller that wants one almost never wants the other.
# Updated 2026-09-02 (feat/sites-analytics-entitlement-field, SA-5): added
#   ``SiteEntitlements.analytics`` (bool) — may this site's visitors be counted.
#   A PAID grant, resolved by ``site_analytics_entitled`` rather than re-derived,
#   because the publish seam and the read endpoint already share that predicate and
#   a third copy is how the two drift. Exposing it lets the dashboard disable the
#   panel and name the reason instead of rendering a refusal.

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
    features: frozenset[str] = field(default_factory=frozenset)


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
