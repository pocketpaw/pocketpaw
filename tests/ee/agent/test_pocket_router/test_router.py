# test_router.py — Tests for the pocket execution router's dispatch +
#   observability (Increment 3).
# Created: 2026-05-22 — pins the router contract: a Tier-0 verdict invokes
#   the EXISTING executor (it does not reimplement it) and emits a
#   ``pocket_execution`` SSE frame with ``tokens:{0,0}`` and the layout /
#   render stages marked skipped; a Tier-2 verdict escalates (handled is
#   False); and the kill-switch (``pocket_router_enabled=false``) makes
#   every request escalate. The classifier itself is covered exhaustively
#   in test_classifier.py — here we exercise routing, not classification.
"""Dispatch + observability tests for the pocket execution router."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.agent.pocket_router.router import classify_and_route


class _Settings:
    """Minimal settings stand-in — only the two router fields matter."""

    def __init__(self, enabled: bool = True, min_confidence: float = 0.9) -> None:
        self.pocket_router_enabled = enabled
        self.pocket_router_min_confidence = min_confidence
        # The Tier-1 path hands settings to EditAgentModeAdapter; that
        # adapter ignores backend/model, but keep the field present.
        self.pocket_specialist_mode = "agent"


class _EditInput:
    """Stand-in for ``PocketSpecialistEditInput`` — duck-typed; the router
    only reads ``pocket_id`` / ``intent`` / ``pocket`` / ``target_node_ids``."""

    def __init__(self, intent: str, pocket: dict | None = None) -> None:
        self.pocket_id = "507f1f77bcf86cd799439011"
        self.intent = intent
        self.pocket = pocket
        self.target_node_ids = None
        self.ops = None


_SPEC_WITH_SOURCE = {
    "version": "1.0",
    "sources": {"prs": {"method": "GET", "path": "/pulls", "bind": "state.prs"}},
    "state": {"prs": []},
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}


def _drain(sink: asyncio.Queue) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_makes_everything_escalate():
    """``pocket_router_enabled=false`` -> every request escalates without
    even classifying. The router returns ``(False, None)`` so the caller
    falls through to the specialist — today's behaviour exactly."""
    # An intent that WOULD be Tier 0 if the router were enabled.
    input = _EditInput("refresh the prs source", pocket={"rippleSpec": _SPEC_WITH_SOURCE})
    handled, output = await classify_and_route(
        input,
        workspace_id="w1",
        user_id="u1",
        settings=_Settings(enabled=False),
    )
    assert handled is False
    assert output is None


@pytest.mark.asyncio
async def test_kill_switch_emits_tier2_execution_frame():
    """Even with the switch off the router still emits its observability
    frame — a kill-switch escalation is traced as Tier 2."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )

    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        await classify_and_route(
            _EditInput("refresh the prs source", pocket={"rippleSpec": _SPEC_WITH_SOURCE}),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(enabled=False),
        )
    finally:
        detach_sse_event_sink(token)

    events = _drain(sink)
    names = [n for n, _ in events]
    assert "pocket_execution" in names
    _, frame = next(e for e in events if e[0] == "pocket_execution")
    assert frame["tier_chosen"] == 2
    assert frame["tokens"] == {"prompt": 0, "completion": 0}


# ---------------------------------------------------------------------------
# Tier 2 escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structural_intent_escalates_without_running_executor():
    """A structural intent classifies as Tier 2 — the router escalates
    and never touches an executor."""
    with patch(
        "pocketpaw_ee.cloud.pockets.source_executor.run_sources",
        new=AsyncMock(),
    ) as run_sources:
        handled, output = await classify_and_route(
            _EditInput("add a chart widget", pocket={"rippleSpec": _SPEC_WITH_SOURCE}),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )
    assert handled is False
    assert output is None
    run_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_sub_threshold_confidence_escalates():
    """A cheap-tier verdict whose confidence is below the configured
    floor escalates — the fail-safe gate. Tier-0 refresh scores ~0.97;
    a floor of 0.99 trips it."""
    handled, output = await classify_and_route(
        _EditInput("refresh the prs source", pocket={"rippleSpec": _SPEC_WITH_SOURCE}),
        workspace_id="w1",
        user_id="u1",
        settings=_Settings(min_confidence=0.99),
    )
    assert handled is False
    assert output is None


# ---------------------------------------------------------------------------
# Tier 0 — declarative: invokes the EXISTING executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier0_invokes_source_executor_and_handles():
    """A Tier-0 refresh routes to ``source_executor.run_sources`` — the
    router INVOKES the executor, it does not reimplement it. On success
    the router returns ``(True, output)`` so the caller skips the
    specialist."""
    fake_run = AsyncMock(return_value={"ran": [{"source": "prs", "value": []}], "errors": []})
    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
            new=AsyncMock(
                return_value=("https://api.example.com", "bearer", None, "tok", [], None)
            ),
        ),
        patch("pocketpaw_ee.cloud.pockets.source_executor.run_sources", new=fake_run),
    ):
        handled, output = await classify_and_route(
            _EditInput("refresh the prs source", pocket={"rippleSpec": _SPEC_WITH_SOURCE}),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )
    assert handled is True
    assert output is not None
    assert output.ok is True
    assert output.backend_used == "pocket_router:tier0"
    # The router passed the named source straight through to the executor.
    fake_run.assert_awaited_once()
    assert fake_run.await_args.kwargs["only_source"] == "prs"
    assert fake_run.await_args.kwargs["pocket_id"] == "507f1f77bcf86cd799439011"


@pytest.mark.asyncio
async def test_tier0_emits_execution_frame_with_zero_tokens_and_skipped_stages():
    """A Tier-0 route emits one ``pocket_execution`` frame: ``tokens:{0,0}``
    and the ``layout_build`` / ``widget_render`` stages marked
    ``ran:false`` with reason 'data-only change' — the Thesys readout."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )

    sink: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(sink)
    try:
        with (
            patch(
                "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
                new=AsyncMock(
                    return_value=("https://api.example.com", "bearer", None, "tok", [], None)
                ),
            ),
            patch(
                "pocketpaw_ee.cloud.pockets.source_executor.run_sources",
                new=AsyncMock(return_value={"ran": [], "errors": []}),
            ),
        ):
            handled, _ = await classify_and_route(
                _EditInput("refresh prs", pocket={"rippleSpec": _SPEC_WITH_SOURCE}),
                workspace_id="w1",
                user_id="u1",
                settings=_Settings(),
            )
    finally:
        detach_sse_event_sink(token)

    assert handled is True
    events = _drain(sink)
    exec_frames = [d for n, d in events if n == "pocket_execution"]
    assert len(exec_frames) == 1, f"expected exactly one pocket_execution frame: {events}"
    frame = exec_frames[0]
    assert frame["tier_chosen"] == 0
    assert frame["tokens"] == {"prompt": 0, "completion": 0}

    stages = {s["stage"]: s for s in frame["stages"]}
    assert stages["layout_build"]["ran"] is False
    assert stages["layout_build"]["skipped_reason"] == "data-only change"
    assert stages["widget_render"]["ran"] is False
    assert stages["widget_render"]["skipped_reason"] == "data-only change"
    # classify + apply both ran.
    assert stages["classify"]["ran"] is True
    assert stages["apply"]["ran"] is True


@pytest.mark.asyncio
async def test_tier0_source_errors_escalate():
    """A Tier-0 attempt that the executor reports as errored escalates —
    the specialist can still satisfy the intent."""
    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
            new=AsyncMock(
                return_value=("https://api.example.com", "bearer", None, "tok", [], None)
            ),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.source_executor.run_sources",
            new=AsyncMock(return_value={"ran": [], "errors": [{"source": "prs", "error": "boom"}]}),
        ),
    ):
        handled, output = await classify_and_route(
            _EditInput("refresh prs", pocket={"rippleSpec": _SPEC_WITH_SOURCE}),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )
    assert handled is False
    assert output is None


@pytest.mark.asyncio
async def test_tier0_no_backend_escalates():
    """A Tier-0 verdict on a pocket with no backend configured cannot
    run declaratively — it escalates rather than crashes."""
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
        new=AsyncMock(return_value=None),
    ):
        handled, output = await classify_and_route(
            _EditInput("refresh prs", pocket={"rippleSpec": _SPEC_WITH_SOURCE}),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )
    assert handled is False
    assert output is None


# ---------------------------------------------------------------------------
# ripple_spec resolution fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_resolves_spec_via_agent_view_when_pocket_absent():
    """When the caller does not hand a pocket view, the router reads the
    spec via ``agent_view`` — and still classifies + routes correctly."""
    fake_run = AsyncMock(return_value={"ran": [], "errors": []})
    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.agent_view",
            new=AsyncMock(return_value=({"rippleSpec": _SPEC_WITH_SOURCE}, None)),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
            new=AsyncMock(
                return_value=("https://api.example.com", "bearer", None, "tok", [], None)
            ),
        ),
        patch("pocketpaw_ee.cloud.pockets.source_executor.run_sources", new=fake_run),
    ):
        handled, output = await classify_and_route(
            _EditInput("refresh prs", pocket=None),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )
    assert handled is True
    assert output.ok is True


# ---------------------------------------------------------------------------
# Tier 1 — deterministic op: end-to-end through the op-apply path
# ---------------------------------------------------------------------------


_SPEC_TASKS = {
    "version": "1.0",
    "state": {
        "tasks": [
            {"id": 1, "label": "buy milk", "status": "todo"},
            {"id": 2, "label": "walk dog", "status": "todo"},
        ],
        "filter": "all",
    },
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}


@pytest.fixture
def recording_bus():
    """Install an in-memory realtime bus for the duration of a test.

    The granular ops the Tier-1 path drives emit ``PocketUpdated`` via
    the realtime bus, which is only wired by ``init_realtime`` in a live
    process. ``tests/cloud`` installs this autouse; ``tests/ee`` does
    not, so the router's end-to-end test installs its own."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list = []

        async def publish(self, event) -> None:  # noqa: ANN001
            self.events.append(event)

        def subscribe(self, event_type, handler) -> None:  # noqa: ANN001, ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    try:
        yield rec
    finally:
        bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tier1_applies_one_op_end_to_end(beanie_test_db, recording_bus):
    """A Tier-1 'mark task done' verdict routes through the existing
    op-apply path and persists the single ``set_state`` op against a real
    Pocket — no LLM runs, the router returns ``(True, output)``."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_agent_identity,
        attach_sse_event_sink,
        detach_agent_identity,
        detach_sse_event_sink,
    )
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace="w1",
        name="Tasks",
        owner="u1",
        visibility="workspace",
        rippleSpec=dict(_SPEC_TASKS),
    )
    await doc.insert()
    pocket_id = str(doc.id)

    sink: asyncio.Queue = asyncio.Queue()
    id_tokens = attach_agent_identity(workspace_id="w1", user_id="u1")
    sink_token = attach_sse_event_sink(sink)
    try:
        input = _EditInput("mark task 1 as done")
        input.pocket_id = pocket_id
        handled, output = await classify_and_route(
            input,
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )
    finally:
        detach_sse_event_sink(sink_token)
        detach_agent_identity(id_tokens)

    assert handled is True, f"Tier-1 should handle 'mark task 1 done': {output}"
    assert output.ok is True
    assert len(output.ops) == 1

    # The op persisted: task 1 (index 0) is now done.
    refreshed = await Pocket.get(doc.id)
    assert refreshed.rippleSpec["state"]["tasks"][0]["status"] == "done"
    assert refreshed.rippleSpec["state"]["tasks"][1]["status"] == "todo"  # untouched

    # The execution frame says Tier 1, zero tokens, layout/render skipped.
    events = _drain(sink)
    exec_frames = [d for n, d in events if n == "pocket_execution"]
    assert len(exec_frames) == 1
    frame = exec_frames[0]
    assert frame["tier_chosen"] == 1
    assert frame["tokens"] == {"prompt": 0, "completion": 0}
    stages = {s["stage"]: s for s in frame["stages"]}
    assert stages["layout_build"]["ran"] is False
    assert stages["widget_render"]["ran"] is False
