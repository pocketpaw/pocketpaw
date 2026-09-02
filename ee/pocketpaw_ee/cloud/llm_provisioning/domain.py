# ee/pocketpaw_ee/cloud/llm_provisioning/domain.py — frozen value objects + the
# provisioning rate card for the LLM-provisioning entity (MCG-8).
#
# Framework-free shapes the service hands back across the entity boundary (never
# the Beanie ``LiteLLMTenantKey`` doc, never raw proxy JSON):
#
#   * ``KeyBudget``       — the budget / rate-limit / allowlist a virtual key is
#                           provisioned with, resolved from runtime settings. The
#                           declarative knobs the proxy enforces per tenant.
#   * ``ProvisionResult`` — the outcome of ``ensure_tenant_key``: the workspace,
#   * ``CutoverPreparation`` — the outcome of ``prepare_spend_cutover``: how many
#                           tenants are provisioned, how many were stamped with the
#                           billing seam, and how many already had a mark.
#                           its virtual key, and ``created`` (True only on a real
#                           first mint, False on the idempotent already-exists
#                           path) so callers can gate a one-time side effect on it.
#   * ``SpendIngestResult`` — the outcome of one spend sweep for a tenant: how many
#                           proxy spend rows were read, how many credits were
#                           debited, the USD total + cached-token savings, and the
#                           resulting wallet balance.
#   * ``SpendReconciliation`` — the outcome of one SHADOW compare for a tenant (WU-F):
#                           the litellm-vs-BC-3 credits over a window, their delta,
#                           and the coverage-gap verdict. Carries NO debit — shadow
#                           only reads + compares. The Beanie persistence twin is
#                           ``models.spend_reconciliation.SpendReconciliation``.
#   * ``SpendCoverage``   — the outcome of one attribution-coverage check: how many
#                           proxy spend rows a window holds versus how many any
#                           tenant claims. The difference is spend nobody is billed
#                           for, which is the failure this whole seam presents as.
#   * ``SpendCredits``    — the per-tenant spend rate card: USD-cost markup + the
#                           per-credit USD denomination. Mirrors metering's
#                           ``RateCard`` shape deliberately so proxy-spend and
#                           per-run metering convert USD->credits identically.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): new entity.
# Updated 2026-06-26 (feat/litellm-billing-cutover, WU-F): added
# ``SpendReconciliation`` — the value object returned by the shadow-mode compare
# (``service.reconcile_tenant_spend``). Distinct from the Beanie doc of the same
# concept; this is the framework-free shape the sweep logs + the doc is built from.
# Updated 2026-09-02 (fix/bill-workspaces-the-sweep-cannot-see): ``SpendCoverage``
# now splits its remainder into rows that name an unswept workspace and rows that
# name none, and carries the unswept ids. One number could not tell an operator
# which of two unrelated bugs they had.
# Updated 2026-09-02 (feat/proxy-spend-ingest-by-customer): added ``SpendCoverage``
# — what ``service.spend_attribution_coverage`` returns. It exists because the bug
# that motivated this branch was invisible: chat spend was attributed to nobody, the
# per-tenant reads all succeeded, and the sweep logged a confident
# ``3/3 tenants -> 0 credits``. A count of rows no tenant claims is the one number
# that would have said so on the first tick.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class KeyBudget:
    """The budget / limits a per-tenant virtual key is minted with.

    All optional: ``None`` means "let the proxy apply no cap" for that knob. The
    service resolves these from runtime settings (POCKETPAW_TENANT_* ) with sane
    defaults, so budgets are config-driven, never hardcoded secrets. ``models`` is
    the allowed-model allowlist (empty == all models the proxy serves).
    """

    max_budget_usd: float | None = None
    budget_duration: str | None = None  # LiteLLM duration string, e.g. "30d"
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    models: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProvisionResult:
    """The outcome of ``ensure_tenant_key``.

    ``litellm_key`` is the tenant's virtual key (minted now, or the one already on
    file). ``created`` is True ONLY when this call newly minted the key on the
    proxy; False on the idempotent already-provisioned path (no proxy call was
    made). Callers gate a one-time side effect (e.g. an audit emit) on ``created``.
    """

    workspace_id: str
    litellm_key: str
    created: bool


@dataclass(frozen=True)
class SpendCredits:
    """The per-tenant proxy-spend rate card (USD -> integer credits).

    Deliberately the SAME conversion as metering's ``RateCard`` so a dollar of
    proxy spend bills the same number of credits whether it is attributed via the
    per-run meter (BC-3) or this proxy-spend sweep (MCG-8): a single price for
    compute. ``markup`` is the flat multiplier; ``credit_usd`` is the USD value of
    one credit (1 credit == $0.01).
    """

    markup: float
    credit_usd: float

    def to_credits(self, cost_usd: float) -> int:
        """Convert a USD spend amount into integer credits.

        ``round(cost_usd * markup / credit_usd)`` — identical to
        ``metering.domain.RateCard.to_credits``. A non-positive cost yields 0
        credits (no debit).
        """
        if cost_usd <= 0:
            return 0
        return round(cost_usd * self.markup / self.credit_usd)


@dataclass(frozen=True)
class SpendIngestResult:
    """The outcome of one spend sweep for a tenant.

    ``rows_read`` is how many /spend/logs rows the sweep examined; ``rows_billed``
    how many produced a NEW ledger debit (a row already ingested — same
    idempotency key — is counted in ``rows_read`` but not ``rows_billed``).
    ``credits_debited`` is the total integer credits charged this sweep;
    ``cost_usd`` the summed USD; ``cached_tokens`` the summed cached-input tokens
    the proxy reported (the prompt-cache savings signal). ``balance_after`` is the
    wallet balance once the sweep's debits landed.
    """

    workspace_id: str
    rows_read: int
    rows_billed: int
    credits_debited: int
    cost_usd: float
    cached_tokens: int
    balance_after: int


@dataclass(frozen=True)
class CutoverPreparation:
    """The outcome of stamping the billing-cutover mark on every tenant.

    ``provisioned`` is how many tenants have a live proxy key at all — the only
    ones ``live`` mode bills, which is why a workspace with no key must be
    provisioned BEFORE the flip or its usage becomes free. ``seeded`` is how many
    had no high-water mark and got one; ``already_marked`` how many were left
    alone because ingestion had already begun for them (overwriting a live mark
    would skip real spend). ``cutover_at`` is the ISO instant stamped, and it is
    the seam: BC-3 owns every run before it, LiteLLM every proxy row after.
    """

    cutover_at: str
    provisioned: int
    seeded: int
    already_marked: int
    dry_run: bool


@dataclass(frozen=True)
class SpendCoverage:
    """How much of a window's proxy spend any tenant actually claims.

    ``total_rows`` is every spend row the proxy recorded in the window;
    ``attributed_rows`` is the sum of the per-tenant counts over the workspaces
    checked. ``unattributed_rows`` is the remainder, and it has two very different
    halves that this object now reports apart:

      * ``unswept_rows`` — rows that DO name a workspace, but one the sweep was
        not iterating. The request was tagged correctly and the bug is on our
        side of the wire.
      * the rest — rows carrying no workspace at all, which is the failure the
        request-tagging exists to prevent.

    They were one number until 2026-09-02, and conflating them cost real
    debugging time: the log line blamed missing ``user`` fields while a third of
    the window was tagged and simply unswept. A fix for one does nothing for the
    other, so an operator has to be able to tell which they are looking at.

    Any non-zero remainder is a billing hole, and reading it as a ROW count rather
    than a dollar amount is deliberate: a count needs one cheap request per tenant
    and answers the question that matters ("is anything falling through?") without
    pretending to be an invoice. The reconciliation compare is where amounts belong.

    ``degraded`` marks a check that could not complete — a proxy that failed one of
    the counts. The remainder is then unreliable and must not be read as a verdict,
    which is a distinction the log line has to keep: "no gap" and "could not tell"
    look identical in a bare zero.
    """

    window_start: str
    window_end: str
    total_rows: int
    attributed_rows: int
    unattributed_rows: int
    # The half of ``unattributed_rows`` that names a workspace the sweep skipped.
    # ``unswept_workspaces`` lists those ids so the log can name them; an operator
    # who can see the id can go and ask why it is not being swept.
    unswept_rows: int = 0
    unswept_workspaces: tuple[str, ...] = ()
    workspaces_checked: int = 0
    degraded: bool = False


@dataclass(frozen=True)
class SpendReconciliation:
    """The outcome of one SHADOW compare for a tenant (WU-F billing cutover).

    Shadow mode reads the tenant's LiteLLM proxy spend over a window, converts it
    to credits (the SAME rate card BC-3 uses), sums the workspace's BC-3
    ``compute_spend`` ledger debits over that window, and records the two side by
    side. This object is the framework-free result; it carries NO debit — the whole
    point of shadow is to compare WITHOUT touching the wallet.

    ``litellm_credits`` is the proxy spend in credits; ``bc3_credits`` is the summed
    BC-3 metered debits in credits. ``delta = litellm_credits - bc3_credits``
    (positive ⇒ the proxy saw MORE than BC-3 billed — traffic likely bypassing
    per-run metering, or a conversion mismatch; negative ⇒ BC-3 billed more than the
    proxy attributed). ``coverage_gap`` is True when ``abs(delta) > threshold`` — a
    discrepancy big enough to resolve BEFORE flipping to live. ``window_start`` /
    ``window_end`` bound the compare (ISO ``startTime`` strings, half-open).
    ``litellm_rows`` / ``bc3_entries`` are the input counts (provenance).
    """

    workspace_id: str
    window_start: str | None
    window_end: str | None
    litellm_credits: int
    bc3_credits: int
    delta: int
    coverage_gap: bool
    threshold: int
    litellm_rows: int
    bc3_entries: int
