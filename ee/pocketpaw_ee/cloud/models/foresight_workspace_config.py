# ee/pocketpaw_ee/cloud/models/foresight_workspace_config.py
# Updated: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0
# adds the ``insights_synthesizer`` field. Workspace admins can opt
# into the LLM-driven synthesizer alongside the v0.5 pattern rules; the
# default stays "pattern" (deterministic, free). The shape is otherwise
# unchanged — new field is optional with a safe default, so older docs
# stored before this PR continue to load and ``find_one`` returns
# ``insights_synthesizer = "pattern"`` for them.
# Created: 2026-05-26 (feat/foresight-v10-threshold-override-cloud) —
# RFC 08 v1.0. Per-workspace Foresight configuration. v1.0 ships with one
# overridable knob — the onboarding gate threshold — but the doc shape is
# designed so subsequent workspace-scoped foresight settings (default
# scenario sub_type, notification routing, default insight cap) layer on
# without churning the workspace ``Workspace`` document (RFC 03 keeps
# domain-specific config in domain-owned docs).
#
# One row per workspace; an upsert key on ``workspace`` makes the
# read/write paths idempotent (the service's ``set_threshold`` uses
# ``find_one_and_update`` semantics rather than insert-or-error).
#
# Only ``ee.cloud.foresight.service`` may import this module — enforced
# by the import-linter contract in ``ee/pyproject.toml`` (same contract
# that scopes ForesightRun / ForesightBacktest writes to service.py).

from __future__ import annotations

from typing import Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument

# Synthesizer choice values the workspace config accepts. ``pattern`` is
# the v0.5 deterministic rule synthesizer (default); ``llm`` opts into
# the v1.0 LLM-driven synthesizer with a hard fallback to ``pattern`` on
# LLM failure (so the wire response never 5xxs even when the LLM path
# is broken).
InsightsSynthesizerChoice = Literal["pattern", "llm"]


class ForesightWorkspaceConfig(TimestampedDocument):
    """Per-workspace Foresight configuration overrides.

    Fields:
      - ``workspace`` — tenancy key. Indexed unique so ``find_one``
        / upsert remains O(1).
      - ``threshold_override`` — when not ``None``, the workspace's
        effective onboarding-gate threshold. Read by
        ``get_onboarding_gate`` and by the backtest scorer
        (``create_backtest``) so a workspace that has tightened the bar
        sees ALL gate-scoping reads use the override. Constrained at
        the DTO layer (0.5–0.95 inclusive); the doc stores the raw float
        because a future bump of the floor must not retroactively
        invalidate stored overrides — the DTO validator is the single
        source of truth for the legal range at write time.
      - ``insights_synthesizer`` — ``"pattern"`` (default) keeps the v0.5
        deterministic five-rule synthesizer; ``"llm"`` opts the
        workspace into the LLM-driven synthesizer
        (``ee.foresight.insights_llm``) with a hard fallback to
        ``pattern`` on LLM failure. Cost discipline note: the LLM mode
        is opt-in only — workspaces with the default value pay zero
        LLM cost. Constrained at the DTO layer.
      - ``createdAt`` / ``updatedAt`` — inherited from
        :class:`TimestampedDocument`. ``updatedAt`` doubles as the
        "when did the admin last touch this" timestamp the GET response
        exposes to the UI.

    v1.0 ships threshold + insights_synthesizer. v1.1 candidates:
      - ``default_sub_type: str`` — the sub_type the new-scenario wizard
        opens on.
      - ``notification_routing: dict[str, str]`` — channel routing for
        backtest-completed / onboarding-unlocked.
      - ``insight_cap: int`` — per-call cap on synthesizer insights.
      - ``llm_cache_ttl_seconds: int`` — per-workspace LLM cache TTL
        override (default is the module constant
        ``ee.foresight.insights_llm.DEFAULT_CACHE_TTL_SECONDS``).

    The shape is therefore extension-additive; new optional fields with
    safe defaults won't break callers reading the v1.0 wire dict.
    """

    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    threshold_override: float | None = Field(default=None)
    insights_synthesizer: InsightsSynthesizerChoice = Field(default="pattern")

    class Settings:
        name = "foresight_workspace_configs"
        # ``workspace`` already has a unique single-field index from the
        # ``Indexed(..., unique=True)`` annotation; the upsert path uses it
        # directly. No composite indexes needed in v1.0.
