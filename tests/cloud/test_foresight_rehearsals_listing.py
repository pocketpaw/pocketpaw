# tests/cloud/test_foresight_rehearsals_listing.py
# Created: 2026-05-29 (feat/foresight-rehearsals-joined) — service-level
# tests for ``ee.cloud.foresight.scenarios.list_rehearsals``. Covers the
# v2 ``/foresight`` landing card hydration paths: empty workspace, draft
# scenario (no runs), scenario with multiple completed runs (last_run
# picks the latest), in-flight latest run (status=running surfaces),
# tenancy isolation, pagination, and sub_type filter.
"""Tests for ``ee.cloud.foresight.scenarios.list_rehearsals``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    CreateCustomScenarioRequest,
    CreateScenarioRequest,
)
from pocketpaw_ee.cloud.models.foresight_run import (
    ForesightRun as _ForesightRunDoc,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _decision_forecast_yaml(name: str = "saved-df", n_ticks: int = 1) -> str:
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
"""


def _market_sim_yaml(name: str = "saved-ms") -> str:
    return f"""name: {name}
sub_type: market_sim
n_ticks: 1
personas:
  - name: enterprise-buyer
    role: customer
    ocean: {{}}
  - name: competitor-acme
    role: competitor
    ocean: {{}}
"""


async def _save_scenario(
    ctx: RequestContext,
    *,
    name: str,
    sub_type: str = "decision_forecast",
) -> str:
    yaml_body = (
        _market_sim_yaml(name=name)
        if sub_type == "market_sim"
        else _decision_forecast_yaml(name=name)
    )
    saved = await foresight_scenarios.create_custom_scenario(
        ctx,
        CreateCustomScenarioRequest(
            name=name,
            sub_type=sub_type,  # type: ignore[arg-type]
            description=f"desc for {name}",
            yaml_body=yaml_body,
        ),
    )
    return saved.id


async def _run_saved(ctx: RequestContext, scenario_id: str) -> str:
    """Drive a run through the cloud service so the persisted shape
    (request.custom_scenario_id + workspace + status=complete) matches
    what production writes — keeps the test honest about the join key."""
    body = CreateScenarioRequest(
        name="rehearsal-run",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[],
        custom_scenario_id=scenario_id,
    )
    out = await foresight_service.create_scenario_run(ctx, body)
    return out.id


async def _force_status(
    run_id: str,
    *,
    status: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Flip a persisted run doc's status (and optionally error/result)
    after the engine pass already ran. Used to exercise the
    in-flight / failed / verdict-summary branches without rewiring the
    engine fake."""
    from beanie import PydanticObjectId

    oid = PydanticObjectId(run_id)
    doc = await _ForesightRunDoc.find_one({"_id": oid})
    assert doc is not None
    doc.status = status
    if error is not None:
        doc.error = error
    if result is not None:
        doc.result = result
    await doc.save()


# ---------------------------------------------------------------------------
# Empty + draft state
# ---------------------------------------------------------------------------


async def test_empty_workspace_returns_zero_items() -> None:
    ctx = _ctx(workspace="w-empty")
    response = await foresight_scenarios.list_rehearsals(ctx)
    assert response.items == []
    assert response.total == 0
    assert response.limit == 50
    assert response.offset == 0
    assert response.has_more is False


async def test_scenario_with_no_runs_reports_zero_count_and_none_last_run() -> None:
    """A saved scenario with no runs is the v2 landing's "draft" state:
    ``run_count=0`` and ``last_run=None`` so the card renders the
    "never run" badge instead of a verdict."""
    ctx = _ctx()
    await _save_scenario(ctx, name="draft-renewal")

    response = await foresight_scenarios.list_rehearsals(ctx)
    assert len(response.items) == 1
    item = response.items[0]
    assert item.name == "draft-renewal"
    assert item.run_count == 0
    assert item.last_run is None


# ---------------------------------------------------------------------------
# Multi-run grouping + last_run picks the latest
# ---------------------------------------------------------------------------


async def test_scenario_with_multiple_runs_counts_and_picks_latest() -> None:
    """Two runs against the same scenario: ``run_count == 2`` and
    ``last_run.id`` is the most recently created run."""
    ctx = _ctx()
    saved_id = await _save_scenario(ctx, name="renewal-q3")
    _first_run_id = await _run_saved(ctx, saved_id)
    second_run_id = await _run_saved(ctx, saved_id)

    response = await foresight_scenarios.list_rehearsals(ctx)
    assert len(response.items) == 1
    item = response.items[0]
    assert item.run_count == 2
    assert item.last_run is not None
    # The second run is the most recent — it should win.
    assert item.last_run.id == second_run_id
    # The deterministic engine completes the run synchronously so the
    # persisted status flips to ``complete`` before the rehearsals read
    # sees it.
    assert item.last_run.status == "complete"
    # Verdict summary is best-effort. The deterministic Decision Forecast
    # backend surfaces a non-empty modal_outcome (``last_action=observe``);
    # the helper stringifies it as ``key=value`` pairs sorted by key. We
    # don't pin the exact verdict string here (engine output may evolve);
    # we just assert the summary is non-empty and isn't the failure label.
    assert item.last_run.verdict_summary is not None
    assert item.last_run.verdict_summary != ""
    assert not item.last_run.verdict_summary.startswith("Run failed")


async def test_last_run_status_running_when_latest_run_is_in_flight() -> None:
    """If the most-recent run is still in flight, the landing card needs
    to see ``status=running`` (not the previous run's ``complete``) so
    the badge can render the spinner."""
    ctx = _ctx()
    saved_id = await _save_scenario(ctx, name="long-sim")
    _first_run_id = await _run_saved(ctx, saved_id)
    second_run_id = await _run_saved(ctx, saved_id)
    # Flip the latest run to ``running`` to simulate the v1.0 worker case.
    await _force_status(second_run_id, status="running", result=None)

    response = await foresight_scenarios.list_rehearsals(ctx)
    item = response.items[0]
    assert item.run_count == 2
    assert item.last_run is not None
    assert item.last_run.id == second_run_id
    assert item.last_run.status == "running"
    # In-flight runs surface ``verdict_summary=None`` so the UI renders
    # the spinner instead of an outcome string.
    assert item.last_run.verdict_summary is None


async def test_failed_run_surfaces_error_in_verdict_summary() -> None:
    """A failed run reports its persisted error in ``verdict_summary``
    so the landing card can show "why it failed" without click-through."""
    ctx = _ctx()
    saved_id = await _save_scenario(ctx, name="will-fail")
    run_id = await _run_saved(ctx, saved_id)
    await _force_status(
        run_id,
        status="failed",
        error="EngineError: persona pool exhausted",
    )

    response = await foresight_scenarios.list_rehearsals(ctx)
    item = response.items[0]
    assert item.last_run is not None
    assert item.last_run.status == "failed"
    assert item.last_run.verdict_summary == "EngineError: persona pool exhausted"


async def test_verdict_summary_surfaces_modal_outcome_when_present() -> None:
    """When the run's result blob carries a non-empty ``modal_outcome``,
    the verdict_summary stringifies the dict instead of the
    "Run complete" fallback."""
    ctx = _ctx()
    saved_id = await _save_scenario(ctx, name="verdict-test")
    run_id = await _run_saved(ctx, saved_id)
    await _force_status(
        run_id,
        status="complete",
        result={
            "scenario_name": "verdict-test",
            "modal_outcome": {"action": "approve"},
            "aggregate": {},
        },
    )

    response = await foresight_scenarios.list_rehearsals(ctx)
    item = response.items[0]
    assert item.last_run is not None
    assert item.last_run.verdict_summary == "action=approve"


# ---------------------------------------------------------------------------
# Tenancy isolation
# ---------------------------------------------------------------------------


async def test_runs_from_other_workspace_dont_leak_into_count() -> None:
    """Workspace A's runs don't count against workspace B's scenarios —
    even when scenario ids would collide on a cross-tenant ``$in``
    query, the workspace filter is the leading clause."""
    ctx_a = _ctx(workspace="ws-a", user="ua")
    ctx_b = _ctx(workspace="ws-b", user="ub")

    scenario_a_id = await _save_scenario(ctx_a, name="a-only")
    scenario_b_id = await _save_scenario(ctx_b, name="b-only")
    await _run_saved(ctx_a, scenario_a_id)
    await _run_saved(ctx_a, scenario_a_id)
    await _run_saved(ctx_b, scenario_b_id)

    response_a = await foresight_scenarios.list_rehearsals(ctx_a)
    response_b = await foresight_scenarios.list_rehearsals(ctx_b)

    # Workspace A sees its own scenario + 2 runs; never sees B's.
    assert [item.name for item in response_a.items] == ["a-only"]
    assert response_a.items[0].run_count == 2
    # Workspace B sees its own scenario + 1 run; never sees A's.
    assert [item.name for item in response_b.items] == ["b-only"]
    assert response_b.items[0].run_count == 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_pagination_slices_by_limit_and_offset() -> None:
    """``limit=2 offset=2`` returns the third and fourth scenarios sorted
    by ``updatedAt desc`` — the same ordering the editor picker uses."""
    ctx = _ctx()
    names = ["s1", "s2", "s3", "s4", "s5"]
    for name in names:
        await _save_scenario(ctx, name=name)

    response = await foresight_scenarios.list_rehearsals(ctx, limit=2, offset=2)
    assert response.limit == 2
    assert response.offset == 2
    assert response.total == 5
    assert response.has_more is True
    assert len(response.items) == 2
    # Most-recent-edit-first: s5, s4, s3, s2, s1 → offset 2 → s3, s2.
    assert [item.name for item in response.items] == ["s3", "s2"]


async def test_pagination_has_more_false_on_last_page() -> None:
    ctx = _ctx()
    for name in ["a", "b", "c"]:
        await _save_scenario(ctx, name=name)

    response = await foresight_scenarios.list_rehearsals(ctx, limit=2, offset=2)
    assert response.total == 3
    assert response.has_more is False
    assert len(response.items) == 1


# ---------------------------------------------------------------------------
# sub_type filter
# ---------------------------------------------------------------------------


async def test_sub_type_filter_narrows_to_matching_scenarios_only() -> None:
    """``sub_type=market_sim`` returns only the Market Sim scenarios —
    Decision Forecasts and Org Change rehearsals are excluded even when
    they live in the same workspace."""
    ctx = _ctx()
    await _save_scenario(ctx, name="forecast-1", sub_type="decision_forecast")
    await _save_scenario(ctx, name="market-1", sub_type="market_sim")
    await _save_scenario(ctx, name="market-2", sub_type="market_sim")

    response = await foresight_scenarios.list_rehearsals(ctx, sub_type="market_sim")
    assert response.total == 2
    assert {item.name for item in response.items} == {"market-1", "market-2"}
    for item in response.items:
        assert item.sub_type == "market_sim"
