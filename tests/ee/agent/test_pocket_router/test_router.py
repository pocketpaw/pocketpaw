# test_router.py — Tests for the pocket execution router's dispatch +
#   observability (Increment 3).
# Updated: 2026-06-10 (W2c — surface Tier-0 agent-parked writes for
#   approval) — adds the deny-by-default park-routing tests at the bottom:
#   a Tier-0 write whose binding OMITS ``requires_instinct`` (the agent
#   default) now PARKS at the executor's gate and the router routes it into
#   the Instinct approval queue, so ``store.pending()`` shows a real PENDING
#   Action a human can see — not just a "needs approval" string. An
#   explicitly-exempt action still fires through Tier-0. These drive the
#   REAL ``action_executor.run_action`` end-to-end against an isolated
#   InstinctStore.
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
                return_value=(
                    "https://api.example.com",
                    "bearer",
                    None,
                    "tok",
                    [],
                    None,
                    "http",
                    None,
                )
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
                    return_value=(
                        "https://api.example.com",
                        "bearer",
                        None,
                        "tok",
                        [],
                        None,
                        "http",
                        None,
                    )
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
                return_value=(
                    "https://api.example.com",
                    "bearer",
                    None,
                    "tok",
                    [],
                    None,
                    "http",
                    None,
                )
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
                return_value=(
                    "https://api.example.com",
                    "bearer",
                    None,
                    "tok",
                    [],
                    None,
                    "http",
                    None,
                )
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


# ---------------------------------------------------------------------------
# W2c — Tier-0 deny-by-default: a parked agent write reaches the approval queue
# ---------------------------------------------------------------------------
#
# A pocket whose write binding OMITS `requires_instinct` is the agent's
# default authoring shape. The classifier reads the RAW dict, sees no
# `requires_instinct` key, and routes it Tier-0 (auto-fire). Under W2a
# deny-by-default the executor PARKS it (the binding defaults
# requires_instinct=True). W2c routes that park into the Instinct approval
# queue, so a human sees a PENDING Action. An explicitly-exempt binding
# (`instinct_exempt=True, requires_instinct=False`) still fires.

# A write action with NO `requires_instinct` key — the classifier waves it
# through Tier-0 (it reads the raw dict), the executor parks it (deny-by-
# default). `save_lease` + intent "save lease" matches the action verb +
# key; static params resolve.
_SPEC_DENY_BY_DEFAULT_ACTION = {
    "version": "1.0",
    "actions": {
        "save_lease": {
            "kind": "write_binding",
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {"rent": 2000},
        }
    },
    "state": {},
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}

# Same shape, but the binding is EXPLICITLY exempt — it should fire through
# Tier-0 (no park) exactly like the legacy opt-in path.
_SPEC_EXEMPT_ACTION = {
    "version": "1.0",
    "actions": {
        "save_lease": {
            "kind": "write_binding",
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {"rent": 2000},
            "instinct_exempt": True,
            "requires_instinct": False,
        }
    },
    "state": {},
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}

_ALLOWLIST = [{"method": "POST", "path_pattern": "/leases/*/renew"}]


def _public_dns(monkeypatch) -> None:
    """Make every hostname resolve to a public IP so the executor's DNS
    pre-resolve guard (gate 6) passes — it runs BEFORE the gate-7 park."""

    def _fake_getaddrinfo(host, *_a, **_k):  # noqa: ANN001, ANN002, ANN003
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo)


def _isolated_instinct_store(monkeypatch, tmp_path):
    """Wire an isolated ``InstinctStore`` into the bridge.

    ``instinct_bridge.propose_pocket_write`` lazy-imports
    ``get_instinct_store`` from ``pocketpaw.stores`` — patch it there so the
    proposed Action lands in a temp-backed store this test can query and
    never touches ``~/.pocketpaw/instinct.db``."""
    from pocketpaw.instinct.store import InstinctStore

    st = InstinctStore(tmp_path / "w2c_router_instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.mark.asyncio
async def test_tier0_deny_by_default_write_lands_in_instinct_pending(monkeypatch, tmp_path):
    """W2c — a Tier-0 agent write whose binding OMITS `requires_instinct`
    is PARKED by the executor (deny-by-default) and the router routes it
    into the Instinct approval queue. The proposal is visible via
    `store.pending()` — a REAL PENDING Action, not just a 'needs approval'
    string. The router reports it as handled-but-pending (no auto-fire, no
    escalation to the specialist)."""
    from pocketpaw_ee.cloud.pockets import action_executor

    from pocketpaw.instinct.models import ActionStatus

    _public_dns(monkeypatch)
    action_executor._action_log.clear()
    store = _isolated_instinct_store(monkeypatch, tmp_path)

    # No HTTP must be attempted — the write parks before gate 8.
    http_hit = {"called": False}

    def _no_http(*_a, **_k):  # noqa: ANN002, ANN003
        http_hit["called"] = True
        raise AssertionError("a parked write must NOT make an HTTP call")

    monkeypatch.setattr(action_executor, "_do_request", _no_http)

    pocket_wire = {
        "_id": "507f1f77bcf86cd799439011",
        "workspace": "w1",
        "name": "Leases",
        "owner": "u1",
    }

    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
            new=AsyncMock(
                return_value=(
                    "https://api.example.com",
                    "none",
                    None,
                    "",
                    _ALLOWLIST,
                    None,
                    "http",
                    None,
                )
            ),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.has_action_run_access",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=pocket_wire),
        ),
    ):
        handled, output = await classify_and_route(
            _EditInput("save lease", pocket={"rippleSpec": _SPEC_DENY_BY_DEFAULT_ACTION}),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )

    # Handled (no escalation to the specialist) but NOT applied — the write
    # is pending approval, not fired.
    assert handled is True
    assert output is not None
    assert output.ok is False
    assert output.action == "instinct_pending"
    assert output.ops == []
    assert http_hit["called"] is False

    # The PROOF: a real PENDING Instinct Action now exists for this pocket.
    pending = await store.pending(pocket_id="507f1f77bcf86cd799439011")
    assert len(pending) == 1, f"expected one pending proposal, got {pending}"
    action = pending[0]
    assert action.status == ActionStatus.PENDING
    blob = action.parameters["_pocket_write"]
    assert blob["action"] == "save_lease"
    assert blob["method"] == "POST"
    assert blob["path"] == "/leases/42/renew"
    assert blob["params"] == {"rent": 2000}
    # The pending id round-trips on the output so the agent can deep-link it.
    assert action.id in output.backend_used


@pytest.mark.asyncio
async def test_tier0_explicitly_exempt_write_still_fires(monkeypatch, tmp_path):
    """W2c — an explicitly-exempt binding
    (`instinct_exempt=True, requires_instinct=False`) is NOT parked: it
    fires through Tier-0 and the router returns an applied output. No
    Instinct proposal is created."""
    from pocketpaw_ee.cloud.pockets import action_executor

    _public_dns(monkeypatch)
    action_executor._action_log.clear()
    store = _isolated_instinct_store(monkeypatch, tmp_path)

    http_hit = {"called": False}

    def _ok_http(*_a, **_k):  # noqa: ANN002, ANN003
        http_hit["called"] = True
        return {"status": 200, "response": {}}

    monkeypatch.setattr(action_executor, "_do_request", AsyncMock(side_effect=_ok_http))

    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend_for_executor",
            new=AsyncMock(
                return_value=(
                    "https://api.example.com",
                    "none",
                    None,
                    "",
                    _ALLOWLIST,
                    None,
                    "http",
                    None,
                )
            ),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.has_action_run_access",
            new=AsyncMock(return_value=True),
        ),
    ):
        handled, output = await classify_and_route(
            _EditInput("save lease", pocket={"rippleSpec": _SPEC_EXEMPT_ACTION}),
            workspace_id="w1",
            user_id="u1",
            settings=_Settings(),
        )

    assert handled is True
    assert output is not None
    assert output.ok is True
    assert output.action == "applied"
    assert http_hit["called"] is True

    # An exempt fire creates NO pending proposal.
    pending = await store.pending(pocket_id="507f1f77bcf86cd799439011")
    assert pending == [], f"an exempt write must not create a proposal: {pending}"
