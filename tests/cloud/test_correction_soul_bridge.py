# tests/cloud/test_correction_soul_bridge.py — Tests for the correction soul bridge
# and the instinct_corrections agent tool (Move 1 PR-B).
# Created: 2026-04-13 — Covers observe() call shape, 3x procedural promotion,
# graceful degradation when no soul is loaded, and tool output formatting.

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ee.instinct.correction import Correction, CorrectionPatch
from ee.instinct.correction_soul_bridge import CorrectionSoulBridge
from ee.instinct.models import (
    Action,
    ActionCategory,
    ActionPriority,
    ActionTrigger,
)
from ee.instinct.store import InstinctStore


@pytest.fixture(autouse=True)
def _stub_soul_protocol(monkeypatch):
    """Provide a minimal fake `soul_protocol` module when the real one is absent.

    Production code imports `soul_protocol.Interaction` lazily inside the
    bridge; the real package is an optional dep not installed in the base
    dev env. A lightweight stub is sufficient to exercise the bridge's code
    path without pulling in the full soul runtime.
    """
    if "soul_protocol" in sys.modules:
        return

    module = types.ModuleType("soul_protocol")

    class _Interaction:
        def __init__(self, user_input: str = "", agent_output: str = "", **kwargs):
            self.user_input = user_input
            self.agent_output = agent_output
            for k, v in kwargs.items():
                setattr(self, k, v)

    module.Interaction = _Interaction  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "soul_protocol", module)


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="claude", reason="test")


def _action(**overrides) -> Action:
    defaults: dict = {
        "pocket_id": "pocket-1",
        "title": "Send renewal outreach",
        "description": "Two accounts up for renewal",
        "recommendation": "Draft a formal nudge email",
        "trigger": _trigger(),
        "category": ActionCategory.WORKFLOW,
        "priority": ActionPriority.MEDIUM,
        "parameters": {"tone": "formal", "discount_pct": 20},
    }
    defaults.update(overrides)
    return Action(**defaults)


def _correction(
    *,
    action_id: str = "act-1",
    patches: list[CorrectionPatch] | None = None,
    pocket_id: str = "pocket-1",
    actor: str = "user:priya",
) -> Correction:
    return Correction(
        action_id=action_id,
        pocket_id=pocket_id,
        actor=actor,
        patches=patches or [CorrectionPatch(path="title", before="A", after="B")],
        context_summary="softened the greeting",
        action_title="Send renewal outreach",
    )


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "bridge_test.db")


@pytest.fixture
def fake_soul():
    soul = MagicMock()
    soul.observe = AsyncMock()
    soul.remember = AsyncMock()
    return soul


@pytest.fixture
def manager_with_soul(fake_soul):
    manager = MagicMock()
    manager.soul = fake_soul
    return manager


# ---------------------------------------------------------------------------
# record() — observe path
# ---------------------------------------------------------------------------


class TestObserveCorrection:
    @pytest.mark.asyncio
    async def test_observe_called_once_per_correction(
        self, manager_with_soul, fake_soul, store: InstinctStore
    ) -> None:
        bridge = CorrectionSoulBridge(soul_manager=manager_with_soul, store=store)
        await bridge.record(_correction(), _action())

        assert fake_soul.observe.await_count == 1

    @pytest.mark.asyncio
    async def test_observe_payload_includes_summary_and_patches(
        self, manager_with_soul, fake_soul, store: InstinctStore
    ) -> None:
        bridge = CorrectionSoulBridge(soul_manager=manager_with_soul, store=store)
        correction = _correction(
            patches=[
                CorrectionPatch(path="title", before="Formal", after="Casual"),
                CorrectionPatch(path="parameters.discount_pct", before=20, after=15),
            ],
        )
        await bridge.record(correction, _action())

        (call,) = fake_soul.observe.await_args_list
        interaction = call.args[0]
        assert "pocket-1" in interaction.user_input
        assert "user:priya" in interaction.user_input
        assert "title" in interaction.agent_output
        assert "parameters.discount_pct" in interaction.agent_output
        assert "softened the greeting" in interaction.agent_output

    @pytest.mark.asyncio
    async def test_no_observe_when_soul_is_absent(self, store: InstinctStore) -> None:
        manager = MagicMock()
        manager.soul = None
        bridge = CorrectionSoulBridge(soul_manager=manager, store=store)
        # Should not raise, just no-op.
        await bridge.record(_correction(), _action())

    @pytest.mark.asyncio
    async def test_observe_exception_is_swallowed(
        self, manager_with_soul, fake_soul, store: InstinctStore
    ) -> None:
        fake_soul.observe.side_effect = RuntimeError("soul went down")
        bridge = CorrectionSoulBridge(soul_manager=manager_with_soul, store=store)
        # Should not raise — approval flow must never break because soul is sick.
        await bridge.record(_correction(), _action())


# ---------------------------------------------------------------------------
# Procedural promotion — 3x-same-path heuristic
# ---------------------------------------------------------------------------


class TestProceduralPromotion:
    @pytest.mark.asyncio
    async def test_promotes_on_third_same_path(
        self, manager_with_soul, fake_soul, store: InstinctStore
    ) -> None:
        bridge = CorrectionSoulBridge(soul_manager=manager_with_soul, store=store)

        first = _correction(
            action_id="act-a",
            patches=[CorrectionPatch(path="parameters.tone", before="formal", after="casual")],
        )
        second = _correction(
            action_id="act-b",
            patches=[CorrectionPatch(path="parameters.tone", before="formal", after="casual")],
        )
        third = _correction(
            action_id="act-c",
            patches=[CorrectionPatch(path="parameters.tone", before="formal", after="casual")],
        )

        await store.record_correction(first)
        await bridge.record(first, _action())
        assert fake_soul.remember.await_count == 0

        await store.record_correction(second)
        await bridge.record(second, _action())
        assert fake_soul.remember.await_count == 0

        await store.record_correction(third)
        await bridge.record(third, _action())
        assert fake_soul.remember.await_count == 1

        kwargs = fake_soul.remember.await_args.kwargs
        assert kwargs["type"] == "procedural"
        assert kwargs["importance"] == 7
        assert "parameters.tone" in kwargs["content"] or "tone" in kwargs["content"]
        assert "casual" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_does_not_re_promote_past_threshold(
        self, manager_with_soul, fake_soul, store: InstinctStore
    ) -> None:
        bridge = CorrectionSoulBridge(soul_manager=manager_with_soul, store=store)

        for i in range(4):
            correction = _correction(
                action_id=f"act-{i}",
                patches=[CorrectionPatch(path="title", before="A", after="B")],
            )
            await store.record_correction(correction)
            await bridge.record(correction, _action())

        # Promotion fires exactly once, when count hits the threshold.
        assert fake_soul.remember.await_count == 1

    @pytest.mark.asyncio
    async def test_promotes_per_path_independently(
        self, manager_with_soul, fake_soul, store: InstinctStore
    ) -> None:
        bridge = CorrectionSoulBridge(soul_manager=manager_with_soul, store=store)

        for i in range(3):
            await store.record_correction(
                _correction(
                    action_id=f"title-{i}",
                    patches=[CorrectionPatch(path="title", before="A", after="B")],
                ),
            )
        for i in range(3):
            await store.record_correction(
                _correction(
                    action_id=f"prio-{i}",
                    patches=[CorrectionPatch(path="priority", before="medium", after="high")],
                ),
            )

        await bridge.record(
            _correction(
                action_id="title-2",
                patches=[CorrectionPatch(path="title", before="A", after="B")],
            ),
            _action(),
        )
        await bridge.record(
            _correction(
                action_id="prio-2",
                patches=[CorrectionPatch(path="priority", before="medium", after="high")],
            ),
            _action(),
        )

        assert fake_soul.remember.await_count == 2


# ---------------------------------------------------------------------------
# InstinctCorrectionsTool — agent-facing shape
# ---------------------------------------------------------------------------


class TestInstinctCorrectionsTool:
    @pytest.mark.asyncio
    async def test_tool_returns_no_corrections_message_when_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from pocketpaw.tools.builtin.instinct_corrections import InstinctCorrectionsTool

        empty_store = InstinctStore(tmp_path / "empty.db")
        monkeypatch.setattr(
            "pocketpaw.tools.builtin.instinct_corrections._get_instinct_store",
            lambda: empty_store,
        )

        tool = InstinctCorrectionsTool()
        result = await tool.execute(pocket_id="pocket-1")
        assert "No corrections captured" in result

    @pytest.mark.asyncio
    async def test_tool_formats_each_correction_with_patches(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from pocketpaw.tools.builtin.instinct_corrections import InstinctCorrectionsTool

        store = InstinctStore(tmp_path / "with_data.db")
        await store.record_correction(
            _correction(
                action_id="act-x",
                patches=[
                    CorrectionPatch(path="title", before="Hi John", after="Hey J"),
                    CorrectionPatch(path="parameters.discount_pct", before=20, after=15),
                ],
            ),
        )
        monkeypatch.setattr(
            "pocketpaw.tools.builtin.instinct_corrections._get_instinct_store",
            lambda: store,
        )

        tool = InstinctCorrectionsTool()
        result = await tool.execute(pocket_id="pocket-1")
        assert "Send renewal outreach" in result
        assert "user:priya" in result
        assert "Hi John" in result
        assert "Hey J" in result
        assert "discount_pct" in result

    @pytest.mark.asyncio
    async def test_tool_returns_enterprise_missing_message_when_ee_unavailable(
        self, monkeypatch
    ) -> None:
        from pocketpaw.tools.builtin.instinct_corrections import InstinctCorrectionsTool

        monkeypatch.setattr(
            "pocketpaw.tools.builtin.instinct_corrections._get_instinct_store",
            lambda: None,
        )

        tool = InstinctCorrectionsTool()
        result = await tool.execute(pocket_id="pocket-1")
        assert "enterprise" in result.lower()

    def test_tool_advertises_required_parameters(self) -> None:
        from pocketpaw.tools.builtin.instinct_corrections import InstinctCorrectionsTool

        tool = InstinctCorrectionsTool()
        assert tool.name == "instinct_corrections"
        schema = tool.parameters
        assert "pocket_id" in schema["properties"]
        assert schema["required"] == ["pocket_id"]
