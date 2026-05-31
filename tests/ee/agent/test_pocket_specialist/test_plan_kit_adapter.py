# Created: 2026-05-25 (feat/pocket-planner-skill) — tests for the
#   plan-pointer kit path on the create adapter. When
#   POCKETPAW_POCKET_SPECIALIST_USE_SKILL is on and the brief did not
#   match a template, the adapter must return a plan_kit response
#   pointing at the pocketpaw-pocket-planner skill + plan_pocket MCP
#   tool instead of the one-shot draft kit.
#
#   These tests sit alongside the existing draft-kit tests in
#   test_adapters.py — kept separate so the plan-kit MVP's wiring can
#   evolve without touching the green draft-kit test set.
"""Tests for AgentModeAdapter.create's plan-pointer kit branch."""

from __future__ import annotations

import pytest
from pocketpaw_ee.agent.pocket_specialist.adapters import AgentModeAdapter
from pocketpaw_ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistHints,
)

from pocketpaw.config import Settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every env var that might smuggle a real operator override
    into these tests. Mirrors test_adapters.py's fixture."""
    for key in (
        "POCKETPAW_POCKET_SPECIALIST_BACKEND",
        "POCKETPAW_POCKET_SPECIALIST_MODEL",
        "POCKETPAW_POCKET_SPECIALIST_MAX_VALIDATION_RETRIES",
        "POCKETPAW_POCKET_SPECIALIST_MODE",
        "POCKETPAW_POCKET_SPECIALIST_USE_SKILL",
        "POCKETPAW_DEEP_AGENTS_MODEL",
        "POCKETPAW_CLAUDE_SDK_MODEL",
        "POCKETPAW_LANGCHAIN_REACT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def agent_settings() -> Settings:
    return Settings(_env_file=None, pocket_specialist_mode="agent")


class TestPlanKitGate:
    """USE_SKILL + no template + no spec → plan_kit. All other shapes
    take the prior path."""

    @pytest.mark.asyncio
    async def test_use_skill_off_returns_draft_kit(
        self, agent_settings: Settings, monkeypatch
    ) -> None:
        """Without USE_SKILL the adapter must keep the original
        draft-kit behaviour — the plan-pointer kit is opt-in."""
        monkeypatch.delenv("POCKETPAW_POCKET_SPECIALIST_USE_SKILL", raising=False)

        adapter = AgentModeAdapter()
        result = await adapter.create(
            PocketSpecialistCreateInput(brief="Build me a custom multi-widget app for X."),
            workspace_id="ws_1",
            user_id="user_1",
            settings=agent_settings,
        )

        assert result.action == "draft_kit"
        # The draft_kit body for one-shot drafting carries the starter
        # widget kinds, not the planner pointer.
        assert "starter_widget_kinds" in result.draft_kit
        assert "skill_name" not in result.draft_kit

    @pytest.mark.asyncio
    async def test_use_skill_on_returns_plan_kit(
        self, agent_settings: Settings, monkeypatch
    ) -> None:
        """USE_SKILL toggles the plan-pointer kit path."""
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_USE_SKILL", "true")

        adapter = AgentModeAdapter()
        result = await adapter.create(
            PocketSpecialistCreateInput(
                brief="Build me a sales CRM with leads, pipeline, activity, forecast.",
            ),
            workspace_id="ws_1",
            user_id="user_1",
            settings=agent_settings,
        )

        assert result.action == "plan_kit"
        assert result.ok is False  # plan_kit is a handoff, not a success
        assert result.pocket is None
        assert result.backend_used == "agent_mode_skill"

        kit = result.draft_kit
        assert kit is not None
        assert kit["skill_name"] == "pocketpaw-pocket-planner"
        assert kit["mcp_tool"] == "mcp__pocketpaw_pocket_planner__plan_pocket"
        # Auth headers carry the tenant identity for loopback /spec/merge.
        assert kit["auth_headers"]["X-PocketPaw-Workspace-Id"] == "ws_1"
        assert kit["auth_headers"]["X-PocketPaw-User-Id"] == "user_1"
        assert kit["auth_headers"]["X-PocketPaw-Internal"] == "true"
        # The brief is carried over so the chat agent can pass it to
        # plan_pocket without re-deriving from chat history.
        assert "sales CRM" in kit["brief"]
        # next_step must teach the four-phase flow.
        assert "plan_pocket" in kit["next_step"]
        assert "/spec/merge" in kit["next_step"]

    @pytest.mark.asyncio
    async def test_spec_supplied_skips_plan_kit(
        self, agent_settings: Settings, monkeypatch
    ) -> None:
        """When the chat agent walked the todos and is calling back with
        a built spec (the legacy create flow's second call), the
        adapter must NOT bounce them into the planner. ``input.spec``
        is the second-call shape — the plan_kit gate only fires on the
        first call."""
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_USE_SKILL", "true")

        adapter = AgentModeAdapter()
        # An input with spec set goes straight to _validate_and_persist.
        # We don't actually want to run that path here — we just verify
        # the adapter does NOT return plan_kit when spec is non-None.
        # The persist call will error out because we don't have a mongo,
        # but that's the point: the test passes if action is not
        # "plan_kit". An exception is fine — pytest just shouldn't see a
        # "plan_kit" action.
        from unittest.mock import patch

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.adapters._validate_and_persist"
        ) as mock_validate:
            mock_validate.return_value = type(
                "FakeOutput",
                (),
                {
                    "action": "created",
                    "ok": True,
                    "draft_kit": None,
                },
            )()
            result = await adapter.create(
                PocketSpecialistCreateInput(
                    brief="Build me something custom.",
                    spec={"version": "1.0", "state": {}, "ui": {"id": "n_a", "type": "flex"}},
                ),
                workspace_id="ws_1",
                user_id="user_1",
                settings=agent_settings,
            )

        # validate_and_persist was called; plan_kit was NOT returned.
        mock_validate.assert_called_once()
        assert result.action != "plan_kit"

    @pytest.mark.asyncio
    async def test_template_id_short_circuit_skips_plan_kit(
        self, agent_settings: Settings, monkeypatch
    ) -> None:
        """A template-match short-circuit must beat the plan_kit gate.

        Built-in templates are the FAST path — even with USE_SKILL on,
        if the chat agent already matched a template we should not
        bounce the user through the planner."""
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_USE_SKILL", "true")

        # Patch _input_with_template_spec to behave as though the
        # template loaded — returns an input clone with spec populated.
        from unittest.mock import patch

        original_input = PocketSpecialistCreateInput(
            brief="Build me a kanban for tasks.",
            hints=PocketSpecialistHints(template_id="kanban-board"),
        )
        templated_input = original_input.model_copy(
            update={"spec": {"version": "1.0", "state": {}, "ui": {"id": "n_a", "type": "flex"}}}
        )

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.adapters._input_with_template_spec",
                return_value=templated_input,
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.adapters._validate_and_persist"
            ) as mock_validate,
        ):
            mock_validate.return_value = type(
                "FakeOutput",
                (),
                {
                    "action": "created",
                    "ok": True,
                    "draft_kit": None,
                },
            )()
            adapter = AgentModeAdapter()
            result = await adapter.create(
                original_input,
                workspace_id="ws_1",
                user_id="user_1",
                settings=agent_settings,
            )

        mock_validate.assert_called_once()
        # Template path won — no plan_kit handoff.
        assert result.action != "plan_kit"
