# tests/cloud/test_belt_stage_events.py — WHICH STAGE a Belt station run has
# reached (feat/belt-entity-events, stage slice).
#
# Created: 2026-09-12. The bus only ever carried ``stage="gate"`` and
# ``stage="done"``, so the console strip could not light orient / develop from
# proven data. These pin the contract that makes it provable:
#   * ``STAGE_ORDER`` — the vocabulary and its ordering, asserted as an exact
#     tuple. It is derived from the ``BeltStage`` Literal, so this also pins
#     that the two cannot drift apart.
#   * ``emit_belt_stage`` — the FORWARD-ONLY guarantee. A stage that does not
#     strictly advance publishes NOTHING and returns None, so the caller's
#     ``prev`` is unchanged. Bus failures are swallowed like every belt emit.
#   * ``maybe_emit_belt_stage`` — the tool filter: BELT surface only, a
#     codebase lookup proves orient, a Write / Edit proves develop, and the
#     first of each is the ONLY one that emits.
#   * REGRESSION PINS on ``gate`` / ``done`` — the two stages that existed
#     before this slice must emit byte-identically, including the exact payload
#     KEY SET (no ``run_id`` leaking into the old shape).
#   * The headless runner's two emits, driven end-to-end through a real
#     InstinctStore with a canned DevelopFn.
#
# The bus is the REAL one (conftest swaps in a RecordingBus) rather than a mock
# of the emitter — over-mocking the seam under test hides live bugs. Only the
# genuinely external things are substituted: the per-stream SSE sink (there is
# no stream in a unit test), the LLM develop loop, and, in the failure test,
# the bus publish itself.
#
# ``pocketpaw_ee`` is import-skipped on an OSS-only install. These tests are
# under ``tests/cloud``, which the root pytest addopts ignores — run them with an
# explicit path: ``uv run pytest tests/cloud/test_belt_stage_events.py -q``.

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt.service import (  # noqa: E402
    STAGE_ORDER,
    emit_belt_run_updated,
    emit_belt_stage,
    maybe_emit_belt_stage,
)

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

_BELT = "belt"
WS = "w1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _SSECapture:
    """Capture push_sse_event calls in place of the real per-stream sink."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, name: str, data: dict) -> None:
        self.events.append((name, data))


@pytest.fixture
def sse(monkeypatch) -> _SSECapture:
    cap = _SSECapture()
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.push_sse_event", cap, raising=True)
    return cap


def _stages(recording_bus) -> list[str]:
    """Every stage value that hit the bus, in order."""
    return [e.data["stage"] for e in recording_bus.events if e.type == "belt_run_updated"]


def _payloads(recording_bus) -> list[dict]:
    return [e.data for e in recording_bus.events if e.type == "belt_run_updated"]


async def _tool(recording_bus, tool_name: str, prev: str | None, **overrides) -> str | None:
    """Drive the bridge for one tool call with BELT defaults."""
    kwargs: dict = {
        "surface": _BELT,
        "tool_name": tool_name,
        "workspace_id": WS,
        "run_id": "run-1",
        "prev": prev,
        **overrides,
    }
    return await maybe_emit_belt_stage(**kwargs)


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_stage_vocabulary_and_its_order() -> None:
    """The shipped vocabulary, pinned exactly. ``station``, ``gate`` and ``done``
    predate this slice and must keep both their spelling and their position —
    the order is the forward-only comparison key, so moving a value silently
    changes which transitions are legal."""
    assert STAGE_ORDER == ("station", "orient", "develop", "verify", "gate", "done")


def test_stage_order_is_derived_from_the_literal() -> None:
    """One definition, not two. If someone adds a value to the ``BeltStage``
    Literal without touching ``STAGE_ORDER`` (or vice versa), they are the same
    object and cannot disagree."""
    from typing import get_args

    from pocketpaw_ee.cloud.belt.service import BeltStage

    assert get_args(BeltStage) == STAGE_ORDER


# ---------------------------------------------------------------------------
# emit_belt_stage — forward-only
# ---------------------------------------------------------------------------


async def test_a_stage_advances_and_returns_itself(recording_bus, sse) -> None:
    assert await emit_belt_stage(workspace_id=WS, stage="orient", prev=None) == "orient"
    assert await emit_belt_stage(workspace_id=WS, stage="develop", prev="orient") == "develop"
    assert _stages(recording_bus) == ["orient", "develop"]


@pytest.mark.parametrize(
    ("stage", "prev"),
    [
        ("orient", "develop"),  # strictly backwards
        ("develop", "develop"),  # a duplicate
        ("station", "done"),  # the whole way back from a terminal
        ("gate", "done"),  # the real ordering risk: a late gate after a terminal
        ("orient", "verify"),
    ],
)
async def test_a_backwards_or_duplicate_emit_is_ignored(
    recording_bus, sse, stage: str, prev: str
) -> None:
    """A late or duplicate emit must not walk a run backwards in the event
    stream. Nothing is published and None comes back, so the caller keeps the
    ``prev`` it already had."""
    assert await emit_belt_stage(workspace_id=WS, stage=stage, prev=prev) is None
    assert _stages(recording_bus) == []


async def test_an_unknown_prev_does_not_block_a_real_stage(recording_bus, sse) -> None:
    """Forward-compat: a ``prev`` this build doesn't know sorts as "before
    everything" rather than swallowing every subsequent emit."""
    assert await emit_belt_stage(workspace_id=WS, stage="develop", prev="nonsense") == "develop"
    assert _stages(recording_bus) == ["develop"]


async def test_no_workspace_emits_nothing(recording_bus, sse) -> None:
    """Tenancy drives the fan-out; without it the event reaches nobody, so it is
    not published at all."""
    assert await emit_belt_stage(workspace_id="", stage="orient", prev=None) is None
    assert _stages(recording_bus) == []


async def test_the_stage_payload_has_a_fixed_shape(recording_bus, sse) -> None:
    """Every documented key present, including the Nones, so a consumer reads one
    shape instead of probing for optional keys."""
    await emit_belt_stage(
        workspace_id=WS, stage="verify", prev="develop", run_id="run-9", action_id="a-9"
    )
    (payload,) = _payloads(recording_bus)
    assert payload == {
        "workspace_id": WS,
        "action_id": "a-9",
        "run_id": "run-9",
        "status": None,
        "stage": "verify",
    }
    # The secondary in-turn path mirrors it.
    assert sse.events == [("belt_run_updated", payload)]


async def test_status_is_never_invented(recording_bus, sse) -> None:
    """The interactive station has no Action row, so it has no lifecycle status.
    Fabricating one (a "working", say) would make the console light a stage the
    data cannot prove — the exact failure this slice exists to avoid."""
    await emit_belt_stage(workspace_id=WS, stage="develop", prev=None, run_id="run-1")
    (payload,) = _payloads(recording_bus)
    assert payload["status"] is None
    assert payload["action_id"] is None


async def test_bus_failure_does_not_raise(recording_bus, sse, monkeypatch) -> None:
    """A dead bus must not take a station run down with it."""

    async def _boom(event) -> None:
        raise RuntimeError("bus is down")

    monkeypatch.setattr("pocketpaw_ee.cloud._core.realtime.emit.emit", _boom, raising=True)

    assert await emit_belt_stage(workspace_id=WS, stage="orient", prev=None) == "orient"
    assert _stages(recording_bus) == []
    # The secondary path still ran — one dead transport doesn't disable the other.
    assert len(sse.events) == 1


async def test_bridge_survives_a_failing_emit(recording_bus, sse, monkeypatch) -> None:
    """Same guarantee one level out: a tool_use event can never abort the turn
    that produced it, and the caller's ``prev`` survives intact."""

    async def _boom(**kwargs) -> None:
        raise RuntimeError("emit exploded")

    monkeypatch.setattr("pocketpaw_ee.cloud.belt.service.emit_belt_stage", _boom, raising=True)
    assert await _tool(recording_bus, "Write", "orient") == "orient"


# ---------------------------------------------------------------------------
# maybe_emit_belt_stage — the tool filter
# ---------------------------------------------------------------------------


async def test_first_write_emits_develop_exactly_once(recording_bus, sse) -> None:
    """The headline contract: a run that writes ten files reports ``develop``
    ONCE, not ten times. The caller threads the returned stage back in, and the
    forward-only guard drops every repeat."""
    stage: str | None = None
    for _ in range(5):
        stage = await _tool(recording_bus, "Write", stage)
    assert _stages(recording_bus) == ["develop"]
    assert stage == "develop"


async def test_edit_also_proves_develop(recording_bus, sse) -> None:
    assert await _tool(recording_bus, "Edit", None) == "develop"
    assert _stages(recording_bus) == ["develop"]


async def test_orient_is_emitted_before_develop(recording_bus, sse) -> None:
    """The ordering the strip renders: a run reads the codebase, then changes
    it. Both stages are reported, in that order, once each."""
    stage = await _tool(recording_bus, "Read", None)
    stage = await _tool(recording_bus, "Grep", stage)
    stage = await _tool(recording_bus, "Write", stage)
    stage = await _tool(recording_bus, "Write", stage)
    assert _stages(recording_bus) == ["orient", "develop"]


async def test_a_read_after_a_write_does_not_regress_the_run(recording_bus, sse) -> None:
    """This is what makes the orient heuristic defensible. Reading a file is
    what orientation DOES, but it is the same tool call mid-development — so
    ``orient`` can only ever be claimed BEFORE the first write."""
    stage = await _tool(recording_bus, "Write", None)
    stage = await _tool(recording_bus, "Read", stage)
    stage = await _tool(recording_bus, "Glob", stage)
    assert _stages(recording_bus) == ["develop"]
    assert stage == "develop"


@pytest.mark.parametrize(
    "tool_name",
    ["mcp__loom__orient", "mcp__loom__locate", "mcp__loom__what_depends_on"],
)
async def test_a_loom_call_proves_orient(recording_bus, sse, tool_name: str) -> None:
    """loom is the workspace's orientation core — any of its tools is an
    explicit orient step. Matched by prefix because the names are namespaced."""
    assert await _tool(recording_bus, tool_name, None) == "orient"
    assert _stages(recording_bus) == ["orient"]


@pytest.mark.parametrize(
    "tool_name",
    ["Bash", "WebFetch", "TodoWrite", "mcp__pocketpaw_belt__belt_propose_change", "", None],
)
async def test_a_tool_that_proves_nothing_emits_nothing(recording_bus, sse, tool_name) -> None:
    """A stage we cannot prove is one we do not emit. Bash in particular could
    be anything — a build, a test run, a sed -i — so it claims no stage."""
    assert await _tool(recording_bus, tool_name, None) is None
    assert _stages(recording_bus) == []


@pytest.mark.parametrize("surface", [None, "chat", "home", "code", "pocket", "sites"])
async def test_a_non_belt_run_emits_no_stage_events(recording_bus, sse, surface) -> None:
    """The /code surface has Write and Edit too. Only belt runs report stages —
    and the caller's ``prev`` comes back untouched."""
    assert await _tool(recording_bus, "Write", None, surface=surface) is None
    assert _stages(recording_bus) == []


async def test_no_workspace_on_the_bridge_emits_nothing(recording_bus, sse) -> None:
    assert await _tool(recording_bus, "Write", None, workspace_id=None) is None
    assert _stages(recording_bus) == []


# ---------------------------------------------------------------------------
# REGRESSION PINS — gate / done must be byte-identical to before this slice
# ---------------------------------------------------------------------------


async def test_gate_emit_is_unchanged(recording_bus, sse) -> None:
    """``belt_propose_change`` emits exactly this. The KEY SET is pinned too, so
    the new ``run_id`` field cannot leak into the old payload shape and change
    what an existing consumer parses."""
    await emit_belt_run_updated(workspace_id=WS, action_id="a-1", status="proposed", stage="gate")
    (payload,) = _payloads(recording_bus)
    assert payload == {
        "workspace_id": WS,
        "action_id": "a-1",
        "status": "proposed",
        "stage": "gate",
    }


async def test_done_emit_is_unchanged(recording_bus, sse) -> None:
    """The executor's terminals. ``pr_url`` is still present only when set."""
    await emit_belt_run_updated(
        workspace_id=WS,
        action_id="a-2",
        status="landed",
        stage="done",
        pr_url="https://example.test/pr/1",
    )
    await emit_belt_run_updated(workspace_id=WS, action_id="a-3", status="failed", stage="done")
    landed, failed = _payloads(recording_bus)
    assert landed == {
        "workspace_id": WS,
        "action_id": "a-2",
        "status": "landed",
        "stage": "done",
        "pr_url": "https://example.test/pr/1",
    }
    assert failed == {
        "workspace_id": WS,
        "action_id": "a-3",
        "status": "failed",
        "stage": "done",
    }


async def test_the_lifecycle_emitter_has_no_forward_only_guard(recording_bus, sse) -> None:
    """``emit_belt_run_updated`` is NOT gated — approve/reject/execute fire
    independently and must each publish. The guard belongs to the intermediate
    stages only; adding it here would silently drop a terminal event."""
    await emit_belt_run_updated(workspace_id=WS, action_id="a-1", status="proposed", stage="gate")
    await emit_belt_run_updated(workspace_id=WS, action_id="a-1", status="landed", stage="done")
    await emit_belt_run_updated(workspace_id=WS, action_id="a-1", status="approved", stage="gate")
    assert _stages(recording_bus) == ["gate", "done", "gate"]


# ---------------------------------------------------------------------------
# The headless path — the ONE path that carries a real action_id
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore wired into the global resolver the runner reads."""
    st = InstinctStore(tmp_path / "instinct_stage.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


async def _queue_station_run(store: InstinctStore) -> str:
    """File a QUEUED code_change run the way the StationTaskDispatcher does."""
    import pocketpaw_ee.cloud.mandates.executor as ex
    from pocketpaw_ee.cloud.mandates.executor import StationTaskDispatcher

    async def _fake_repo(workspace_id: str, mandate_id: str) -> str | None:
        return "demo-repo"

    orig = ex._repo_for_mandate
    ex._repo_for_mandate = _fake_repo  # type: ignore[assignment]
    try:
        return await StationTaskDispatcher().dispatch(
            workspace_id=WS,
            mandate_id="m1",
            shift_no=1,
            plan_action_id="plan-act-1",
            index=1,
            task={
                "title": "Add a hello file",
                "why": "exercise the stage emits",
                "expected_outcome": "hello.txt exists",
                "requested_by": "u1",
            },
        )
    finally:
        ex._repo_for_mandate = orig  # type: ignore[assignment]


CANNED_DIFF = """\
diff --git a/hello.txt b/hello.txt
index e69de29..3b18e51 100644
--- a/hello.txt
+++ b/hello.txt
@@ -0,0 +1 @@
+hello
"""


async def test_headless_run_emits_orient_then_develop(store, recording_bus, sse) -> None:
    """Driven end-to-end through a real store with a canned develop loop. This
    is the path that can carry a real ``action_id``, so these transitions patch
    a run card the console already lists."""
    from pocketpaw_ee.cloud.belt.headless import (
        DevelopRequest,
        DevelopResult,
        HeadlessDevelopRunner,
    )

    action_id = await _queue_station_run(store)
    recording_bus.events.clear()  # ignore whatever the dispatch itself emitted

    seen_stages_at_develop_time: list[str] = []

    async def _develop(request: DevelopRequest) -> DevelopResult:
        # Assert INSIDE the develop loop: ``develop`` must already have been
        # reported when the long step starts, not after it returns.
        seen_stages_at_develop_time.extend(_stages(recording_bus))
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="added hello")

    await HeadlessDevelopRunner(develop_fn=_develop).run(action_id, workspace_id=WS)

    assert seen_stages_at_develop_time == ["orient", "develop"]
    stage_events = [p for p in _payloads(recording_bus) if p.get("stage") in ("orient", "develop")]
    assert [p["stage"] for p in stage_events] == ["orient", "develop"]
    # The real Action id rides along — this is the only develop path that has one.
    assert {p["action_id"] for p in stage_events} == {action_id}
    # Status stays honest: the Action row IS still queued until the diff lands.
    assert {p["status"] for p in stage_events} == {"queued"}


async def test_headless_emits_nothing_for_a_run_it_refuses(store, recording_bus, sse) -> None:
    """A run that is not ours to develop (already carries a diff) must not
    report stages it never reached — the emits sit AFTER the guard clause."""
    from pocketpaw_ee.cloud.belt.headless import (
        DevelopRequest,
        DevelopResult,
        HeadlessDevelopRunner,
    )

    action_id = await _queue_station_run(store)

    async def _develop(request: DevelopRequest) -> DevelopResult:
        return DevelopResult(diff=CANNED_DIFF, base_branch="main")

    runner = HeadlessDevelopRunner(develop_fn=_develop)
    await runner.run(action_id, workspace_id=WS)
    recording_bus.events.clear()

    # Second run: the action now carries a diff, so the runner skips it.
    await runner.run(action_id, workspace_id=WS)
    assert _stages(recording_bus) == []


async def test_headless_develop_failure_still_reported_the_stages(
    store, recording_bus, sse
) -> None:
    """The stages record what the run REACHED, not whether it succeeded. A run
    that oriented and started developing did both, even if the loop then blew
    up — the failure is carried by the blob's note, not by rewriting history."""
    from pocketpaw_ee.cloud.belt.headless import DevelopRequest, HeadlessDevelopRunner

    action_id = await _queue_station_run(store)
    recording_bus.events.clear()

    async def _boom(request: DevelopRequest):
        raise RuntimeError("the develop loop fell over")

    await HeadlessDevelopRunner(develop_fn=_boom).run(action_id, workspace_id=WS)
    assert _stages(recording_bus) == ["orient", "develop"]


# ---------------------------------------------------------------------------
# The call site
# ---------------------------------------------------------------------------


def test_run_core_calls_the_stage_bridge() -> None:
    """The bridge is only useful if the agent stream reaches it, and nothing
    else in this file would notice its removal. A source assertion is the honest
    cheap guard: it catches a refactor that drops the call, and it does NOT
    prove the arguments run_core passes are right."""
    from pocketpaw_ee.cloud.chat.runs import run_core

    src = inspect.getsource(run_core)
    assert "maybe_emit_belt_stage" in src
    # The forward-only guard is a per-run LOCAL, not shared state. If this name
    # stops being threaded back in, every tool call re-emits.
    assert "prev=belt_stage" in src
    assert "belt_stage = await maybe_emit_belt_stage" in src
