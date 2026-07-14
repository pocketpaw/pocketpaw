# ee/pocketpaw_ee/cloud/models/workspace_automation_config.py
# Created: 2026-07-11 (feat/external-alerting-c2c3) — Per-workspace automation
# opt-out. The always-on cloud sweeps (cycles, decisions, member_ingest,
# fabric_ingest, temporal, refresh) run for EVERY active workspace by default;
# this doc lets a single tenant turn its background automation OFF without a
# code change / redeploy:
#   - ``sweeps_enabled``      — master switch for ALL background sweeps for the
#     workspace. False makes every sweep skip this tenant at its per-workspace
#     fan-out point.
#   - ``automations_enabled`` — narrower switch reserved for the rule/alert
#     automation surface specifically (evaluator-driven rules); kept separate so
#     an admin can silence alerts without stopping data-mirroring sweeps.
# Both default TRUE so an unconfigured workspace keeps the always-on behavior;
# the row only exists once an admin has opted out (and survives a re-enable so
# ``updatedAt`` stays audit-relevant).
#
# Why a per-workspace Mongo doc (mirrors InstinctWorkspaceConfig /
# ForesightWorkspaceConfig / BeltWorkspaceConfig): the toggle must survive
# restarts and be settable at runtime by an admin, and the deployment topology
# is per-tenant-dedicated-server, so a workspace-keyed doc is the tenant-sane
# home. One row per workspace; ``workspace`` is indexed unique so the
# read/upsert path stays O(1). The shape is extension-additive — future
# per-workspace automation knobs layer on as new optional fields with safe
# defaults.
#
# Only ``ee.cloud.automations_status.service`` reads/writes this module (same
# single-importer discipline as InstinctWorkspaceConfig, enforced by the
# "AutomationsStatus" import-linter contract in ee/pyproject.toml).

from __future__ import annotations

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceAutomationConfig(TimestampedDocument):
    """Per-workspace opt-out for the always-on background automation sweeps.

    Fields:
      - ``workspace`` — tenancy key. Indexed unique so ``find_one`` / upsert
        stays O(1).
      - ``sweeps_enabled`` — master switch for ALL background sweeps for this
        workspace. ``True`` (default) keeps the always-on behavior; ``False``
        makes every sweep skip this tenant at its per-workspace fan-out.
      - ``automations_enabled`` — narrower switch for the rule/alert automation
        surface. ``True`` (default); ``False`` silences alert automations while
        leaving other sweeps (data mirroring, snapshots) untouched.
      - ``createdAt`` / ``updatedAt`` — inherited from
        :class:`TimestampedDocument`. ``updatedAt`` doubles as the "when did the
        admin last touch this" timestamp the status response exposes.

    The shape is extension-additive; new optional fields with safe defaults
    won't break callers reading the v1 wire dict.
    """

    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    sweeps_enabled: bool = Field(default=True)
    automations_enabled: bool = Field(default=True)

    class Settings:
        name = "workspace_automation_configs"
        # ``workspace`` already has a unique single-field index from the
        # ``Indexed(..., unique=True)`` annotation; the upsert path uses it
        # directly. No composite indexes needed in v1.
