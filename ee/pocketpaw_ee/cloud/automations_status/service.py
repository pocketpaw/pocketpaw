# service.py — automations-status business logic + the sole writer of
# WorkspaceAutomationConfig + the per-workspace sweep GATE the schedulers import.
# Created: 2026-07-11 (feat/external-alerting-c2c3, C3).
#
# This module owns three responsibilities:
#   1. The per-workspace opt-out GATE — ``sweeps_enabled_for_workspace`` /
#      ``automations_enabled_for_workspace``. Every cloud sweep consults this at
#      its per-workspace fan-out so a tenant can turn its automation off. The gate
#      FAILS OPEN (defaults to enabled) on a store read error: a transient Mongo
#      hiccup must never silently disable the always-on fleet for every tenant.
#   2. The config read / write over ``WorkspaceAutomationConfig`` (the sole Beanie
#      writer — import-linter "AutomationsStatus"). ``get_workspace_config`` /
#      ``set_workspace_config`` mirror the rules.service enforcement upsert.
#   3. The aggregate ``agent_get_status`` the workspace-scoped router returns —
#      OSS rules + evaluator status + the constructed cloud sweep registry + the
#      per-workspace enable state. Tenancy is threaded from ``ctx.workspace_id``;
#      every Beanie read is workspace-filtered. The OSS automation rules store is
#      the box-local single-tenant store (per-tenant-dedicated-server topology),
#      surfaced here as informational status.

from __future__ import annotations

import logging
import os

from pocketpaw.config import get_settings
from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.automations_status.domain import (
    AutomationStatusView,
    EvaluatorStatus,
    RuleSummary,
    SweepDescriptor,
    WorkspaceAutomationState,
)
from pocketpaw_ee.cloud.automations_status.dto import (
    AutomationStatusResponse,
    EvaluatorStatusOut,
    RuleSummaryOut,
    SweepDescriptorOut,
    WorkspaceAutomationStateOut,
)
from pocketpaw_ee.cloud.models.workspace_automation_config import WorkspaceAutomationConfig

logger = logging.getLogger(__name__)

# The master cloud-scheduler env gate. Default OFF — production sets it to
# "true" to run background sweeps (mirrors ``billing_enforced``: env-gated,
# off in tests/dev so a pytest run never spawns a loop that outlives the test).
# The per-workspace opt-out below layers on TOP of this deployment switch.
_CLOUD_SCHEDULER_FLAG = "POCKETPAW_CLOUD_SCHEDULER_ENABLED"
# The pocket-interval refresh sweep carries its own gate.
_REFRESH_SCHEDULER_FLAG = "POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED"


def _env_flag_on(name: str) -> bool:
    return os.environ.get(name, "").lower() == "true"


def scheduler_enabled() -> bool:
    """True when the master cloud-scheduler env gate is on in THIS process."""
    return _env_flag_on(_CLOUD_SCHEDULER_FLAG)


# ---------------------------------------------------------------------------
# Constructed cloud sweep REGISTRY.
#
# There is no queryable registry in the fleet today — each sweep is wired
# independently in ``ee/pocketpaw_ee/cloud/__init__.py``. This static table is
# the single place that enumerates them, kept in sync with the app-factory
# wiring by the C4 touch-time rule (add a sweep → add a row here). The env-flag
# on/off value is resolved at read time so the status reflects THIS process.
# ---------------------------------------------------------------------------

_SWEEP_TABLE: tuple[tuple[str, str, str, str, str | None, str], ...] = (
    (
        "cycles_snapshot",
        "Cycle daily snapshot",
        "snapshot",
        _CLOUD_SCHEDULER_FLAG,
        None,
        "Once per UTC midnight, snapshots every active cycle in each active workspace.",
    ),
    (
        "decisions_reconciler",
        "Decision-graph reconciler + abandon sweeper",
        "decisions",
        _CLOUD_SCHEDULER_FLAG,
        None,
        "60s reconciler drains chain events the hot path missed; hourly sweeper "
        "closes chains for parked Actions past the TTL.",
    ),
    (
        "member_ingest",
        "Member Gmail/Calendar ingest",
        "ingest",
        _CLOUD_SCHEDULER_FLAG,
        "POCKETPAW_MEMBER_INGEST_INTERVAL_SECONDS",
        "Backfills/incrementally syncs each consented member's Gmail + Calendar "
        "into their private KB scope.",
    ),
    (
        "fabric_ingest",
        "Firestore → Fabric ingest",
        "ingest",
        _CLOUD_SCHEDULER_FLAG,
        "POCKETPAW_FABRIC_INGEST_INTERVAL_SECONDS",
        "Mirrors each workspace's configured Firestore collections into Fabric "
        "objects (backfill then incremental).",
    ),
    (
        "temporal_sweeps",
        "Pocket temporal sweeps",
        "temporal",
        _CLOUD_SCHEDULER_FLAG,
        None,
        "Fires per-pocket temporal triggers across the workspace × pocket "
        "cross-product for pockets that declare interval sources.",
    ),
    (
        "pocket_refresh",
        "Pocket interval-source refresh",
        "refresh",
        _REFRESH_SCHEDULER_FLAG,
        None,
        "Re-pulls each pocket's interval data sources on its declared cadence.",
    ),
)


def build_sweep_registry() -> list[SweepDescriptor]:
    """Return the constructed cloud sweep registry with live env-flag state."""
    return [
        SweepDescriptor(
            key=key,
            label=label,
            kind=kind,  # type: ignore[arg-type]
            env_flag=env_flag,
            env_flag_on=_env_flag_on(env_flag),
            interval_env=interval_env,
            description=description,
        )
        for (key, label, kind, env_flag, interval_env, description) in _SWEEP_TABLE
    ]


# ---------------------------------------------------------------------------
# Per-workspace opt-out — config read / write + the sweep GATE.
# ---------------------------------------------------------------------------


async def get_workspace_config(workspace_id: str) -> WorkspaceAutomationState:
    """Return the workspace's automation opt-out state.

    Unconfigured workspaces default to fully enabled (the always-on posture);
    ``configured`` is False until an admin has written an explicit doc. RAISES on
    a store read error so the caller owns the failure decision (the aggregate
    endpoint surfaces it; the sweep gate below catches it and fails OPEN).
    """
    doc = await WorkspaceAutomationConfig.find_one(
        WorkspaceAutomationConfig.workspace == workspace_id,
    )
    if doc is None:
        return WorkspaceAutomationState(
            workspace_id=workspace_id,
            sweeps_enabled=True,
            automations_enabled=True,
            configured=False,
        )
    return WorkspaceAutomationState(
        workspace_id=workspace_id,
        sweeps_enabled=doc.sweeps_enabled,
        automations_enabled=doc.automations_enabled,
        configured=True,
    )


async def set_workspace_config(
    workspace_id: str,
    *,
    sweeps_enabled: bool | None,
    automations_enabled: bool | None,
) -> WorkspaceAutomationState:
    """Upsert the workspace's automation opt-out, returning the new state.

    ``None`` on a field leaves it unchanged. ``workspace`` is unique-indexed so
    the find-then-insert/save upsert is O(1); a concurrent-insert race surfaces
    as a DuplicateKeyError (admin-only write — treated as a 5xx, no retry loop,
    mirroring ``rules.service.set_enforcement``).
    """
    doc = await WorkspaceAutomationConfig.find_one(
        WorkspaceAutomationConfig.workspace == workspace_id,
    )
    if doc is None:
        doc = WorkspaceAutomationConfig(
            workspace=workspace_id,
            sweeps_enabled=True if sweeps_enabled is None else sweeps_enabled,
            automations_enabled=(True if automations_enabled is None else automations_enabled),
        )
        await doc.insert()
    else:
        if sweeps_enabled is not None:
            doc.sweeps_enabled = sweeps_enabled
        if automations_enabled is not None:
            doc.automations_enabled = automations_enabled
        await doc.save()
    return await get_workspace_config(workspace_id)


async def sweeps_enabled_for_workspace(workspace_id: str) -> bool:
    """The GATE every cloud sweep consults at its per-workspace fan-out.

    Returns True (sweep this workspace) unless the workspace explicitly opted
    out. FAILS OPEN: on a store read error it returns True and logs, so a
    transient Mongo hiccup can never silently disable the always-on fleet for
    every tenant. A workspace that deliberately turned sweeps off stays off.
    """
    try:
        state = await get_workspace_config(workspace_id)
    except Exception:
        logger.warning(
            "automations_status: sweep-gate read failed for workspace=%s — failing OPEN",
            workspace_id,
            exc_info=True,
        )
        return True
    return state.sweeps_enabled


async def filter_sweep_enabled_workspaces(workspace_ids: set[str]) -> set[str]:
    """Return the subset of ``workspace_ids`` whose sweeps are enabled.

    A single dedup pass for fan-outs that iterate MANY units (members, sources,
    pockets) spanning few workspaces — each unique workspace is checked once
    instead of once per unit. Fails OPEN per workspace (a read hiccup keeps that
    tenant in the swept set), consistent with ``sweeps_enabled_for_workspace``.
    """
    enabled: set[str] = set()
    for ws in workspace_ids:
        if await sweeps_enabled_for_workspace(ws):
            enabled.add(ws)
    return enabled


async def automations_enabled_for_workspace(workspace_id: str) -> bool:
    """Narrower gate for the rule/alert automation surface. Fails OPEN like the
    sweep gate — a read hiccup never silences alerts for the whole fleet."""
    try:
        state = await get_workspace_config(workspace_id)
    except Exception:
        logger.warning(
            "automations_status: automation-gate read failed for workspace=%s — failing OPEN",
            workspace_id,
            exc_info=True,
        )
        return True
    return state.automations_enabled


# ---------------------------------------------------------------------------
# Aggregate status — the workspace-scoped read the merged-screen UI consumes.
# ---------------------------------------------------------------------------


def _evaluator_status() -> EvaluatorStatus:
    """Snapshot the OSS AutomationEvaluator's runtime state.

    Best-effort: if the OSS automations package is unavailable (a cloud-only
    build without the evaluator wired) the status degrades to not-running rather
    than raising.
    """
    autostart = bool(get_settings().automation_evaluator_autostart)
    try:
        from pocketpaw.automations.evaluator import get_evaluator

        ev = get_evaluator()
        return EvaluatorStatus(
            running=ev.is_running,
            interval_seconds=ev.interval,
            autostart_enabled=autostart,
        )
    except Exception:
        logger.debug("automations_status: OSS evaluator unavailable", exc_info=True)
        return EvaluatorStatus(running=False, interval_seconds=0, autostart_enabled=autostart)


def _oss_rule_summaries() -> list[RuleSummary]:
    """Summarize the box-local OSS automation rules (informational).

    The OSS automation store is the single-tenant box store (per-tenant-dedicated
    -server topology); it has no workspace column, so this is a box-level read
    surfaced in the workspace's status view rather than a tenant-filtered query.
    Best-effort — a missing/broken store degrades to an empty list.
    """
    try:
        from pocketpaw.automations.store import get_automation_store

        rules = get_automation_store().list_rules()
    except Exception:
        logger.debug("automations_status: OSS rule store unavailable", exc_info=True)
        return []
    return [
        RuleSummary(
            id=r.id,
            name=r.name,
            type=str(r.type),
            enabled=r.enabled,
            pocket_id=r.pocket_id,
            fire_count=r.fire_count,
        )
        for r in rules
    ]


async def agent_get_status(ctx: RequestContext) -> AutomationStatusResponse:
    """Aggregate the workspace's automation status behind a single read.

    Tenancy comes from ``ctx.workspace_id`` (never a body/query); a request
    without an active workspace is a 400, not a silent global read.
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise ValidationError(
            "automations_status.workspace_required",
            "an active workspace is required to read automation status",
        )

    workspace_state = await get_workspace_config(workspace_id)
    view = AutomationStatusView(
        workspace_id=workspace_id,
        scheduler_enabled=scheduler_enabled(),
        evaluator=_evaluator_status(),
        workspace_state=workspace_state,
        sweeps=build_sweep_registry(),
        rules=_oss_rule_summaries(),
    )
    return _view_to_response(view)


def _view_to_response(view: AutomationStatusView) -> AutomationStatusResponse:
    return AutomationStatusResponse(
        workspace_id=view.workspace_id,
        scheduler_enabled=view.scheduler_enabled,
        evaluator=EvaluatorStatusOut(
            running=view.evaluator.running,
            interval_seconds=view.evaluator.interval_seconds,
            autostart_enabled=view.evaluator.autostart_enabled,
        ),
        workspace_state=WorkspaceAutomationStateOut(
            workspace_id=view.workspace_state.workspace_id,
            sweeps_enabled=view.workspace_state.sweeps_enabled,
            automations_enabled=view.workspace_state.automations_enabled,
            configured=view.workspace_state.configured,
        ),
        sweeps=[
            SweepDescriptorOut(
                key=s.key,
                label=s.label,
                kind=s.kind,
                env_flag=s.env_flag,
                env_flag_on=s.env_flag_on,
                interval_env=s.interval_env,
                description=s.description,
            )
            for s in view.sweeps
        ],
        rules=[
            RuleSummaryOut(
                id=r.id,
                name=r.name,
                type=r.type,
                enabled=r.enabled,
                pocket_id=r.pocket_id,
                fire_count=r.fire_count,
            )
            for r in view.rules
        ],
    )


__all__ = [
    "agent_get_status",
    "automations_enabled_for_workspace",
    "build_sweep_registry",
    "filter_sweep_enabled_workspaces",
    "get_workspace_config",
    "scheduler_enabled",
    "set_workspace_config",
    "sweeps_enabled_for_workspace",
]
