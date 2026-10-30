# ee/pocketpaw_ee/cloud/models/spend_reconciliation.py — the per-tenant
# shadow-compare reconciliation record (WU-F billing cutover).
#
# One Beanie document per tenant per shadow sweep window. The shadow mode of the
# billing cutover (POCKETPAW_LITELLM_SPEND_MODE=shadow) reads a tenant's LiteLLM
# proxy spend, converts it to credits, sums the BC-3 ``compute_spend`` ledger
# debits over the SAME window, and records the two side by side plus their delta
# and a ``coverage_gap`` flag. It is the audit trail that lets an operator confirm
# the two meters AGREE before cutting over to LiteLLM as the single meter (live
# mode). Writing this row performs ZERO debits — shadow only reads + compares.
#
#   * ``SpendReconciliation`` — ``{workspace, window_start, window_end,
#     litellm_credits, bc3_credits, delta, coverage_gap, threshold}``. ``delta`` is
#     ``litellm_credits - bc3_credits`` (positive ⇒ the proxy saw MORE spend than
#     BC-3 billed — traffic likely bypassing per-run metering, or a conversion
#     mismatch; negative ⇒ BC-3 billed more than the proxy attributed).
#     ``coverage_gap`` is True when ``abs(delta)`` exceeds ``threshold`` credits.
#
# WHY a separate doc (not a field on LiteLLMTenantKey / the credit ledger): RFC 03
# keeps domain-specific records in domain-owned docs, and this is an append-only
# audit series (one row per window per tenant), not per-key state. Only
# ``ee.cloud.llm_provisioning.service`` writes it — the same entity-isolation
# boundary the credit / litellm-key docs use. It is NOT a ledger: it never moves
# money, so it carries no idempotency key and no ``applied`` flag.
#
# Created 2026-06-26 (feat/litellm-billing-cutover, WU-F): new entity. Registered
# in ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``spend_reconciliations`` collection.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SpendReconciliation(TimestampedDocument):
    """One shadow-compare row: LiteLLM proxy spend vs BC-3 metering for a tenant
    over a window.

    Append-only audit, NOT a ledger — recording one performs no debit. The shadow
    sweep writes one per provisioned tenant per window; an operator (or a future
    dashboard) reads them to confirm the two meters agree before flipping to live.

    ``litellm_credits`` is the proxy spend over ``[window_start, window_end)``
    converted to integer credits via the SAME rate card BC-3 uses; ``bc3_credits``
    is the sum of the workspace's ``compute_spend`` ledger debits whose timestamps
    fall in that window. ``delta = litellm_credits - bc3_credits``. ``coverage_gap``
    is True when ``abs(delta) > threshold`` — a likely proxy-bypass or conversion
    mismatch that must be resolved before live cutover.
    """

    # The tenant this row reconciles. Indexed (NOT unique — many windows per
    # workspace) so an operator query for one workspace's history is cheap.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The compare window, as ISO ``startTime`` strings (the same field shape the
    # LiteLLM /spend/logs rows carry, so window math compares like-for-like).
    # Half-open ``[window_start, window_end)``.
    window_start: str | None = None
    window_end: str | None = None
    # The two meters, in integer credits.
    litellm_credits: int = 0
    bc3_credits: int = 0
    # ``litellm_credits - bc3_credits``. Positive ⇒ proxy saw more than BC-3 billed.
    delta: int = 0
    # True when ``abs(delta)`` exceeds ``threshold`` — a coverage gap to resolve
    # before cutting over.
    coverage_gap: bool = False
    # The credit threshold the gap was judged against (recorded for provenance so a
    # later threshold change doesn't retro-reinterpret old rows).
    threshold: int = 0
    # How many proxy spend rows + ledger debits fed this compare (provenance for an
    # operator triaging a flagged gap).
    litellm_rows: int = 0
    bc3_entries: int = 0

    class Settings:
        name = "spend_reconciliations"
