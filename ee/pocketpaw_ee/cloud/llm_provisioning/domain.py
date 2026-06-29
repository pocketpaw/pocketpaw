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
