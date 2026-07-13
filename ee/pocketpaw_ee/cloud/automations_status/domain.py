# domain.py — Frozen value objects for the automations-status entity.
# Created: 2026-07-11 (feat/external-alerting-c2c3, C3) — the constructed cloud
# sweep REGISTRY (there is no queryable registry in the fleet today) plus the
# aggregate status view the workspace-scoped endpoint returns. Tenancy fields are
# required at construction per ee/cloud Rule 3.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# The kind of background automation a sweep performs. Informational only — the
# UI groups the registry rows by this.
SweepKind = Literal[
    "snapshot",  # cycles daily snapshot
    "decisions",  # decision-graph reconciler + abandon sweeper
    "ingest",  # member_ingest / fabric_ingest data mirroring
    "temporal",  # per-pocket temporal sweeps
    "refresh",  # per-pocket interval source refresh
]


@dataclass(frozen=True)
class SweepDescriptor:
    """One row in the constructed cloud sweep registry.

    ``key`` is a stable id; ``env_flag`` is the process env var that gates the
    sweep's loop-spawn at app boot; ``env_flag_on`` is that flag's current value
    in THIS process (resolved at read time). ``interval_env`` names the optional
    override for the sweep's cadence, when it has one.
    """

    key: str
    label: str
    kind: SweepKind
    env_flag: str
    env_flag_on: bool
    interval_env: str | None = None
    description: str = ""


@dataclass(frozen=True)
class RuleSummary:
    """A compact view of one OSS automation rule for the status list."""

    id: str
    name: str
    type: str
    enabled: bool
    pocket_id: str
    fire_count: int


@dataclass(frozen=True)
class WorkspaceAutomationState:
    """The per-workspace opt-out state (tenancy required)."""

    workspace_id: str
    sweeps_enabled: bool
    automations_enabled: bool
    configured: bool  # True once an admin has written an explicit doc


@dataclass(frozen=True)
class EvaluatorStatus:
    """The OSS AutomationEvaluator's runtime state."""

    running: bool
    interval_seconds: int
    autostart_enabled: bool


@dataclass(frozen=True)
class AutomationStatusView:
    """The aggregate the workspace-scoped status endpoint returns.

    Tenancy (``workspace_id``) is required at construction per cloud Rule 3.
    ``scheduler_enabled`` is the master cloud-scheduler env gate's current value
    in this process — the deployment-level "are background sweeps running at all"
    switch that production sets. The per-workspace opt-out (``workspace_state``)
    layers on top of it.
    """

    workspace_id: str
    scheduler_enabled: bool
    evaluator: EvaluatorStatus
    workspace_state: WorkspaceAutomationState
    sweeps: list[SweepDescriptor] = field(default_factory=list)
    rules: list[RuleSummary] = field(default_factory=list)
