# dto.py — Request / response wire schemas for the automations-status entity.
# Created: 2026-07-11 (feat/external-alerting-c2c3, C3). Per cloud rule §4 the
# request schema is distinct from the response schema. Tenancy is NEVER a body
# field — it arrives as the service's explicit ``workspace_id`` from the auth
# context. Responses are plain (snake_case) Pydantic models the merged-screen UI
# reads; the domain value objects convert into these in the service.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class SetWorkspaceAutomationRequest(BaseModel):
    """Body for ``PUT /automations/config`` → ``service.set_workspace_config``.

    Both fields are optional so an admin can toggle one switch without touching
    the other. ``None`` means "leave unchanged"; the service reads the current
    doc, applies only the provided fields, and upserts. Tenancy is the service's
    ``workspace_id`` parameter, never a field here.
    """

    model_config = ConfigDict(extra="forbid")

    sweeps_enabled: bool | None = None
    automations_enabled: bool | None = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class SweepDescriptorOut(BaseModel):
    key: str
    label: str
    kind: str
    env_flag: str
    env_flag_on: bool
    interval_env: str | None = None
    description: str = ""


class RuleSummaryOut(BaseModel):
    id: str
    name: str
    type: str
    enabled: bool
    pocket_id: str
    fire_count: int


class WorkspaceAutomationStateOut(BaseModel):
    workspace_id: str
    sweeps_enabled: bool
    automations_enabled: bool
    configured: bool


class EvaluatorStatusOut(BaseModel):
    running: bool
    interval_seconds: int
    autostart_enabled: bool


class AutomationStatusResponse(BaseModel):
    """The aggregate the workspace-scoped status endpoint returns."""

    workspace_id: str
    scheduler_enabled: bool
    evaluator: EvaluatorStatusOut
    workspace_state: WorkspaceAutomationStateOut
    sweeps: list[SweepDescriptorOut]
    rules: list[RuleSummaryOut]
