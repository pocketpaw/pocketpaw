# tests/cloud/test_foresight_run_with_custom.py
# Created: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) — RFC 08
# v1.0 wave 3. Tests the ``create_scenario_run`` extension that accepts
# ``custom_scenario_id`` on the POST body. When the field is set, the
# service loads the saved YAML scenario and drives the engine from it
# instead of the inline ``personas`` block.
"""Tests for the ``custom_scenario_id`` integration on POST /scenarios."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    CreateCustomScenarioRequest,
    CreateScenarioRequest,
    PersonaSpecRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _custom_yaml(name: str = "saved-decision", n_ticks: int = 1) -> str:
    """Three-persona Decision Forecast YAML — enough to drive the engine
    end-to-end through the deterministic backend so the run completes."""
    return f"""name: {name}
sub_type: decision_forecast
n_ticks: {n_ticks}
personas:
  - name: tenant-anne
    role: tenant
    ocean:
      conscientiousness: 0.4
  - name: approver-bob
    role: approver
    ocean:
      conscientiousness: 0.8
  - name: agent-cory
    role: agent
    ocean: {{}}
"""


async def _save_scenario(ctx: RequestContext, *, name: str = "loaded-scenario") -> str:
    saved = await foresight_scenarios.create_custom_scenario(
        ctx,
        CreateCustomScenarioRequest(
            name=name,
            sub_type="decision_forecast",
            description="",
            yaml_body=_custom_yaml(name=name),
        ),
    )
    return saved.id


# ---------------------------------------------------------------------------
# Happy path — custom_scenario_id drives the engine from the saved YAML
# ---------------------------------------------------------------------------


async def test_run_loads_saved_scenario_when_id_supplied() -> None:
    """``custom_scenario_id`` loads the saved YAML; ``personas`` on the
    body may be empty because the saved scenario provides them."""
    ctx = _ctx()
    saved_id = await _save_scenario(ctx, name="renewal-q3")

    body = CreateScenarioRequest(
        name="run-against-saved",  # echoed for audit; engine uses saved name
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[],  # empty — saved YAML supplies them
        custom_scenario_id=saved_id,
    )
    out = await foresight_service.create_scenario_run(ctx, body)
    assert out.status == "complete"
    assert out.result is not None
    # The engine name comes from the saved YAML's ``name`` field.
    assert out.result["scenario_name"] == "renewal-q3"


async def test_run_with_custom_id_persists_request_blob() -> None:
    """Audit trail keeps the operator's POST body shape — the loaded
    scenario id is preserved on ``request.custom_scenario_id`` so a
    post-mortem can rebuild the engine inputs."""
    ctx = _ctx()
    saved_id = await _save_scenario(ctx, name="audit-trail")
    body = CreateScenarioRequest(
        name="audit-via-saved",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[],
        custom_scenario_id=saved_id,
    )
    out = await foresight_service.create_scenario_run(ctx, body)
    assert out.request["custom_scenario_id"] == saved_id
    # The result references the saved YAML's name (engine truth).
    assert out.result["scenario_name"] == "audit-trail"


async def test_run_with_custom_id_wins_over_sub_type_field() -> None:
    """When the body specifies a ``sub_type`` that differs from the
    saved YAML's ``sub_type``, ``custom_scenario_id`` wins and the
    engine runs the saved sub_type. The body's value is preserved
    on the audit blob but doesn't drive the engine."""
    ctx = _ctx()
    saved_id = await _save_scenario(ctx, name="conflict-test")  # decision_forecast
    body = CreateScenarioRequest(
        name="conflict-run",
        # Operator body says market_sim, saved YAML says decision_forecast.
        # custom_scenario_id wins — engine ticks decision_forecast.
        sub_type="market_sim",
        n_ticks=1,
        personas=[],
        custom_scenario_id=saved_id,
    )
    out = await foresight_service.create_scenario_run(ctx, body)
    assert out.status == "complete"
    assert out.result["sub_type"] == "decision_forecast"


# ---------------------------------------------------------------------------
# Validation paths
# ---------------------------------------------------------------------------


async def test_run_422_on_unknown_custom_scenario_id() -> None:
    ctx = _ctx()
    body = CreateScenarioRequest(
        name="unknown",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[],
        custom_scenario_id="5f50c31b1c9d440000000000",
    )
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_scenario_run(ctx, body)
    assert exc.value.code == "foresight.custom_scenario_not_found"


async def test_run_422_on_cross_tenant_custom_scenario_id() -> None:
    """A custom scenario from another workspace must not be loadable —
    the 422 collapses cross-tenant into the same not_found code so
    existence isn't leakable across tenants."""
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    saved_id = await _save_scenario(ctx_w1, name="w1-private")
    body = CreateScenarioRequest(
        name="cross-tenant-attempt",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[],
        custom_scenario_id=saved_id,
    )
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_scenario_run(ctx_w2, body)
    assert exc.value.code == "foresight.custom_scenario_not_found"


async def test_run_422_on_malformed_custom_scenario_id() -> None:
    ctx = _ctx()
    body = CreateScenarioRequest(
        name="bad-id",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[],
        custom_scenario_id="not-an-objectid",
    )
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_scenario_run(ctx, body)
    assert exc.value.code == "foresight.custom_scenario_not_found"


# ---------------------------------------------------------------------------
# Inline-personas path unaffected (regression guard)
# ---------------------------------------------------------------------------


async def test_inline_personas_path_unchanged_when_id_absent() -> None:
    """The v0.5 inline-personas POST keeps working when no
    ``custom_scenario_id`` is supplied. Regression guard for the
    helper refactor — the inline branch must still complete."""
    ctx = _ctx()
    body = CreateScenarioRequest(
        name="inline-still-works",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="Anne", role="approver", ocean={})],
    )
    out = await foresight_service.create_scenario_run(ctx, body)
    assert out.status == "complete"
    assert out.result["scenario_name"] == "inline-still-works"


async def test_inline_path_still_422s_on_empty_personas_without_id() -> None:
    """Without ``custom_scenario_id``, an empty personas list still
    fails engine validation (the engine requires ≥1 persona)."""
    ctx = _ctx()
    body = CreateScenarioRequest(
        name="empty-personas-no-id",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[],
    )
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_scenario_run(ctx, body)
    assert exc.value.code == "foresight.invalid_scenario"
