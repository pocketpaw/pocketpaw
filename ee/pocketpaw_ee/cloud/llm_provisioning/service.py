# ee/pocketpaw_ee/cloud/llm_provisioning/service.py — the per-tenant LiteLLM
# virtual-key lifecycle (MCG-8). Module-level ``async def`` API (NOT a class, per
# the EE cloud rule, mirroring ``credits.service`` / ``metering.service``). Sole
# owner of writes to the ``LiteLLMTenantKey`` doc (entity isolation — only THIS
# module imports ``models.litellm_key``).
#
# Two jobs:
#
#   1. PROVISIONING (``ensure_tenant_key``) — idempotently ensure a budgeted,
#      rate-limited LiteLLM virtual key exists for a workspace. Mints one via the
#      proxy admin API (POST /key/generate, with metadata={workspace_id}) the
#      FIRST time, persists the workspace -> key mapping (upsert on the unique
#      ``workspace`` index), and on every later call returns the stored key
#      WITHOUT a second proxy call. The budget / rpm / tpm / allowed-models come
#      from runtime settings (``load_key_budget``) — config-driven, never
#      hardcoded. ``get_tenant_key`` reads the key back for spend attribution on
#      the tenant's proxy calls.
#
#   2. SPEND INGESTION (``ingest_tenant_spend``) — read the tenant key's proxy
#      spend (GET /spend/logs?api_key=<key>) and feed it into the EXISTING credit
#      ledger via ``credits.service.debit``. This entity does NOT own a ledger; it
#      plugs into BC-1's. Each spend row is debited EXACTLY ONCE: the debit is
#      keyed ``litellm:{request_id}`` against BC-1's unique
#      ``(workspace, idempotency_key)`` index (the real guard), and the
#      ``last_spend_ingest_ts`` high-water mark bounds the read so a re-sweep
#      doesn't re-read settled rows. ``allow_negative=True`` + a DISTINCT cause
#      (``litellm_spend``) — proxy compute already happened, so it bills fully,
#      and the distinct cause keeps these movements separable from BC-3's
#      ``compute_spend`` rows on the dashboard.
#
# DOUBLE-BILL BOUNDARY (read this before enabling ingestion): the proxy's
# /spend/logs includes EVERY call routed through the proxy — including the text
# chat runs BC-3 metering already bills per ``ChatRunDoc`` (keyed ``run:{run_id}``,
# cause ``compute_spend``). Running BOTH unconditionally would double-bill text
# chat. So spend ingestion is GATED OFF by default behind
# ``settings.litellm_spend_ingest_enabled`` (POCKETPAW_LITELLM_SPEND_INGEST,
# default False). It is the future single-source-of-truth path — bill ALL compute
# from proxy spend and retire per-run metering — but flipping that seam (and the
# row-level dedup against BC-3, e.g. skipping rows whose metadata carries a
# ``run_id`` already billed) is a deliberate product decision, NOT a default. The
# PROVISIONING half is always-on and unconditional; only the ingestion half is
# gated. Today's live, attributed path is media (it tags ``user=workspace_id`` so
# the proxy logs spend per tenant) once provisioning hands media the tenant key.
#
# Rule 6 — validate at entry (a workspace id is required). Rule 7 — every read is
# tenant-filtered on ``workspace``. Rule 10 — only ``CloudError`` subclasses
# propagate to HTTP; a proxy admin failure raises ``LiteLLMAdminError`` (a plain
# exception) which a system-job caller logs + retries, never a bare HTTPException.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): new entity.
# Updated 2026-09-02 (feat/litellm-spend-cutover): two changes that make ``live``
#   safe to flip. ``prepare_spend_cutover`` stamps the high-water mark on every
#   provisioned tenant, so the first live sweep bills FORWARD instead of charging
#   the whole proxy history — which would have re-billed every chat run BC-3
#   already charged, under a key BC-1 cannot dedup against. And the high-water
#   skip now compares PARSED instants rather than raw ISO strings: the proxy emits
#   naive, Z-suffixed and offset-bearing shapes interchangeably, and a naive
#   timestamp is a string PREFIX of the offset-bearing form of the SAME instant,
#   so a boundary row was silently dropped by the meter that is meant to be the
#   only one charging. See docs/deployment/litellm-billing-cutover.md.
# Updated 2026-06-26 (feat/litellm-billing-cutover, WU-F): three changes for the
# billing cutover from per-run metering (BC-3) to LiteLLM as the single meter,
# done through a safe shadow-compare phase.
#   1. ``spend_mode`` / ``reconcile_gap_threshold`` — read the 3-position cutover
#      switch (POCKETPAW_LITELLM_SPEND_MODE off|shadow|live). The legacy INGEST bool
#      is honoured ONLY as far as ``shadow`` — ``live`` requires an EXPLICIT mode, so
#      deploying WU-F never auto-flips an old bool-setter into live billing (a
#      one-time deprecation notice fires when the legacy bool is seen).
#      ``spend_ingest_enabled`` is now a back-compat shim over it.
#   2. ``reconcile_tenant_spend`` — the SHADOW compare. Reads proxy spend + the
#      BC-3 ``compute_spend`` ledger debits over the same window and records a
#      reconciliation row (litellm vs bc3 + delta + coverage_gap). It DEBITS
#      NOTHING and never advances the high-water mark — BC-3 keeps billing during
#      shadow. The cross-entity ledger read goes through the credits service's
#      tenant-filtered ``sum_debits_by_cause`` (entity-isolation preserved).
#   3. ``ingest_tenant_spend`` high-water boundary fix — the same-second skip was
#      ``<=`` (dropped a distinct boundary row on a later sweep → under-bill); it
#      is now strict ``<`` with the ``litellm:{request_id}`` ledger dedup as the
#      exactly-once guard at the boundary. See the inline note at the loop.
#
# Updated 2026-09-02 (fix/bill-workspaces-the-sweep-cannot-see): the previous entry
# made a chat run's spend READABLE. This one makes it reachable, because nothing
# was asking for it.
#
# The sweep's tenant list was ``list_provisioned_workspaces`` — the workspaces we
# minted a virtual key for. Chat needs no such key: it authenticates with the
# deployment key and names its workspace in the request body. So the set of
# workspaces that spend and the set we swept were free to drift apart, and on the
# deployment where this surfaced they had drifted completely: three provisioned
# tenants with no proxy spend, three spending customers with no provisioned key,
# zero overlap. Every tick read three empty tenants, reported
# ``3/3 tenants -> 0 credits``, and gave the chat away.
#
#   * ``list_sweepable_workspaces`` — the union of the provisioned tenants and the
#     customers the PROXY reports. Asking the proxy who spent is the only way to
#     learn about a workspace our own tables never recorded.
#   * ``ingest_tenant_spend`` bills a keyless workspace instead of returning zero.
#     ``_spend_bookkeeping_row`` gives it a row to hold the high-water mark, and
#     ``ensure_tenant_key`` fills that same row in if a key is minted later rather
#     than colliding with it on the UNIQUE index.
#   * ``spend_attribution_coverage`` splits its remainder into rows naming an
#     unswept workspace and rows naming nobody. It reported both as "no ``user``
#     field", which is how this bug hid behind a diagnostic written to catch it.
#
# The discovery path checks every id against a real ``Workspace`` before billing
# it: the value reaches us from a request body, and the cost of trusting it is a
# ledger debit against a tenant that does not exist.
#
# Updated 2026-09-02 (feat/proxy-spend-ingest-by-customer): ``ingest_tenant_spend``
# now reads a tenant's spend BY CUSTOMER as well as by virtual key.
#
# The per-key read could never see a chat run. Both agent backends authenticate
# with ``settings.litellm_api_key`` — the DEPLOYMENT's key — so a chat row is
# stamped with that key and ``/spend/logs?api_key=<tenant key>`` does not match it.
# With ``live`` gating BC-3's per-run metering off so exactly one meter charges,
# the one meter was reading a filter that excludes the product's main cost centre:
# production logged ``ingested spend for 3/3 tenants -> 0 credits`` against runs the
# proxy had priced in dollars. Nothing errored, because nothing was wrong with the
# read — it was scoped to the wrong thing.
#
# The companion change puts the workspace id in each request's ``user`` field, which
# the proxy records as the row's ``end_user``. This side reads it back with
# ``GET /spend/logs/v2?end_user=<workspace>``. Four things worth knowing:
#
#   * BOTH reads run, and their rows are merged by ``request_id``. The customer read
#     should be a superset today (Studio and the media MCP server tag ``user`` as
#     well as sending the tenant key), but "should be" is not a thing to assume when
#     the failure mode is an under-bill, and a row seen twice is billed once —
#     ``litellm:{request_id}`` is the ledger key either way.
#   * The customer read is DATE-BOUNDED, so it needs a window rather than the
#     high-water mark alone. It starts a short overlap BEFORE the mark
#     (``_SPEND_READ_OVERLAP``) and, for a tenant with no mark at all, at the key
#     doc's ``createdAt`` — the point from which this deployment has been the tenant's
#     proxy, and so the earliest spend that can be theirs.
#   * Rows from the customer read BYPASS the high-water skip. The mark exists to
#     bound an unbounded read; this read is already bounded, and honouring the mark
#     here would re-introduce the exact bug WU-F fixed at the boundary — a row that
#     lands late with a ``startTime`` older than the mark would be skipped forever.
#     Exactly-once still holds: it always came from the ledger key, never the mark.
#   * A workspace with no provisioned key still returns a zero result, because the
#     high-water mark lives on that document and there is nowhere else to keep it.
#     The sweep only iterates provisioned tenants, so this is not a hole so much as
#     the edge of the map — and the sweep's coverage check is what makes spend
#     outside it visible.

# Updated 2026-09-04 (fix/litellm-spend-leaks): three changes, one root cause —
# money and noise were both being lost to arithmetic applied at the wrong grain.
#
#   1. ``ingest_tenant_spend`` CARRIES the sub-credit remainder instead of dropping
#      it. It converted each row on its own and skipped anything that rounded to
#      zero, so with the default card (round(usd * 250)) every call under $0.002
#      billed nothing — permanently, because the high-water mark advanced past the
#      dropped row in the same pass and nothing accumulated. Per-run metering shares
#      the conversion and never showed this, because it priced a whole RUN at once;
#      the cutover kept the arithmetic and made the unit ~100x smaller.
#      SIZE, honestly: ``round`` is unbiased, so a row at $0.003 rounds UP to a credit
#      it has not earned and offsets one at $0.0015 rounding down. Over a week of real
#      traffic (2026-08-28..09-04) the two cancelled — one tenant over-billed by a
#      credit, another under-billed by one. This is not a steady drain; it is being
#      wrong per tenant in BOTH directions, and unboundedly wrong for a workload of
#      uniformly cheap calls where nothing rounds up to offset anything. A thousand
#      $0.0015 requests cost $1.50 and bill zero. The carry makes it exact instead.
#      It does NOT bill what was already dropped: the reads start at the mark minus
#      ``_SPEND_READ_OVERLAP``. A dropped row has no ledger entry, so rewinding a
#      tenant's mark WOULD recover it and the request_id key still stops a double
#      debit — but never rewind past the ``prepare_spend_cutover`` mark, where BC-3
#      owns the billing under a key this ledger cannot dedup against.
#      The carry also needs a LEASE, which the ledger cannot provide. It is a
#      read-modify-write: two overlapping ingests read the same remainder, each folds
#      in a row the other has not seen, each crosses the credit line, and both debit
#      under different ``litellm:{request_id}`` keys — so the unique index never
#      fires and the tenant quietly pays twice. ``ingest_tenant_spend`` is now a
#      lease wrapper around ``_ingest_tenant_spend_locked``; a caller that cannot
#      take the lease returns ``lease_skipped`` rather than waiting.
#      The already-recorded check MOVED ABOVE the conversion as part of this: a row
#      folded into the remainder carries a zero-value ledger entry rather than a
#      debit, and the customer read re-offers 15 minutes of settled rows every tick,
#      so without the check first the same fraction would be folded in once per
#      sweep. That would turn an under-bill into an over-bill.
#   2. ``reconcile_tenant_spend`` sums the window's USD and converts ONCE. Doing it
#      per row understated the LiteLLM side of the shadow compare by exactly the
#      rows the live ingest was dropping, which is how the cutover looked safe.
#   3. ``spend_attribution_coverage`` PRICES its remainder and splits it three ways.
#      A proxy logs traffic of its own — a human trying a model in its admin
#      dashboard, its periodic health check — that can never name a workspace and is
#      nobody's to bill. Counting it as "served and not billed" made the check
#      permanently red while the runbook said to treat any non-zero count as
#      blocking. Measured on the production proxy 2026-09-03: all 8 flagged rows
#      were exactly that, worth $0.00014545 between them.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.catalog.admin_client import LiteLLMAdminClient, LiteLLMAdminError
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.llm_provisioning.domain import (
    CutoverPreparation,
    KeyBudget,
    ProvisionResult,
    SpendCoverage,
    SpendCredits,
    SpendIngestResult,
    SpendReconciliation,
)
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey
from pocketpaw_ee.cloud.models.spend_reconciliation import (
    SpendReconciliation as SpendReconciliationDoc,
)

logger = logging.getLogger(__name__)

# Once-per-process guard for the legacy-spend-bool deprecation notice (WU-F). The
# warning is emitted the first time the mode is resolved while the deprecated bool
# is the only thing set, so an operator is told their old flag now means ``shadow``
# (NOT ``live``) and that billing requires an explicit POCKETPAW_LITELLM_SPEND_MODE.
_legacy_bool_warned = False

# Business cause stamped on the ledger movement for proxy-attributed spend. KEPT
# DISTINCT from BC-3 metering's ``compute_spend`` so a dashboard / audit can tell
# proxy-gateway spend apart from per-run metered spend (and so the two paths can
# coexist without their idempotency namespaces colliding).
_LITELLM_SPEND_CAUSE = "litellm_spend"

# The BC-3 per-run metering cause (``metering.service._COMPUTE_SPEND_CAUSE``). The
# shadow compare sums the credit ledger's ``compute_spend`` debits to put BC-3 next
# to LiteLLM. Duplicated as a literal here ON PURPOSE — metering is a SIBLING entity,
# and importing its private constant would couple two cloud entities at module load
# (and the import-linter forbids the cross-entity reach). If BC-3's cause string ever
# changes, this literal + the matching test must change with it.
_BC3_COMPUTE_SPEND_CAUSE = "compute_spend"

# The key-alias prefix the proxy stamps on a tenant's virtual key, for operator
# legibility in the proxy admin UI + spend logs.
_KEY_ALIAS_PREFIX = "ws-"

# How far BEFORE the high-water mark the customer-scoped read starts.
#
# Proxy spend rows are written when a call COMPLETES but stamped with when it
# STARTED, and the write is batched, so a row can appear after the mark has already
# moved past its ``startTime``. A date-bounded read that began exactly at the mark
# would never see it. The overlap re-reads a few minutes of settled rows each
# sweep — they cost one ``is_recorded`` lookup apiece and debit nothing — to buy
# back rows that would otherwise be lost silently and permanently.
_SPEND_READ_OVERLAP = timedelta(minutes=15)

# Team ids LiteLLM stamps on its OWN traffic. Rows carrying one of these are the
# proxy talking to itself and can never name a workspace: ``litellm-dashboard`` is
# a human trying a model in the proxy's admin UI, ``litellm-internal-health-check``
# is its periodic model probe. Neither is ours to bill, so counting them as spend
# "being served and not billed" is what made the coverage check permanently red.
#
# Matched on ``team_id`` rather than on a zero cost: the dashboard's test chats DO
# cost money (a real model answers them), just nobody's money. A cost filter would
# also swallow a genuinely untagged production call that happened to be free, which
# is the case worth keeping visible — free models do not stay free.
_PROXY_INTERNAL_TEAMS = frozenset({"litellm-dashboard", "litellm-internal-health-check"})

# The timestamp format /spend/logs/v2 parses. It also accepts a bare date; second
# precision keeps the window tight enough that the overlap above is the only
# deliberate re-read.
_PROXY_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Config — budgets + the spend rate card from runtime settings (NEVER hardcoded).
# ---------------------------------------------------------------------------


def load_key_budget() -> KeyBudget:
    """Build the per-tenant key budget from runtime settings.

    Reads (all with sane defaults so a fresh deploy provisions a sensible key):
      * POCKETPAW_TENANT_MAX_BUDGET_USD  — USD ceiling per ``budget_duration``.
      * POCKETPAW_TENANT_BUDGET_DURATION — reset window (LiteLLM duration string).
      * POCKETPAW_TENANT_RPM_LIMIT       — requests-per-minute cap (0 == unset).
      * POCKETPAW_TENANT_TPM_LIMIT       — tokens-per-minute cap (0 == unset).

    The allowed-models allowlist defaults to empty (all models the proxy serves) —
    a deployment that wants to restrict a tenant can layer that on later without
    changing the mint path. Lazy import so the entity has no import-time dependency
    on the settings singleton.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    rpm = int(settings.tenant_rpm_limit)
    tpm = int(settings.tenant_tpm_limit)
    budget = float(settings.tenant_max_budget_usd)
    return KeyBudget(
        max_budget_usd=budget if budget > 0 else None,
        budget_duration=settings.tenant_budget_duration or None,
        rpm_limit=rpm if rpm > 0 else None,
        tpm_limit=tpm if tpm > 0 else None,
        models=[],
    )


def load_spend_credits() -> SpendCredits:
    """Build the proxy-spend rate card from runtime settings.

    Reuses the SAME ``billing_markup`` / ``credit_usd`` settings the BC-3 meter
    uses (``metering.service.load_rate_card``) so a dollar of compute bills the
    same credits regardless of which path attributes it. Lazy import for the same
    reason as above.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    return SpendCredits(
        markup=float(settings.billing_markup), credit_usd=float(settings.credit_usd)
    )


def warn_legacy_spend_bool_once() -> None:
    """Emit a ONE-TIME deprecation notice when the legacy spend bool is the only
    cutover signal set (WU-F money-safety guard).

    Fires at most once per process, the first time the mode is resolved while
    ``POCKETPAW_LITELLM_SPEND_INGEST_ENABLED=true`` AND the new
    ``POCKETPAW_LITELLM_SPEND_MODE`` is left at its ``off`` default. Tells the
    operator the legacy bool now resolves to ``shadow`` (reads + compares, debits
    NOTHING) and that to actually bill from proxy spend they must EXPLICITLY set
    ``POCKETPAW_LITELLM_SPEND_MODE=live``. This is why deploying WU-F can never
    auto-flip an old bool-setter into live billing — the bool can only ever reach
    the safe shadow mode, loudly, with this notice.
    """
    global _legacy_bool_warned
    if _legacy_bool_warned:
        return
    from pocketpaw.config import get_settings

    settings = get_settings()
    if settings.litellm_spend_mode == "off" and settings.litellm_spend_ingest_enabled:
        _legacy_bool_warned = True
        logger.warning(
            "DEPRECATION (WU-F): POCKETPAW_LITELLM_SPEND_INGEST_ENABLED is set but "
            "POCKETPAW_LITELLM_SPEND_MODE is unset — the legacy bool now resolves to "
            "'shadow' (read-only reconciliation, NO debits), NOT live billing. "
            "LiteLLM will NOT charge and BC-3 per-run metering keeps billing. To make "
            "LiteLLM the sole meter you must EXPLICITLY set "
            "POCKETPAW_LITELLM_SPEND_MODE=live."
        )


def spend_mode() -> str:
    """The LiteLLM billing-cutover mode for this deployment: ``off`` | ``shadow`` |
    ``live`` (WU-F).

    Delegates to ``Settings.effective_spend_mode()``. The legacy
    ``POCKETPAW_LITELLM_SPEND_INGEST_ENABLED`` bool is honoured ONLY as far as
    ``shadow`` — it never resolves to ``live`` (that requires an explicit
    ``POCKETPAW_LITELLM_SPEND_MODE=live``), so merely deploying WU-F can never flip
    an old bool-setter into live billing. The first resolution that sees the legacy
    bool emits a one-time deprecation notice. Provisioning is unaffected by the mode
    (always on); only the spend SWEEP behaviour changes.
    """
    warn_legacy_spend_bool_once()
    from pocketpaw.config import get_settings

    return get_settings().effective_spend_mode()


def spend_ingest_enabled() -> bool:
    """DEPRECATED back-compat shim — whether the spend sweep DEBITS (mode == live).

    Superseded by ``spend_mode()`` (WU-F). Kept so any caller still asking the old
    yes/no question gets the right answer: only ``live`` mode debits proxy spend.
    ``shadow`` returns False here (it reads + compares but never debits), matching
    the original contract that True meant "ingestion debits the ledger".
    """
    return spend_mode() == "live"


def reconcile_gap_threshold() -> int:
    """The shadow-compare coverage-gap threshold in credits (WU-F).

    Reads ``POCKETPAW_LITELLM_RECONCILE_GAP_THRESHOLD_CREDITS`` (default 10). A
    reconciliation row is flagged ``coverage_gap`` when ``abs(delta)`` exceeds this.
    Clamped to >= 0 so a negative config can't make every window read as a gap.
    """
    from pocketpaw.config import get_settings

    return max(0, int(get_settings().litellm_reconcile_gap_threshold_credits))


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _require_workspace(workspace: str) -> None:
    if not workspace:
        raise ValidationError("llm_provisioning.invalid_workspace", "workspace is required")


def _num(value: Any) -> float:
    """Coerce a proxy spend value to a float, tolerating None / strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    """Coerce a token count to a non-negative int, tolerating None / strings."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


# ---------------------------------------------------------------------------
# Provisioning — idempotently ensure a tenant has a budgeted virtual key.
# ---------------------------------------------------------------------------


async def get_tenant_key(workspace: str) -> str | None:
    """Return the workspace's LiteLLM virtual key, or None when not yet
    provisioned. Tenant-filtered read (Rule 7). The proxy-calling paths (media /
    the harness) use this to attribute spend + enforce the tenant budget."""
    _require_workspace(workspace)
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    return doc.litellm_key if doc is not None else None


async def list_provisioned_workspaces() -> list[str]:
    """Every workspace that has a live LiteLLM virtual key.

    The cutover sweep iterates these to run the per-mode spend logic. A SYSTEM-job
    read across tenants (no per-tenant scope) — but it only returns workspace IDs
    that have a real key; an unprovisioned workspace has nothing to sweep. Reading
    ``LiteLLMTenantKey`` stays inside THIS entity (only this module touches that
    doc), so the cutover sweeper goes through this helper rather than querying the
    doc itself.
    """
    docs = await LiteLLMTenantKey.find(LiteLLMTenantKey.litellm_key != None).to_list()  # noqa: E711
    return [d.workspace for d in docs if d.litellm_key]


async def _existing_workspace_ids(candidates: list[str]) -> set[str]:
    """Which of ``candidates`` are real workspaces. Bogus / deleted ids drop out.

    The candidates come off the proxy's customer list, which is ultimately a
    caller-supplied string: whatever a request put in its ``user`` field became a
    customer. Everything ours puts there is a workspace id, but "ours" is an
    assumption about a value that crossed the wire, and the consequence of
    trusting it is a credit-ledger debit against a workspace that does not exist.
    So it is checked rather than assumed.

    Reaches into the ``Workspace`` doc, which belongs to the workspace entity, for
    a single bulk existence read. The alternative — a public helper over there —
    would be the tidier boundary, but this module is the only caller and the
    import is lazy so the two services still load independently.
    """
    if not candidates:
        return set()

    from bson import ObjectId
    from bson.errors import InvalidId

    from pocketpaw_ee.cloud.models.workspace import Workspace

    by_oid: dict[ObjectId, str] = {}
    for raw in candidates:
        try:
            by_oid[ObjectId(raw)] = raw
        except (InvalidId, TypeError):
            # Not even shaped like a workspace id. Nothing to sweep, and saying so
            # once at debug beats a warning per tick for a value we will never bill.
            logger.debug(
                "llm_provisioning: proxy customer %r is not a workspace id — skipping",
                raw,
            )
    if not by_oid:
        return set()

    docs = await Workspace.find({"_id": {"$in": list(by_oid)}}).to_list()
    return {by_oid[d.id] for d in docs if d.id in by_oid}


async def list_sweepable_workspaces(*, admin_client: LiteLLMAdminClient | None = None) -> list[str]:
    """Every workspace the spend sweep should visit this tick.

    The union of two sources, because neither one alone is the set of tenants that
    owe money:

      * the workspaces we PROVISIONED a virtual key for — spend that arrives
        attributed to that key, which is Studio and the media server; and
      * the workspaces the PROXY has recorded a customer for — spend that arrives
        attributed to the request's ``user`` field, which is chat.

    Only the first was swept until 2026-09-02, and a workspace can spend its whole
    life without appearing in it: chat authenticates with the deployment key and
    never needs a tenant key at all. On the deployment where this was found, the
    three provisioned tenants had no proxy spend and the three spending customers
    had no provisioned key — the two sets were disjoint, every sweep reported
    ``3/3 tenants -> 0 credits``, and every real dollar of chat was free.

    The proxy half is best-effort: if that call fails we sweep the provisioned
    tenants alone rather than skipping the tick, which is the same partial-outage
    rule the two spend reads follow. It fails toward the old behaviour, which
    under-bills — the safe direction for a mistake that moves money.
    """
    provisioned = await list_provisioned_workspaces()
    client = admin_client if admin_client is not None else LiteLLMAdminClient()

    try:
        customers = await client.list_customers()
    except Exception:
        logger.exception(
            "llm_provisioning.list_sweepable_workspaces: could not read the proxy's "
            "customer list — sweeping the %d provisioned tenant(s) alone this tick. "
            "Spend from any UNPROVISIONED workspace goes unbilled until this recovers",
            len(provisioned),
        )
        return provisioned

    known = set(provisioned)
    discovered = await _existing_workspace_ids([c for c in customers if c not in known])
    if discovered:
        logger.info(
            "llm_provisioning.list_sweepable_workspaces: %d workspace(s) have proxy "
            "spend but no provisioned key — sweeping them too: %s",
            len(discovered),
            ", ".join(sorted(discovered)),
        )
    # Provisioned order first, then the discovered ids sorted, so the sweep's
    # per-tenant logging is stable tick to tick.
    return provisioned + sorted(discovered)


async def prepare_spend_cutover(
    *,
    at: datetime | None = None,
    dry_run: bool = False,
) -> CutoverPreparation:
    """Stamp the billing-cutover mark so ``live`` mode bills forward, not backward.

    **Run this before setting POCKETPAW_LITELLM_SPEND_MODE=live.** Without it the
    first live sweep bills each tenant's ENTIRE ``/spend/logs`` history in one
    debit run, because ``ingest_tenant_spend`` skips rows older than
    ``last_spend_ingest_ts`` and that field is ``None`` until something ingests.
    Worse than the size of that bill is its overlap: those rows include every text
    chat run BC-3 already charged, under a different idempotency key
    (``litellm:{request_id}`` vs ``run:{run_id}``), so BC-1's unique index cannot
    dedup them. The module header calls row-level dedup against BC-3 a deliberate
    product decision rather than a default, and there is no ``run_id`` on the key
    metadata to dedup against in any case.

    Stamping a mark makes the seam clean instead of overlapping: **BC-3 owns every
    run before ``at``, LiteLLM owns every proxy row after it.**

    Only tenants with NO mark are stamped. A tenant already ingesting has a live
    high-water mark, and moving it forward would silently drop the spend between
    the old mark and ``at``.

    ORDER MATTERS, and the ordering is the operator's to get right:

      1. Let the BC-3 sweep drain, so no completed run is still unbilled. The
         sweep no-ops entirely once the mode is ``live``, and a run left unbilled
         at the flip whose proxy rows predate ``at`` is billed by NEITHER meter.
      2. Call this (``dry_run=True`` first to see the counts).
      3. Confirm ``provisioned`` matches the number of workspaces you expect to
         bill. **Live mode bills provisioned tenants only** — a workspace with no
         proxy key is swept by nothing and its usage is free.
      4. Set the mode to ``live``.

    ``at`` defaults to now (UTC). ``dry_run`` reports what would be stamped and
    writes nothing.
    """
    at = at or datetime.now(tz=UTC)
    cutover_at = at.isoformat()

    docs = await LiteLLMTenantKey.find(LiteLLMTenantKey.litellm_key != None).to_list()  # noqa: E711
    provisioned = [d for d in docs if d.litellm_key]

    unmarked = [d for d in provisioned if not d.last_spend_ingest_ts]
    already = len(provisioned) - len(unmarked)

    if not dry_run:
        for doc in unmarked:
            doc.last_spend_ingest_ts = cutover_at
            await doc.save()

    logger.info(
        "prepare_spend_cutover: %s cutover_at=%s provisioned=%d seeded=%d already_marked=%d",
        "DRY RUN — nothing written" if dry_run else "stamped",
        cutover_at,
        len(provisioned),
        len(unmarked),
        already,
    )
    return CutoverPreparation(
        cutover_at=cutover_at,
        provisioned=len(provisioned),
        seeded=len(unmarked),
        already_marked=already,
        dry_run=dry_run,
    )


async def ensure_tenant_key(
    workspace: str,
    *,
    budget: KeyBudget | None = None,
    admin_client: LiteLLMAdminClient | None = None,
) -> ProvisionResult:
    """Ensure a LiteLLM virtual key exists for ``workspace``. Idempotent.

    First call: mint a key via the proxy admin API (POST /key/generate) carrying
    the budget / rpm / tpm / allowed-models from ``budget`` (default:
    ``load_key_budget()``) and ``metadata={"workspace_id": workspace}`` so the
    proxy attributes the key to the tenant, then persist the workspace -> key
    mapping. Returns ``ProvisionResult(..., created=True)``.

    Every later call: the mapping already exists, so return the stored key with
    ``created=False`` and NO proxy call (the idempotency guarantee — a workspace
    never gets two proxy keys, even under a concurrent double-provision: the
    unique ``workspace`` index makes the second insert collide and we re-read the
    winner).

    ``admin_client`` is injectable for tests (an ``httpx.MockTransport``-backed
    client); production builds one bound to the deployment master key.
    """
    _require_workspace(workspace)

    # Fast path — already provisioned. No proxy call.
    existing = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    if existing is not None and existing.litellm_key:
        return ProvisionResult(
            workspace_id=workspace, litellm_key=existing.litellm_key, created=False
        )

    card = budget if budget is not None else load_key_budget()
    client = admin_client if admin_client is not None else LiteLLMAdminClient()
    alias = f"{_KEY_ALIAS_PREFIX}{workspace}"

    # Mint the key on the proxy. A failure here raises LiteLLMAdminError — we do
    # NOT persist a half-row, so a retry re-attempts the mint cleanly.
    body = await client.generate_key(
        key_alias=alias,
        max_budget=card.max_budget_usd,
        budget_duration=card.budget_duration,
        rpm_limit=card.rpm_limit,
        tpm_limit=card.tpm_limit,
        models=card.models or None,
        metadata={"workspace_id": workspace},
    )
    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise LiteLLMAdminError("LiteLLM /key/generate returned no key")

    # A KEYLESS row already exists when the spend sweep discovered this workspace
    # on the proxy before anything minted it a key. Fill that row in rather than
    # inserting beside it: ``workspace`` is UNIQUE, so an insert would collide and
    # be re-raised as a provisioning failure, and the row carries this tenant's
    # ``last_spend_ingest_ts`` — replacing it would rewind the high-water mark and
    # re-read spend the ledger has already been charged for.
    if existing is not None:
        existing.litellm_key = key
        existing.key_alias = alias
        existing.max_budget_usd = card.max_budget_usd
        existing.budget_duration = card.budget_duration
        existing.rpm_limit = card.rpm_limit
        existing.tpm_limit = card.tpm_limit
        existing.models = list(card.models)
        await existing.save()
        logger.info(
            "llm_provisioning.ensure_tenant_key: workspace=%s minted a key onto the row "
            "the spend sweep had already created (alias=%s)",
            workspace,
            alias,
        )
        return ProvisionResult(workspace_id=workspace, litellm_key=key, created=True)

    # Persist the mapping. Upsert-on-insert with the unique ``workspace`` index as
    # the concurrency guard: if a racing call minted + inserted first, our insert
    # collides — we swallow it and re-read the winner so we never store two rows
    # (the loser's freshly-minted proxy key is orphaned but harmless: unused, and
    # the proxy budget bounds its blast radius; revoking orphans is a noted
    # follow-up).
    doc = LiteLLMTenantKey(
        workspace=workspace,
        litellm_key=key,
        key_alias=alias,
        max_budget_usd=card.max_budget_usd,
        budget_duration=card.budget_duration,
        rpm_limit=card.rpm_limit,
        tpm_limit=card.tpm_limit,
        models=list(card.models),
    )
    try:
        await doc.insert()
    except Exception as exc:  # noqa: BLE001 — Beanie raises DuplicateKeyError here
        winner = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
        if winner is not None and winner.litellm_key:
            logger.info(
                "llm_provisioning.ensure_tenant_key: workspace=%s lost mint race; "
                "using existing key (orphaned a fresh proxy key)",
                workspace,
            )
            return ProvisionResult(
                workspace_id=workspace, litellm_key=winner.litellm_key, created=False
            )
        if winner is not None:
            # The racer was the spend sweep creating a KEYLESS bookkeeping row, not
            # another mint. Nobody has a key yet, so ours wins the row rather than
            # being thrown away with a provisioning error.
            winner.litellm_key = key
            winner.key_alias = alias
            await winner.save()
            return ProvisionResult(workspace_id=workspace, litellm_key=key, created=True)
        # Not a duplicate-key collision — re-raise so the caller sees the real error.
        raise LiteLLMAdminError(f"persisting tenant key failed: {exc}") from exc

    logger.info(
        "llm_provisioning.ensure_tenant_key: workspace=%s provisioned a LiteLLM key "
        "(alias=%s, budget=%s/%s, rpm=%s, tpm=%s)",
        workspace,
        alias,
        card.max_budget_usd,
        card.budget_duration,
        card.rpm_limit,
        card.tpm_limit,
    )
    return ProvisionResult(workspace_id=workspace, litellm_key=key, created=True)


# ---------------------------------------------------------------------------
# Spend ingestion — read the tenant key's proxy spend -> the EXISTING ledger.
# ---------------------------------------------------------------------------


def _row_id(row: dict[str, Any]) -> str | None:
    """The stable per-row id used as the ledger idempotency key. LiteLLM spend
    rows carry ``request_id`` (the canonical id); fall back to ``id``. None when
    neither is present (such a row can't be deduped safely, so it is skipped)."""
    rid = row.get("request_id") or row.get("id")
    return str(rid) if rid else None


def _row_start_time(row: dict[str, Any]) -> str | None:
    """The row's ISO ``startTime`` (the high-water-mark field). None when absent."""
    ts = row.get("startTime") or row.get("startTimeStamp")
    return str(ts) if ts else None


def _cached_tokens(row: dict[str, Any]) -> int:
    """The cached-input token count a spend row reports (the prompt-cache savings
    signal). LiteLLM nests it under ``prompt_tokens_details.cached_tokens``; some
    rows expose a flat ``cache_read_input_tokens``. Best-effort across shapes."""
    details = row.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _int(details.get("cached_tokens"))
        if cached:
            return cached
    return _int(row.get("cache_read_input_tokens") or row.get("cached_tokens"))


def _proxy_window(start: datetime, end: datetime) -> tuple[str, str]:
    """Format a window the way /spend/logs/v2 parses it."""
    return (
        _as_aware(start).strftime(_PROXY_DATE_FORMAT),  # type: ignore[union-attr]
        _as_aware(end).strftime(_PROXY_DATE_FORMAT),  # type: ignore[union-attr]
    )


def _customer_read_window(doc: LiteLLMTenantKey, *, now: datetime) -> tuple[datetime, datetime]:
    """The window the customer-scoped spend read should cover for ``doc``.

    Starts an overlap before the high-water mark, or — when nothing has ever been
    ingested for this tenant — at the moment we provisioned their key, which is the
    earliest instant any spend on this proxy can be theirs. That start is
    deliberately NOT "the beginning of time": a tenant whose mark was seeded by
    ``prepare_spend_cutover`` has one, and a tenant without one is new, so an
    unbounded start could only ever reach back into spend some other meter already
    charged.
    """
    mark = _parse_iso(doc.last_spend_ingest_ts)
    if mark is not None:
        return mark - _SPEND_READ_OVERLAP, now
    created = _as_aware(doc.createdAt)
    return (created if created is not None else now - _SPEND_READ_OVERLAP), now


# How long a spend-ingest lease is held before anyone else may take it. Long enough
# to cover a slow sweep (the per-key read walks a tenant's whole history), short
# enough that a process killed mid-sweep does not wedge a tenant's billing for long.
_SPEND_LEASE_TTL = timedelta(seconds=60)


async def _acquire_spend_lease(workspace: str) -> bool:
    """Take the exclusive right to ingest ``workspace``'s spend. True if we got it.

    A single-document compare-and-swap: claim the row only if nobody holds it or the
    holder's lease has expired. The same idiom ``credits.debit`` uses for the wallet,
    and for the same reason — read-then-write cannot serialise two racers.

    WHY A LEASE AT ALL, when every debit is already idempotent per row.
    ``pending_spend_usd`` is the part the ledger cannot protect. Two ingests read the
    same remainder, each folds in a row the OTHER has not seen, and each crosses the
    credit line — so both debit, under different ``litellm:{request_id}`` keys, and
    the unique index never fires. Nothing errors and nothing looks wrong; the tenant
    is simply billed twice for one credit's worth of spend. The overlap is routine:
    the sweep runs on the API process heartbeat AND at worker boot, and the per-run
    trigger makes it happen on every run.

    Mongo rather than an ``asyncio.Lock`` because the racers are in different
    PROCESSES — runs execute in the arq worker, the sweep loop in the API.
    """
    now = datetime.now(UTC)
    coll = LiteLLMTenantKey.get_pymongo_collection()
    updated = await coll.find_one_and_update(
        {
            "workspace": workspace,
            "$or": [
                {"spend_ingest_lease_until": None},
                {"spend_ingest_lease_until": {"$exists": False}},
                {"spend_ingest_lease_until": {"$lt": now}},
            ],
        },
        {"$set": {"spend_ingest_lease_until": now + _SPEND_LEASE_TTL}},
    )
    return updated is not None


async def _release_spend_lease(workspace: str) -> None:
    """Hand the lease back. Best effort — the expiry is the real guarantee.

    Released in a ``finally`` so a failed sweep does not hold a tenant for the full
    TTL, but a crash that skips this is survivable by design: the next caller past
    the expiry takes the lease anyway.
    """
    try:
        coll = LiteLLMTenantKey.get_pymongo_collection()
        await coll.update_one(
            {"workspace": workspace}, {"$set": {"spend_ingest_lease_until": None}}
        )
    except Exception:  # noqa: BLE001 — never fail a completed sweep on cleanup
        logger.debug(
            "llm_provisioning: could not release the spend lease for workspace=%s "
            "— it expires on its own",
            workspace,
        )


async def _spend_bookkeeping_row(workspace: str) -> LiteLLMTenantKey:
    """This tenant's spend row, created keyless if provisioning never ran.

    The row's job here is to hold ``last_spend_ingest_ts``. That mark is what
    bounds the customer-scoped read and lets it advance, and a workspace with
    nowhere to store it cannot be swept twice without re-reading its whole
    history.

    Creating one for an UNPROVISIONED workspace is the fix for a hole that made
    real spend unbillable. Chat authenticates with the deployment key and names
    its workspace in the request body, so a workspace can spend for its entire
    life without ever minting a virtual key. ``ingest_tenant_spend`` used to
    return a zero result for exactly those workspaces, and the sweep only ever
    passed it workspaces that HAD a key, so their spend was read by nobody while
    the coverage check reported it as untagged. Both readings were wrong in the
    same direction: the spend was tagged, and free.

    The new row carries no key, so ``list_provisioned_workspaces`` still excludes
    it and ``ensure_tenant_key`` still mints on the same row later (it upserts on
    ``workspace``). Its ``createdAt`` becomes the read window's start, which means
    the first sweep after discovery bills forward from now and never reaches back
    into spend some other meter already charged — the same guarantee
    ``prepare_spend_cutover`` gives a provisioned tenant.
    """
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    if doc is not None:
        return doc

    doc = LiteLLMTenantKey(workspace=workspace, litellm_key=None)
    try:
        await doc.insert()
    except Exception:
        # The UNIQUE index on ``workspace`` is the arbiter: a concurrent sweep or
        # a mint that landed between the read and this insert wins, and we re-read
        # rather than raise. Losing this race is not an error — both racers wanted
        # the same row to exist.
        logger.debug(
            "llm_provisioning: lost the create race for the spend row of workspace=%s "
            "— re-reading the winner",
            workspace,
        )
        existing = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
        if existing is None:
            raise
        return existing
    return doc


async def _read_tenant_spend_rows(
    workspace: str,
    doc: LiteLLMTenantKey,
    client: LiteLLMAdminClient,
) -> list[tuple[dict[str, Any], bool]]:
    """Every spend row that could belong to ``workspace``, from both reads.

    Returns ``(row, honour_high_water_mark)`` pairs. The flag is True only for
    rows from the per-KEY read, which is unbounded and needs the mark to stop it
    re-walking the tenant's whole history; the customer-scoped rows carry their
    own window and must not be filtered again (see the loop's note).

    Merged on ``request_id``, key-read rows yielding to customer-read ones — the
    two overlap wherever a caller both sends the tenant key and tags ``user``,
    which is what Studio and the media server do. A row with no ``request_id``
    cannot be de-duplicated OR billed, so it is passed through and rejected once,
    loudly, in the loop.

    A failure of the customer read does not sink the key read, or vice versa. One
    of them returning nothing is normal; both being skipped because the other
    raised would turn a partial outage into a total one, and the sweep retries.
    """
    customer_rows: list[dict[str, Any]] = []
    since, until = _customer_read_window(doc, now=datetime.now(UTC))
    start_date, end_date = _proxy_window(since, until)
    try:
        customer_rows = await client.spend_logs_by_end_user(
            end_user=workspace, start_date=start_date, end_date=end_date
        )
    except Exception:
        logger.exception(
            "llm_provisioning: customer-scoped spend read failed for workspace=%s over "
            "[%s,%s] — falling back to the per-key read alone this sweep, which does "
            "NOT see chat. A 404 here means the proxy predates /spend/logs/v2; every "
            "other error is worth reading, because this is the read that bills chat",
            workspace,
            start_date,
            end_date,
        )

    key_rows: list[dict[str, Any]] = []
    if doc.litellm_key:
        try:
            key_rows = await client.spend_logs(api_key=doc.litellm_key)
        except Exception:
            logger.exception(
                "llm_provisioning: per-key spend read failed for workspace=%s — "
                "continuing with the customer-scoped rows alone this sweep",
                workspace,
            )

    merged: list[tuple[dict[str, Any], bool]] = [(row, False) for row in customer_rows]
    seen = {rid for row in customer_rows if (rid := _row_id(row)) is not None}
    for row in key_rows:
        rid = _row_id(row)
        if rid is not None and rid in seen:
            continue
        merged.append((row, True))

    logger.debug(
        "llm_provisioning: workspace=%s spend rows — %d by customer over [%s,%s], "
        "%d additional by key",
        workspace,
        len(customer_rows),
        start_date,
        end_date,
        len(merged) - len(customer_rows),
    )
    return merged


async def ingest_tenant_spend(
    workspace: str,
    *,
    spend_card: SpendCredits | None = None,
    admin_client: LiteLLMAdminClient | None = None,
) -> SpendIngestResult:
    """Bill ``workspace``'s proxy spend, under an exclusive per-tenant lease.

    Thin wrapper over ``_ingest_tenant_spend_locked``, which holds the real logic
    and its documentation. Everything here is the lease.

    A tenant is ingested by one caller at a time because the sub-credit remainder is
    a read-modify-write and the ledger's per-row idempotency key cannot protect it —
    see ``_acquire_spend_lease``. A caller that cannot get the lease returns an empty
    result with ``lease_skipped`` set rather than waiting: whoever holds it is
    already reading the same rows, and the next sweep is at most five minutes out.
    Skipping is therefore never a lost bill, only a later one.
    """
    _require_workspace(workspace)
    # The lease is a CAS on this row, so the row has to exist before we can claim it.
    await _spend_bookkeeping_row(workspace)

    if not await _acquire_spend_lease(workspace):
        logger.debug(
            "llm_provisioning.ingest_tenant_spend: workspace=%s is already being "
            "ingested — skipping this pass",
            workspace,
        )
        return SpendIngestResult(
            workspace_id=workspace,
            rows_read=0,
            rows_billed=0,
            credits_debited=0,
            cost_usd=0.0,
            cached_tokens=0,
            balance_after=await credits_service.balance(workspace),
            lease_skipped=True,
        )

    try:
        return await _ingest_tenant_spend_locked(
            workspace, spend_card=spend_card, admin_client=admin_client
        )
    finally:
        await _release_spend_lease(workspace)


async def _ingest_tenant_spend_locked(
    workspace: str,
    *,
    spend_card: SpendCredits | None = None,
    admin_client: LiteLLMAdminClient | None = None,
) -> SpendIngestResult:
    """Read ``workspace``'s LiteLLM proxy spend and debit it to the EXISTING credit
    ledger, exactly once per spend row.

    Reads the tenant's spend TWICE and merges it: once by customer
    (``GET /spend/logs/v2?end_user=<workspace>``, which is the only read that sees
    a chat run, because chat authenticates with the deployment key) and once by
    virtual key (``GET /spend/logs?api_key=<tenant key>``, which predates it and
    stays as a safety net). Rows are merged on ``request_id``; the key-read rows are
    the ones still bounded by the stored ``last_spend_ingest_ts`` high-water mark.
    Then it converts each row's USD ``spend`` to integer credits via the rate card,
    and debits the wallet with
    ``credits.service.debit(..., cause="litellm_spend",
    idempotency_key="litellm:{request_id}", allow_negative=True)``. BC-1's unique
    ``(workspace, idempotency_key)`` index makes a re-ingested row a ledger no-op
    (the real exactly-once guard); the high-water mark merely bounds the read.

    Advances ``last_spend_ingest_ts`` to the newest row's ``startTime`` after the
    sweep. Returns a ``SpendIngestResult`` (rows read / billed, credits + USD +
    cached-token totals, resulting balance).

    NOTE: this is the GATED half (see the module header's double-bill boundary).
    The caller decides whether to run it — this function does NOT check the flag,
    so it stays reusable by an explicit admin trigger even when the periodic sweep
    is off. A workspace with no provisioned key is billed the same way as one with
    a key: only the per-KEY half of the read is skipped, because the customer-
    scoped half needs no key. It used to return a zero result instead, which is
    how workspaces that spend on chat alone were served for free.
    """
    _require_workspace(workspace)

    doc = await _spend_bookkeeping_row(workspace)

    card = spend_card if spend_card is not None else load_spend_credits()
    client = admin_client if admin_client is not None else LiteLLMAdminClient()

    rows = await _read_tenant_spend_rows(workspace, doc, client)

    high_water = doc.last_spend_ingest_ts
    # Compare PARSED instants, not the raw strings. LiteLLM emits naive,
    # Z-suffixed and offset-bearing ``startTime`` shapes interchangeably, and
    # lexicographic ordering across those is wrong in both directions: a naive
    # "2026-09-02T14:00:00" sorts BELOW an aware "2026-09-02T14:00:00+00:00"
    # despite being the same instant, so a mark written in one shape silently
    # re-reads or silently skips rows written in the other. Every other timestamp
    # in this module already goes through ``_parse_iso``; this one did not, and a
    # seeded cutover mark makes that latent bug load-bearing.
    high_water_dt = _parse_iso(high_water)
    newest_ts = high_water
    newest_dt = high_water_dt

    rows_read = 0
    rows_billed = 0
    credits_debited = 0
    cost_usd_total = 0.0
    cached_total = 0
    # Spend carried from earlier sweeps that was not yet worth a whole credit. It
    # is real money the tenant owes; it just could not be expressed in the ledger's
    # integer unit yet.
    pending_usd = float(doc.pending_spend_usd or 0.0)
    pending_at_start = pending_usd

    # Oldest first so the high-water mark advances monotonically.
    for row, honour_mark in sorted(rows, key=lambda pair: _row_start_time(pair[0]) or ""):
        start_ts = _row_start_time(row)
        # WU-F boundary fix — skip rows STRICTLY older than the high-water mark
        # only. The previous ``start_ts <= high_water`` dropped a DISTINCT row that
        # shared the mark's exact ``startTime`` second when it first appeared on a
        # LATER sweep (proxy spend rows are not strictly ordered, and second-grained
        # timestamps collide), silently UNDER-BILLING it. Now a same-second row at
        # the boundary is RE-EXAMINED and de-duplicated by its own
        # ``litellm:{request_id}`` ledger key below (the ``is_recorded`` skip + BC-1's
        # unique index), so an already-ingested boundary row no-ops while a new
        # boundary row bills exactly once. The mark stays an optimisation that bounds
        # the read; the per-row request_id is the real exactly-once guard.
        #
        # ``honour_mark`` is False for the customer-scoped rows, which arrive
        # already bounded by their own window. Applying the mark to those would
        # re-create the boundary bug this paragraph describes, in its worst form:
        # a row written late, with a ``startTime`` behind a mark that has already
        # advanced, would be skipped on this sweep AND on every sweep after it.
        # Nothing is lost by skipping the skip — the ledger key, not the mark, is
        # what makes a row bill exactly once.
        start_dt = _parse_iso(start_ts)
        if (
            honour_mark
            and high_water_dt is not None
            and start_dt is not None
            and start_dt < high_water_dt
        ):
            continue

        rows_read += 1
        advances = newest_dt is None or (start_dt is not None and start_dt > newest_dt)
        if start_ts is not None and advances:
            newest_ts = start_ts
            newest_dt = start_dt

        cost_usd = _num(row.get("spend"))
        cached_total += _cached_tokens(row)
        cost_usd_total += cost_usd

        rid = _row_id(row)
        if rid is None:
            # No stable id — we can't dedup it, so skip rather than risk a
            # double-debit on the next sweep.
            logger.warning(
                "llm_provisioning.ingest_tenant_spend: workspace=%s skipping a spend "
                "row with no request_id (cost_usd=%.6f)",
                workspace,
                cost_usd,
            )
            continue

        ledger_key = f"litellm:{rid}"
        # Skip a row already accounted for. This gate now guards the REMAINDER as
        # well as the debit, and that is why it moved above the conversion. A row
        # folded into ``pending_usd`` on an earlier sweep has a zero-value ledger
        # entry rather than a debit, and the customer read deliberately re-offers
        # the last ``_SPEND_READ_OVERLAP`` of settled rows every tick — so without
        # this check first, a cheap row would be added to the remainder once per
        # sweep for fifteen minutes and the tenant would be billed several times
        # over for it. Under-billing became over-billing, which is worse.
        if await credits_service.is_recorded(workspace, ledger_key):
            continue

        # Carry the fraction instead of discarding it. Credits are integers (1 ==
        # $0.01) and the proxy prices ONE API call, so a single row is routinely
        # worth less than a whole credit. Converting each row on its own and
        # dropping anything that rounded to zero served every cheap call for free,
        # permanently: nothing accumulated, and the high-water mark moved past the
        # dropped row in the same pass. Per-run metering had the same arithmetic and
        # never showed it, because it priced a whole run at once — the cutover kept
        # the conversion and made the unit ~100x smaller.
        pending_usd += cost_usd
        credits = card.whole_credits(pending_usd)

        if credits <= 0:
            # Not yet worth a credit. Record it so the overlap cannot re-fold it,
            # and leave the money in ``pending_usd`` for the row that tips it over.
            await credits_service.record_no_movement(
                workspace=workspace,
                cause=_LITELLM_SPEND_CAUSE,
                idempotency_key=ledger_key,
                ref={
                    "request_id": rid,
                    "cost_usd": cost_usd,
                    "model": row.get("model"),
                    "cached_tokens": _cached_tokens(row),
                    "source": "litellm_spend_log",
                    "pending_usd": pending_usd,
                },
            )
            continue

        balance_after = await credits_service.debit(
            workspace=workspace,
            amount=credits,
            cause=_LITELLM_SPEND_CAUSE,
            idempotency_key=ledger_key,
            ref={
                "request_id": rid,
                "cost_usd": cost_usd,
                "model": row.get("model"),
                "cached_tokens": _cached_tokens(row),
                "source": "litellm_spend_log",
                # What this debit actually settles: its own row plus every
                # sub-credit row folded in ahead of it. Without this the ledger
                # looks like a $0.0015 call was billed 3 credits.
                "settles_usd": card.usd_for_credits(credits),
            },
            allow_negative=True,
        )
        # Take the billed part back out; what is left is the unbilled fraction.
        pending_usd -= card.usd_for_credits(credits)
        credits_debited += credits
        rows_billed += 1
        logger.debug(
            "llm_provisioning.ingest_tenant_spend: workspace=%s billed %d credits "
            "(request_id=%s, cost_usd=%.6f) -> balance=%d",
            workspace,
            credits,
            rid,
            cost_usd,
            balance_after,
        )

    # Advance the high-water mark so a re-sweep doesn't re-read settled rows, and
    # persist the sub-credit remainder alongside it. Only write when something
    # actually moved (avoid a no-op save).
    #
    # The two MUST be saved together. The mark says "these rows are settled" and the
    # remainder says "and this much of them is still owed"; writing the mark without
    # the remainder is exactly the old bug, since the rows behind the mark are the
    # ones whose fractions are sitting in it.
    mark_moved = newest_ts is not None and newest_ts != doc.last_spend_ingest_ts
    pending_moved = pending_usd != pending_at_start
    if mark_moved or pending_moved:
        if newest_ts is not None:
            doc.last_spend_ingest_ts = newest_ts
        doc.pending_spend_usd = pending_usd
        doc.updatedAt = datetime.now(UTC)
        await doc.save()

    balance_after = await credits_service.balance(workspace)
    if rows_billed:
        logger.info(
            "llm_provisioning.ingest_tenant_spend: workspace=%s ingested %d/%d new "
            "spend rows -> %d credits (cost_usd=%.6f, cached_tokens=%d), balance=%d",
            workspace,
            rows_billed,
            rows_read,
            credits_debited,
            cost_usd_total,
            cached_total,
            balance_after,
        )
    return SpendIngestResult(
        workspace_id=workspace,
        rows_read=rows_read,
        rows_billed=rows_billed,
        credits_debited=credits_debited,
        cost_usd=cost_usd_total,
        cached_tokens=cached_total,
        balance_after=balance_after,
    )


# ---------------------------------------------------------------------------
# Shadow compare — read proxy spend + BC-3 ledger side by side, debit NOTHING.
# ---------------------------------------------------------------------------


def _as_aware(dt: datetime | None) -> datetime | None:
    """Normalise a datetime to tz-aware UTC (a naive value is assumed UTC), or
    pass None through. Used so window comparisons never mix naive + aware."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse a LiteLLM ``startTime`` ISO string to a datetime, or None.

    Tolerant of the proxy's shapes: a trailing ``Z`` is normalised to ``+00:00``;
    a naive (no-offset) string parses as naive. Returns None on anything
    unparseable so a malformed row is excluded from a window rather than crashing
    the compare.
    """
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # LiteLLM /spend/logs ``startTime`` is frequently NAIVE (no offset). The window
    # bounds the sweep passes are tz-AWARE (UTC), and Python refuses to compare a
    # naive datetime to an aware one. Normalise a naive timestamp to UTC (the proxy
    # records UTC) so the window test never crashes on a real proxy row.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _in_window(row_dt: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    """Half-open window test ``[since, until)`` on a row's parsed timestamp.

    A row with NO parseable timestamp is INCLUDED (we'd rather over-count an
    undated row into the compare than silently drop spend from the shadow check —
    the compare is a safety net, so it errs toward surfacing discrepancies). An
    open bound (``None``) passes that side. ``since`` / ``until`` are normalised to
    tz-aware UTC alongside the row timestamp so a caller passing naive bounds (or a
    proxy emitting naive timestamps) can't trip a naive-vs-aware comparison.
    """
    if row_dt is None:
        return True
    since = _as_aware(since)
    until = _as_aware(until)
    row_dt = _as_aware(row_dt)
    if since is not None and row_dt < since:
        return False
    return not (until is not None and row_dt >= until)


async def reconcile_tenant_spend(
    workspace: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    spend_card: SpendCredits | None = None,
    threshold: int | None = None,
    admin_client: LiteLLMAdminClient | None = None,
) -> SpendReconciliation:
    """SHADOW compare for ``workspace`` over ``[since, until)`` — debit NOTHING.

    The safe cutover step. Reads the tenant's LiteLLM proxy spend, converts it to
    credits via the SAME rate card BC-3 uses, sums the workspace's BC-3
    ``compute_spend`` ledger debits over the same window, and records the two side
    by side as a ``SpendReconciliation`` doc (and a structured log line). The
    ``delta`` (litellm - bc3) and the ``coverage_gap`` verdict (``abs(delta)`` over
    ``threshold``) tell an operator whether the two meters agree before flipping to
    ``live``.

    CRITICAL — this performs ZERO debits. It NEVER calls ``credits.service.debit``
    and NEVER advances the spend high-water mark. BC-3 keeps billing untouched
    during shadow. The credit ledger is read (via the credits service's own
    tenant-filtered ``sum_debits_by_cause``) but never written. A workspace with no
    provisioned key still produces a record (litellm_credits=0) so the operator
    sees every tenant in the compare, not a silent gap.

    ``since`` / ``until`` bound the window (datetimes; ``until`` exclusive). The
    LiteLLM rows are filtered on their parsed ``startTime``; the BC-3 debits on
    ``createdAt`` — the same window on both sides. ``spend_card`` / ``threshold``
    are injectable for tests; they default to the settings-derived values.
    """
    _require_workspace(workspace)

    card = spend_card if spend_card is not None else load_spend_credits()
    gap_threshold = threshold if threshold is not None else reconcile_gap_threshold()

    # --- LiteLLM side: proxy spend over the window -> credits. ---------------
    litellm_credits = 0
    litellm_rows = 0
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    if doc is not None and doc.litellm_key:
        client = admin_client if admin_client is not None else LiteLLMAdminClient()
        rows = await client.spend_logs(api_key=doc.litellm_key)
        # Sum the USD and convert ONCE. Converting per row and adding the results
        # up rounds each row separately, so every call worth less than half a credit
        # contributes nothing — and this is the compare an operator reads to decide
        # whether LiteLLM can be trusted as the only meter. It understated the
        # LiteLLM side against BC-3's per-run figure by exactly the rows the live
        # ingest was also dropping, which made the cutover look safer than it was.
        litellm_usd = 0.0
        for row in rows:
            row_dt = _parse_iso(_row_start_time(row))
            if not _in_window(row_dt, since, until):
                continue
            litellm_rows += 1
            litellm_usd += _num(row.get("spend"))
        litellm_credits = card.to_credits(litellm_usd)

    # --- BC-3 side: the metered compute_spend debits over the SAME window. ---
    # Read through the credits service (entity-isolation: it owns its ledger doc).
    # This is a READ — no debit, no write.
    bc3_credits, bc3_entries = await credits_service.sum_debits_by_cause(
        workspace, _BC3_COMPUTE_SPEND_CAUSE, since=since, until=until
    )

    delta = litellm_credits - bc3_credits
    coverage_gap = abs(delta) > gap_threshold

    window_start = since.isoformat() if since is not None else None
    window_end = until.isoformat() if until is not None else None

    # Persist the reconciliation row (append-only audit — NOT a ledger; recording
    # one moves no money). One row per tenant per window.
    record = SpendReconciliationDoc(
        workspace=workspace,
        window_start=window_start,
        window_end=window_end,
        litellm_credits=litellm_credits,
        bc3_credits=bc3_credits,
        delta=delta,
        coverage_gap=coverage_gap,
        threshold=gap_threshold,
        litellm_rows=litellm_rows,
        bc3_entries=bc3_entries,
    )
    await record.insert()

    log = logger.warning if coverage_gap else logger.info
    log(
        "llm_provisioning.reconcile_tenant_spend: workspace=%s window=[%s,%s) "
        "litellm=%d bc3=%d delta=%d coverage_gap=%s (threshold=%d, litellm_rows=%d, "
        "bc3_entries=%d) — SHADOW, no debit",
        workspace,
        window_start,
        window_end,
        litellm_credits,
        bc3_credits,
        delta,
        coverage_gap,
        gap_threshold,
        litellm_rows,
        bc3_entries,
    )

    return SpendReconciliation(
        workspace_id=workspace,
        window_start=window_start,
        window_end=window_end,
        litellm_credits=litellm_credits,
        bc3_credits=bc3_credits,
        delta=delta,
        coverage_gap=coverage_gap,
        threshold=gap_threshold,
        litellm_rows=litellm_rows,
        bc3_entries=bc3_entries,
    )


async def spend_attribution_coverage(
    workspaces: list[str],
    *,
    since: datetime,
    until: datetime,
    admin_client: LiteLLMAdminClient | None = None,
) -> SpendCoverage:
    """Count the window's spend rows that NO tenant claims. Debits nothing.

    Asks the proxy how many spend rows the window holds in total, then how many it
    holds per workspace, and reports the difference. Each count is one request that
    returns a single row and reads the reported ``total``, so the whole check costs
    ``len(workspaces) + 1`` small calls however much spend the window contains.

    This exists because of how the bug it watches for presented. Chat spend was
    attributed to no tenant for the entire time ``live`` was on; every per-tenant
    read succeeded, every sweep logged a healthy ``3/3 tenants``, and the only
    visible symptom was a number nobody had a reason to disbelieve: zero credits.
    A remainder here is the one signal that distinguishes "nobody spent anything"
    from "we are not looking where the spending is".

    Never raises. A failed count sets ``degraded`` and leaves the remainder
    untrustworthy rather than taking the sweep down with it — the sweep's job is to
    bill, and this is an observation of the sweep, not part of it.
    """
    client = admin_client if admin_client is not None else LiteLLMAdminClient()
    start_date, end_date = _proxy_window(since, until)

    degraded = False
    total_rows = 0
    try:
        total_rows = await client.spend_log_count(start_date=start_date, end_date=end_date)
    except Exception:
        degraded = True
        logger.exception(
            "llm_provisioning.spend_attribution_coverage: could not read the window "
            "total over [%s,%s] — coverage is unknown this tick, NOT clean",
            start_date,
            end_date,
        )

    attributed_rows = 0
    for workspace in workspaces:
        try:
            attributed_rows += await client.spend_log_count(
                start_date=start_date, end_date=end_date, end_user=workspace
            )
        except Exception:
            degraded = True
            logger.exception(
                "llm_provisioning.spend_attribution_coverage: could not count rows for "
                "workspace=%s — coverage is unknown this tick, NOT clean",
                workspace,
            )

    # Clamped at zero. The two counts are separate queries against a table that is
    # still being written, so a row landing between them can make the per-tenant sum
    # exceed the total. A negative remainder is a race, not a surplus, and reporting
    # one would train an operator to ignore the number.
    unattributed = max(0, total_rows - attributed_rows)

    # Split the remainder. Rows that name a workspace nobody swept are a different
    # bug from rows that name nobody, and until they were separated the log line
    # blamed the wrong one: it advertised "no ``user`` field" while a third of the
    # window was tagged and merely unswept. Asking the proxy for its customer list
    # is what makes the distinction possible at all — a customer id is by
    # definition one some request DID carry.
    unswept_rows = 0
    unswept: list[str] = []
    swept = set(workspaces)
    try:
        customers = await client.list_customers()
    except Exception:
        degraded = True
        customers = []
        logger.exception(
            "llm_provisioning.spend_attribution_coverage: could not list the proxy's "
            "customers — the remainder cannot be split this tick, so all %d of it "
            "reads as untagged whether or not it is",
            unattributed,
        )
    for customer in customers:
        if customer in swept:
            continue
        try:
            rows = await client.spend_log_count(
                start_date=start_date, end_date=end_date, end_user=customer
            )
        except Exception:
            degraded = True
            logger.exception(
                "llm_provisioning.spend_attribution_coverage: could not count rows for "
                "unswept customer=%s — coverage is unknown this tick, NOT clean",
                customer,
            )
            continue
        if rows:
            unswept_rows += rows
            unswept.append(customer)

    # Bounded by the remainder for the same reason it is clamped at zero: the two
    # sides are separate queries, and a split that claimed more unswept rows than
    # there are unattributed ones would be reporting a race as a finding.
    unswept_rows = min(unswept_rows, unattributed)

    # Price and classify the untagged half, but only when there IS one. The counts
    # above are O(1) in spend volume and run every tick; this reads rows, so it is
    # gated on there being something to explain.
    #
    # It exists because the bare count cries wolf. A LiteLLM proxy logs its own
    # traffic next to ours — an operator trying a model in the dashboard, the
    # periodic model health check — and none of it can ever name a workspace or be
    # billed to one. Measured on the production proxy 2026-09-03: all 8 "served and
    # not billed" rows were exactly that, worth $0.00014545 between them, while the
    # runbook told the operator to treat any non-zero count as blocking. The number
    # was right and the conclusion it invited was wrong.
    internal_rows = 0
    untagged_rows = 0
    unattributed_usd = 0.0
    untagged_usd = 0.0
    classified = False
    if unattributed - unswept_rows > 0:
        try:
            window_rows, complete = await client.spend_logs_window(
                start_date=start_date, end_date=end_date
            )
        except Exception:
            logger.exception(
                "llm_provisioning.spend_attribution_coverage: could not read the "
                "window's rows to classify %d unattributed row(s) — reporting the "
                "count alone, which cannot tell the proxy's own traffic from a real "
                "billing hole",
                unattributed,
            )
        else:
            classified = complete
            for row in window_rows:
                if (row.get("end_user") or "").strip():
                    continue
                cost = _num(row.get("spend"))
                unattributed_usd += cost
                if (row.get("team_id") or "") in _PROXY_INTERNAL_TEAMS:
                    internal_rows += 1
                else:
                    untagged_rows += 1
                    untagged_usd += cost

    coverage = SpendCoverage(
        window_start=start_date,
        window_end=end_date,
        total_rows=total_rows,
        attributed_rows=attributed_rows,
        unattributed_rows=unattributed,
        unswept_rows=unswept_rows,
        unswept_workspaces=tuple(unswept),
        workspaces_checked=len(workspaces),
        degraded=degraded,
        internal_rows=internal_rows,
        untagged_rows=untagged_rows,
        unattributed_usd=unattributed_usd,
        untagged_usd=untagged_usd,
        classified=classified,
    )

    if degraded:
        logger.warning(
            "llm_provisioning.spend_attribution_coverage: [%s,%s] INCOMPLETE — "
            "%d/%d rows attributed across %d workspace(s); at least one count failed, "
            "so the %d unattributed is a floor, not a finding",
            start_date,
            end_date,
            attributed_rows,
            total_rows,
            len(workspaces),
            unattributed,
        )
    elif unswept_rows or untagged_rows or (unattributed and not classified):
        # Loud only for the halves that are actually ours. The proxy's own dashboard
        # and health-check rows are reported, because an operator who sees a
        # remainder wants to know where it went, but they do not raise the alarm.
        logger.warning(
            "llm_provisioning.spend_attribution_coverage: [%s,%s] %d of %d proxy spend "
            "row(s) claimed by no tenant ($%.6f) — %d name a workspace the sweep did "
            "not visit (%s), %d are a real caller that named nobody ($%.6f), %d are the "
            "proxy's own dashboard / health-check traffic and are nobody's to bill. A "
            "tagged-but-unswept row is OUR bug: the request said who pays and the sweep "
            "did not ask. An untagged row is a caller reaching the proxy without a "
            "``user`` field%s",
            start_date,
            end_date,
            unattributed,
            total_rows,
            unattributed_usd,
            unswept_rows,
            ", ".join(unswept) or "none",
            untagged_rows,
            untagged_usd,
            internal_rows,
            "" if classified else " (the split is from a TRUNCATED read — treat it as a sample)",
        )
    elif unattributed:
        logger.info(
            "llm_provisioning.spend_attribution_coverage: [%s,%s] %d of %d proxy spend "
            "row(s) claimed by no tenant ($%.6f), and every one is the proxy's own "
            "dashboard / health-check traffic — nothing of ours is unbilled",
            start_date,
            end_date,
            unattributed,
            total_rows,
            unattributed_usd,
        )
    else:
        logger.info(
            "llm_provisioning.spend_attribution_coverage: [%s,%s] all %d proxy spend "
            "row(s) attributed across %d workspace(s)",
            start_date,
            end_date,
            total_rows,
            len(workspaces),
        )

    return coverage


__all__ = [
    "ensure_tenant_key",
    "get_tenant_key",
    "ingest_tenant_spend",
    "list_provisioned_workspaces",
    "list_sweepable_workspaces",
    "load_key_budget",
    "load_spend_credits",
    "prepare_spend_cutover",
    "reconcile_gap_threshold",
    "reconcile_tenant_spend",
    "spend_attribution_coverage",
    "spend_ingest_enabled",
    "spend_mode",
    "warn_legacy_spend_bool_once",
]
