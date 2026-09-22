# ee/pocketpaw_ee/cloud/models/platform_rollup.py — the PlatformDailyRollup
# document (chunk 8 of the Paw Admin PRD, the platform dashboard).
#
# One row per (day, workspace). Exists because CreditLedgerEntry's only indexes
# are workspace-prefixed (`workspace`, `(workspace, idempotency_key)`,
# `(workspace, createdAt)`) — a platform-wide `{createdAt: {$gte, $lt}}` query
# with no workspace term can use none of them, so "platform spend last month"
# is a full collection scan of the hot metering write path. This collection is
# the only way to ask that question; see
# docs/design/drafts/2026-09-15-paw-admin-screen-dashboard.md §3.1/3.2.
#
# Read side only in this PR: the model, its indexes, and the dashboard endpoint
# that reads it (ee/pocketpaw_ee/cloud/platform/stats.py). The nightly arq job
# and backfill script that POPULATE this collection are explicitly out of
# scope here — see the stats.py module docstring and the PR description for
# why, and cloud/platform/stats.py's handling of an empty collection (job never
# ran) as a first-class, tested state rather than an error.
#
# Only ee.cloud.platform.stats reads this doc for now. No import-linter
# contract restricts it (verified: no "Beanie writes only from service.py"
# contract exists for this entity in ee/pyproject.toml), so this is not an
# entity-isolation boundary the way Credit/Payment/Subscription are — there is
# no service module yet because nothing writes this collection in this PR.
#
# Created 2026-09-16 (feat/platform-stats-revenue) — chunk 8 of the Paw Admin PRD.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ModelSpendRollup(BaseModel):
    """One (model) slice of a day's platform-wide spend, embedded in the rollup row."""

    model: str
    spend_micro: int
    runs: int
    tokens: int


class PlatformDailyRollup(TimestampedDocument):
    """A platform-wide, per-tenant summary of one UTC day's credit spend.

    ``day`` is a ``YYYY-MM-DD`` UTC string — the same convention
    ``credits.service._spend_by_model_pipeline`` uses (``$dateToString`` with no
    ``timezone`` key), so a rollup and a live ledger read can never disagree
    about which calendar day an entry belongs to.

    ``spend_micro`` / the ``by_model[].spend_micro`` figures are MICRO-credits
    (``credits.domain.MICRO_PER_CREDIT`` = 1_000_000), per spine §6.10 — money
    stays in the fine unit end to end and converts once, at the display edge.
    ``granted_micro_by_cause`` is keyed by ledger cause (``top_up`` /
    ``subscription_grant`` / ``promo`` / ``genesis`` / ``operator_grant``), kept
    apart from spend so a revenue read can exclude operator grants without
    re-deriving the cause split.

    ``plan`` and ``deleted`` are the tenant's plan and soft-delete state ON THAT
    DAY, captured at rollup time rather than joined against today's ``Workspace``
    — so plan-mix history does not get silently rewritten every time a tenant
    upgrades. ``generated_at`` is when the job wrote the row, and is the
    dashboard's staleness signal (surfaced as ``as_of``).
    """

    day: str
    workspace: str
    spend_micro: int
    granted_micro_by_cause: dict[str, int] = Field(default_factory=dict)
    runs: int = 0
    tokens: int = 0
    by_model: list[ModelSpendRollup] = Field(default_factory=list)
    plan: str = "free"
    deleted: bool = False
    generated_at: datetime

    class Settings:
        name = "platform_daily_rollup"
        indexes = [
            # The global time-range query the dashboard runs: "every tenant's
            # rollup between start and end", with no workspace term.
            IndexModel([("day", 1)], name="ix_day"),
            # One row per (day, workspace) — a re-run or a backfill upserts
            # instead of double-counting.
            IndexModel(
                [("day", 1), ("workspace", 1)],
                unique=True,
                name="uq_day_workspace",
            ),
        ]
