# tests/ee/agent/test_pocket_specialist/test_adapters.py
# Created: 2026-05-14 (feat/pocket-specialist-agent-mode) — covers the new
# mode-dispatch layer in ee/agent/pocket_specialist/adapters.py.
# SubagentAdapter wraps the historical pipeline; AgentModeAdapter
# implements the two-call protocol (draft kit on first call, validate-
# and-persist on second). pick_adapter() routes by setting.
"""Tests for ``ee.agent.pocket_specialist.adapters``.

Adapter behavior is mocked end-to-end so we don't need a real backend
or a real Mongo: ``SubagentAdapter`` is tested by patching
``_run_subagent_pipeline`` and asserting delegation; ``AgentModeAdapter``
is tested against a patched ``make_persist_pocket_tool`` factory that
mimics the capture-dict mutation the real tool performs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.agent.pocket_specialist.adapters import (
    AgentModeAdapter,
    SubagentAdapter,
    pick_adapter,
)
from pocketpaw_ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistCreateOutput,
    PocketSpecialistHints,
)

from pocketpaw.config import Settings

_LEAKY_ENV_KEYS = (
    "POCKETPAW_POCKET_SPECIALIST_BACKEND",
    "POCKETPAW_POCKET_SPECIALIST_MODEL",
    "POCKETPAW_POCKET_SPECIALIST_MAX_VALIDATION_RETRIES",
    "POCKETPAW_POCKET_SPECIALIST_MODE",
    "POCKETPAW_DEEP_AGENTS_MODEL",
    "POCKETPAW_CLAUDE_SDK_MODEL",
    "POCKETPAW_LANGCHAIN_REACT_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every env var that might smuggle a real operator override
    into these tests."""
    for key in _LEAKY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def agent_settings() -> Settings:
    return Settings(_env_file=None, pocket_specialist_mode="agent")


def _persist_factory_stub(pocket: dict[str, Any] | None, warnings: list[str] | None = None):
    """Mirror tests/test_runtime.py's _persist_factory_stub: the real
    tool factory captures the persisted pocket into the supplied dict.
    Here we let the test pass a None pocket to simulate the validation-
    redraft branch (no save)."""

    def _stub(
        *,
        workspace_id: str,
        user_id: str,
        capture: dict[str, Any] | None = None,
        max_validation_retries: int = 3,
    ):
        async def _ainvoke(args: dict[str, Any]) -> dict[str, Any]:
            if capture is not None:
                if warnings:
                    capture["warnings"] = list(warnings)
                if pocket is not None:
                    capture["pocket"] = pocket
            return {"ok": pocket is not None}

        tool = MagicMock()
        tool.ainvoke = AsyncMock(side_effect=_ainvoke)
        return tool

    return _stub


# ---------------------------------------------------------------------------
# pick_adapter
# ---------------------------------------------------------------------------


class TestPickAdapter:
    def test_subagent_mode_returns_subagent_adapter(self) -> None:
        assert isinstance(pick_adapter("subagent"), SubagentAdapter)

    def test_agent_mode_returns_agent_mode_adapter(self) -> None:
        assert isinstance(pick_adapter("agent"), AgentModeAdapter)

    def test_unknown_mode_falls_back_to_subagent(self, caplog) -> None:
        """A stale config value shouldn't brick a deployment — log + degrade."""
        with caplog.at_level("WARNING"):
            adapter = pick_adapter("not-a-real-mode")
        assert isinstance(adapter, SubagentAdapter)
        assert any("not-a-real-mode" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# SubagentAdapter
# ---------------------------------------------------------------------------


class TestSubagentAdapter:
    @pytest.mark.asyncio
    async def test_delegates_to_run_subagent_pipeline(self, settings) -> None:
        expected = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p1"},
            duration_ms=10,
            backend_used="deep_agents",
        )
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime._run_subagent_pipeline",
            new=AsyncMock(return_value=expected),
        ) as mock_pipeline:
            adapter = SubagentAdapter()
            payload = PocketSpecialistCreateInput(brief="brief enough to clear minlen")
            out = await adapter.create(payload, workspace_id="w1", user_id="u1", settings=settings)
        assert out is expected
        mock_pipeline.assert_awaited_once_with(
            payload, workspace_id="w1", user_id="u1", settings=settings
        )


# ---------------------------------------------------------------------------
# AgentModeAdapter
# ---------------------------------------------------------------------------


class TestAgentModeAdapterDraftKit:
    @pytest.mark.asyncio
    async def test_first_call_no_spec_returns_draft_kit(self, agent_settings) -> None:
        adapter = AgentModeAdapter()
        hints = PocketSpecialistHints(
            name="Cat Tracker",
            color="#F59E0B",
            purpose="Track cat moods",
            focal_widget="stat",
        )
        payload = PocketSpecialistCreateInput(
            brief="A whimsical pocket for tracking cat moods", hints=hints
        )

        out = await adapter.create(
            payload, workspace_id="w1", user_id="u1", settings=agent_settings
        )

        assert out.ok is False
        assert out.action == "draft_kit"
        assert out.pocket is None
        assert out.backend_used == "agent_mode"
        assert out.draft_kit is not None
        # Kit echoes the structural plan from hints so the chat agent
        # has it in fresh context, not just system-prompt-buried.
        plan = out.draft_kit["structural_plan"]
        assert plan["name"] == "Cat Tracker"
        assert plan["focal_widget"] == "stat"
        # Kit includes shape reminder + widget kinds + next step instructions.
        assert "ripple_spec_shape" in out.draft_kit
        assert "starter_widget_kinds" in out.draft_kit
        assert "spec=" in out.draft_kit["next_step"]

    @pytest.mark.asyncio
    async def test_first_call_no_hints_returns_empty_plan(self, agent_settings) -> None:
        """No hints → empty structural plan, but the rest of the kit is intact."""
        adapter = AgentModeAdapter()
        payload = PocketSpecialistCreateInput(brief="brief enough to clear minlen")

        out = await adapter.create(
            payload, workspace_id="w1", user_id="u1", settings=agent_settings
        )
        assert out.action == "draft_kit"
        assert out.draft_kit is not None
        assert out.draft_kit["structural_plan"] == {}
        # Starter list expanded from 10 → 40+ widgets so the chat agent
        # has visibility into the actual library (Ripple ships 150
        # widgets; the prior 10-item starter list was provably too
        # narrow). Bound is conservative — drop in case someone trims
        # the tuple aggressively in a future cleanup.
        starters = out.draft_kit["starter_widget_kinds"]
        assert isinstance(starters, list)
        assert len(starters) >= 30
        # The polished pattern layouts MUST be in the starter list —
        # they're the lever that flips the LLM away from "compose a
        # dashboard from scratch" toward "use the pre-composed widget".
        for required in (
            "pipeline-dashboard",
            "analytics-dashboard",
            "entity-detail",
            "master-detail",
            "filter-bar",
            "wizard-layout",
            "audit-log",
        ):
            assert required in starters, (
                f"starter_widget_kinds missing high-leverage widget {required!r}"
            )

    @pytest.mark.asyncio
    async def test_draft_kit_includes_rich_widgets_by_pattern(self, agent_settings) -> None:
        """The kit carries a pattern → polished-widget map so the chat
        agent picks the composed layout instead of rebuilding it from
        primitives. Every pattern from STEP 1 of VISUAL_VARIATION_RULE
        must have at least one entry."""
        adapter = AgentModeAdapter()
        payload = PocketSpecialistCreateInput(brief="brief enough to clear minlen")
        out = await adapter.create(
            payload, workspace_id="w1", user_id="u1", settings=agent_settings
        )
        assert out.draft_kit is not None
        rich = out.draft_kit.get("rich_widgets_by_pattern")
        assert isinstance(rich, dict)
        # Every pattern from VISUAL_VARIATION_RULE STEP 1 must be
        # covered. Missing a pattern here means the LLM falls back to
        # primitives for that whole category.
        for pattern in ("dashboard", "viewer", "app", "browser", "wizard", "feed"):
            assert pattern in rich, f"rich_widgets_by_pattern missing {pattern!r}"
            assert isinstance(rich[pattern], list)
            assert len(rich[pattern]) >= 1
        # Spot-check the dashboard family — this is the case the team-
        # dashboard regression came in through.
        assert "pipeline-dashboard" in rich["dashboard"]
        # And the widget-quality-bar reminder ties it together.
        assert "widget_quality_bar" in out.draft_kit
        assert "pipeline-dashboard" in out.draft_kit["widget_quality_bar"].lower()

    @pytest.mark.asyncio
    async def test_no_backend_spawned_on_draft_kit(self, agent_settings) -> None:
        """Agent mode must not spin up an isolated backend for the kit
        — the chat agent's own model is the one drafting."""
        with patch("pocketpaw.agents.router.AgentRouter.create_isolated_backend") as mock_backend:
            adapter = AgentModeAdapter()
            payload = PocketSpecialistCreateInput(brief="brief enough to clear minlen")
            await adapter.create(payload, workspace_id="w1", user_id="u1", settings=agent_settings)
        mock_backend.assert_not_called()


class TestAgentModeAdapterPersist:
    @pytest.mark.asyncio
    async def test_second_call_with_spec_validates_and_persists(self, agent_settings) -> None:
        """The second call (input.spec set) goes through the same
        persist tool the subagent flow uses — no LLM, no backend."""
        persisted = {"id": "p-new", "name": "Cats"}
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools.make_persist_pocket_tool",
            new=MagicMock(side_effect=_persist_factory_stub(persisted)),
        ) as mock_factory:
            adapter = AgentModeAdapter()
            payload = PocketSpecialistCreateInput(
                brief="brief enough to clear minlen",
                hints=PocketSpecialistHints(name="Cats", color="#F59E0B"),
                spec={"type": "flex", "children": []},
            )
            out = await adapter.create(
                payload, workspace_id="w1", user_id="u1", settings=agent_settings
            )

        assert out.ok is True
        assert out.action == "created"
        assert out.pocket == persisted
        assert out.backend_used == "agent_mode"
        assert out.draft_kit is None
        mock_factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_call_with_target_pocket_id_marks_extended(self, agent_settings) -> None:
        persisted = {"id": "p-existing", "name": "Cats"}
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools.make_persist_pocket_tool",
            new=MagicMock(side_effect=_persist_factory_stub(persisted)),
        ):
            adapter = AgentModeAdapter()
            payload = PocketSpecialistCreateInput(
                brief="brief enough to clear minlen",
                hints=PocketSpecialistHints(target_pocket_id="p-existing"),
                spec={"type": "flex", "children": []},
            )
            out = await adapter.create(
                payload, workspace_id="w1", user_id="u1", settings=agent_settings
            )
        assert out.action == "extended"

    @pytest.mark.asyncio
    async def test_validation_warnings_surface_for_redraft(self, agent_settings) -> None:
        """When the persist tool refuses to save (warnings present, retry
        budget unspent), the adapter returns the warnings under
        ``action="redraft"`` (distinct from ``"failed"``) so the chat
        agent can switch on the action label and call again with a
        corrected spec without treating the run as a terminal error."""
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools.make_persist_pocket_tool",
            new=MagicMock(
                side_effect=_persist_factory_stub(None, warnings=["chart.xKey is not a valid prop"])
            ),
        ):
            adapter = AgentModeAdapter()
            payload = PocketSpecialistCreateInput(
                brief="brief enough to clear minlen",
                spec={"type": "chart", "props": {"xKey": "nope"}},
            )
            out = await adapter.create(
                payload, workspace_id="w1", user_id="u1", settings=agent_settings
            )

        assert out.ok is False
        assert out.action == "redraft"
        assert out.pocket is None
        assert "chart.xKey" in out.warnings[0]
        assert "redraft" in (out.error or "").lower()

    @pytest.mark.asyncio
    async def test_persist_anyway_after_retries_returns_ok_with_warnings(
        self, agent_settings
    ) -> None:
        """``make_persist_pocket_tool`` is designed to persist anyway
        after ``max_validation_retries`` attempts — never blocks the
        user on a perma-retry loop. When that happens the capture dict
        has BOTH a pocket AND non-empty warnings. The adapter must
        return ``action="created"`` with the warnings surfaced — NOT
        ``"redraft"`` or ``"failed"``. This pins the read-order between
        ``captured_pocket`` and ``captured_warnings`` in
        ``_validate_and_persist`` — a regression that flipped the
        check (warnings-first instead of pocket-first) would route a
        successful-with-warnings persist into the redraft branch and
        leave the chat agent stuck in a loop."""
        persisted = {"id": "p-imperfect", "name": "Imperfect"}
        residual = ["chart.xKey unrecognized (kept anyway after retries)"]
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools.make_persist_pocket_tool",
            new=MagicMock(side_effect=_persist_factory_stub(persisted, warnings=residual)),
        ):
            adapter = AgentModeAdapter()
            payload = PocketSpecialistCreateInput(
                brief="brief enough to clear minlen",
                spec={"type": "flex", "children": []},
            )
            out = await adapter.create(
                payload, workspace_id="w1", user_id="u1", settings=agent_settings
            )

        assert out.ok is True
        assert out.action == "created"
        assert out.pocket == persisted
        assert out.warnings == residual
        assert out.error is None

    @pytest.mark.asyncio
    async def test_persist_exception_returns_failed(self, agent_settings) -> None:
        """If the persist tool raises (transport error, etc.) the
        adapter surfaces the exception in the error field rather than
        bubbling up to the chat agent as an unhandled traceback."""

        def _exploding_factory(**kwargs):
            tool = MagicMock()
            tool.ainvoke = AsyncMock(side_effect=RuntimeError("mongo down"))
            return tool

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools.make_persist_pocket_tool",
            new=MagicMock(side_effect=_exploding_factory),
        ):
            adapter = AgentModeAdapter()
            payload = PocketSpecialistCreateInput(
                brief="brief enough to clear minlen",
                spec={"type": "flex"},
            )
            out = await adapter.create(
                payload, workspace_id="w1", user_id="u1", settings=agent_settings
            )

        assert out.ok is False
        assert out.action == "failed"
        assert "mongo down" in (out.error or "")

    @pytest.mark.asyncio
    async def test_no_backend_spawned_on_persist(self, agent_settings) -> None:
        with (
            patch("pocketpaw.agents.router.AgentRouter.create_isolated_backend") as mock_backend,
            patch(
                "pocketpaw_ee.agent.pocket_specialist.tools.make_persist_pocket_tool",
                new=MagicMock(side_effect=_persist_factory_stub({"id": "p1"})),
            ),
        ):
            adapter = AgentModeAdapter()
            payload = PocketSpecialistCreateInput(
                brief="brief enough to clear minlen",
                spec={"type": "flex"},
            )
            await adapter.create(payload, workspace_id="w1", user_id="u1", settings=agent_settings)
        mock_backend.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch integration: run_specialist picks the right adapter
# ---------------------------------------------------------------------------


class TestRunSpecialistDispatch:
    @pytest.mark.asyncio
    async def test_run_specialist_uses_subagent_adapter_by_default(self) -> None:
        """The public entry point honors settings.pocket_specialist_mode."""
        from pocketpaw_ee.agent.pocket_specialist.runtime import run_specialist

        s = Settings(_env_file=None)  # default mode = subagent
        sentinel = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p1"},
            duration_ms=5,
            backend_used="deep_agents",
        )
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime._run_subagent_pipeline",
            new=AsyncMock(return_value=sentinel),
        ) as mock_pipeline:
            out = await run_specialist(
                PocketSpecialistCreateInput(brief="brief enough to clear minlen"),
                workspace_id="w1",
                user_id="u1",
                settings=s,
            )
        assert out is sentinel
        mock_pipeline.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_specialist_uses_agent_mode_adapter_when_set(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import run_specialist

        s = Settings(_env_file=None, pocket_specialist_mode="agent")
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime._run_subagent_pipeline",
            new=AsyncMock(),
        ) as mock_pipeline:
            out = await run_specialist(
                PocketSpecialistCreateInput(brief="brief enough to clear minlen"),
                workspace_id="w1",
                user_id="u1",
                settings=s,
            )
        # Subagent pipeline is bypassed entirely in agent mode.
        mock_pipeline.assert_not_called()
        assert out.action == "draft_kit"
        assert out.backend_used == "agent_mode"
