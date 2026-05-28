# tests/cloud/test_foresight_agent_context.py
# Created: 2026-05-28 — coverage for the agent-facing Foresight wrappers
# (``ee.cloud.foresight.agent_context``) that back the in-process MCP
# tools. The wrappers translate ``ContextVar`` identity + CloudError
# subclasses into the ``{ok, ...}`` envelope the MCP server consumes.
# Tests live alongside the other foresight cloud tests so they reuse
# the ``mongo_db`` fixture from ``tests/cloud/conftest.py``.

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import attach_agent_identity, detach_agent_identity
from pocketpaw_ee.cloud.foresight import agent_context

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.fixture
def bound_identity() -> Iterator[None]:
    """Bind a workspace/user pair into the chat ContextVars for the
    duration of one test — mirrors what ``agent_router._run_agent_stream``
    does for a live chat session."""
    tokens = attach_agent_identity(workspace_id="w1", user_id="prakash")
    try:
        yield None
    finally:
        detach_agent_identity(tokens)


def _decision_forecast_yaml(name: str = "agent-ctx-forecast") -> str:
    return f"""name: {name}
sub_type: decision_forecast
n_ticks: 1
personas:
  - name: anne
    role: approver
    ocean:
      conscientiousness: 0.5
  - name: bob
    role: tenant
    ocean: {{}}
"""


# ---------------------------------------------------------------------------
# Missing workspace context — the bug we're fixing
# ---------------------------------------------------------------------------


async def test_save_without_workspace_context_returns_clean_error() -> None:
    """No ContextVar set → wrappers MUST refuse rather than fabricate a
    workspace. This is the captain-hit bug — the agent claimed success
    while writing to nothing."""
    out = await agent_context.save_scenario_for_agent(
        name="anywhere",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml(),
    )
    assert out == {
        "ok": False,
        "error": agent_context.NO_WORKSPACE_ERROR,
        "message": agent_context.NO_WORKSPACE_MESSAGE,
    }


async def test_list_without_workspace_context_returns_clean_error() -> None:
    out = await agent_context.list_scenarios_for_agent()
    assert out["ok"] is False
    assert out["error"] == agent_context.NO_WORKSPACE_ERROR


async def test_run_without_workspace_context_returns_clean_error() -> None:
    out = await agent_context.run_scenario_for_agent(name="x", custom_scenario_id="abc")
    assert out["ok"] is False
    assert out["error"] == agent_context.NO_WORKSPACE_ERROR


# ---------------------------------------------------------------------------
# Happy path — save / list / get / update / delete
# ---------------------------------------------------------------------------


async def test_save_scenario_persists_and_returns_id(bound_identity: None) -> None:
    out = await agent_context.save_scenario_for_agent(
        name="q3-renewals",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("q3-renewals"),
        description="enterprise cohort",
    )
    assert out["ok"] is True
    assert out["name"] == "q3-renewals"
    assert out["sub_type"] == "decision_forecast"
    assert out["description"] == "enterprise cohort"
    assert out["workspace_id"] == "w1"
    assert out["author"] == "prakash"
    assert out["parsed_meta"]["num_personas"] == 2
    # The id is captured so a follow-up run knows which scenario to use.
    assert "id" in out and out["id"]


async def test_list_after_save_includes_the_new_scenario(bound_identity: None) -> None:
    save = await agent_context.save_scenario_for_agent(
        name="renewal-listing",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("renewal-listing"),
    )
    listing = await agent_context.list_scenarios_for_agent(limit=10)
    assert listing["ok"] is True
    assert listing["total"] >= 1
    saved_ids = [item["id"] for item in listing["items"]]
    assert save["id"] in saved_ids


async def test_get_scenario_returns_full_yaml_body(bound_identity: None) -> None:
    save = await agent_context.save_scenario_for_agent(
        name="renewal-detail",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("renewal-detail"),
    )
    detail = await agent_context.get_scenario_for_agent(save["id"])
    assert detail["ok"] is True
    assert detail["id"] == save["id"]
    assert detail["yaml_body"].startswith("name: renewal-detail")


async def test_update_full_replaces_fields(bound_identity: None) -> None:
    save = await agent_context.save_scenario_for_agent(
        name="renewal-edit",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("renewal-edit"),
        description="initial",
    )
    out = await agent_context.update_scenario_for_agent(
        scenario_id=save["id"],
        name="renewal-edit-v2",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("renewal-edit-v2"),
        description="edited",
    )
    assert out["ok"] is True
    assert out["name"] == "renewal-edit-v2"
    assert out["description"] == "edited"


async def test_delete_removes_the_scenario(bound_identity: None) -> None:
    save = await agent_context.save_scenario_for_agent(
        name="renewal-delete",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("renewal-delete"),
    )
    out = await agent_context.delete_scenario_for_agent(save["id"])
    assert out == {"ok": True, "scenario_id": save["id"]}
    # Second delete surfaces a clean 404 envelope.
    again = await agent_context.delete_scenario_for_agent(save["id"])
    assert again["ok"] is False
    assert again["error"] == "foresight_custom_scenario.not_found"


# ---------------------------------------------------------------------------
# CloudError surfacing — validation paths return ok=False without raising
# ---------------------------------------------------------------------------


async def test_save_surfaces_invalid_yaml_as_ok_false(bound_identity: None) -> None:
    out = await agent_context.save_scenario_for_agent(
        name="bad",
        sub_type="decision_forecast",
        yaml_body="not: valid: yaml: : :: :",
    )
    assert out["ok"] is False
    assert out["error"] == "foresight.invalid_yaml"
    assert "message" in out


async def test_save_surfaces_sub_type_mismatch_as_ok_false(bound_identity: None) -> None:
    # YAML declares decision_forecast but request claims market_sim.
    out = await agent_context.save_scenario_for_agent(
        name="mismatch",
        sub_type="market_sim",
        yaml_body=_decision_forecast_yaml("mismatch"),
    )
    assert out["ok"] is False
    assert out["error"] == "foresight.sub_type_mismatch"


async def test_get_unknown_scenario_returns_not_found(bound_identity: None) -> None:
    out = await agent_context.get_scenario_for_agent("507f1f77bcf86cd799439011")
    assert out["ok"] is False
    assert out["error"] == "foresight_custom_scenario.not_found"


# ---------------------------------------------------------------------------
# Run path — saved-scenario only
# ---------------------------------------------------------------------------


async def test_run_requires_custom_scenario_id(bound_identity: None) -> None:
    out = await agent_context.run_scenario_for_agent(name="orphan", custom_scenario_id="")
    assert out["ok"] is False
    assert out["error"] == "foresight.missing_scenario_id"


async def test_run_scenario_executes_a_saved_scenario(
    bound_identity: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the run path against a real saved scenario. We stub the
    engine call so the test stays cheap — the goal is to confirm the
    wrapper threads workspace/user identity and the saved-id correctly
    into ``create_scenario_run``, not to exercise the engine."""
    save = await agent_context.save_scenario_for_agent(
        name="run-target",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("run-target"),
    )

    async def _fake_engine(body: Any, *, workspace_id: str, run_id: str, route_to_instinct: bool):
        return {"aggregates": {"verdict": "stubbed"}, "projected_decisions": []}

    monkeypatch.setattr("pocketpaw_ee.cloud.foresight.service._run_engine_inline", _fake_engine)

    out = await agent_context.run_scenario_for_agent(
        name="run-target", custom_scenario_id=save["id"]
    )
    assert out["ok"] is True
    assert out["status"] == "complete"
    assert out["scenario_name"] == "run-target"
    assert out["workspace_id"] == "w1"
    assert out["result"]["aggregates"]["verdict"] == "stubbed"


async def test_list_runs_returns_workspace_runs(
    bound_identity: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = await agent_context.save_scenario_for_agent(
        name="lr-target",
        sub_type="decision_forecast",
        yaml_body=_decision_forecast_yaml("lr-target"),
    )

    async def _fake_engine(body: Any, *, workspace_id: str, run_id: str, route_to_instinct: bool):
        return {"aggregates": {}, "projected_decisions": []}

    monkeypatch.setattr("pocketpaw_ee.cloud.foresight.service._run_engine_inline", _fake_engine)
    await agent_context.run_scenario_for_agent(name="lr-target", custom_scenario_id=save["id"])

    out = await agent_context.list_runs_for_agent(limit=5)
    assert out["ok"] is True
    assert len(out["items"]) >= 1
    assert any(item["scenario_name"] == "lr-target" for item in out["items"])
