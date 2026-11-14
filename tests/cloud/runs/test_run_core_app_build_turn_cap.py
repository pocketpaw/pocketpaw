# tests/cloud/runs/test_run_core_app_build_turn_cap.py — an approved Sites app plan
# gets an uncapped turn to build every milestone, and an honest stop when a bound
# (a free run's) still cuts it short.
#
# Pins, in run_core and the PLAN.md reader (``sites/app_plan.py``):
#   * ``next_milestone`` finds the first unticked milestone in the shapes agents
#     write (nested items under an M-line, M-headings, flat items), None when done;
#   * a /sites PROJECT turn whose PLAN.md has unticked milestones resolves a
#     profile with ``max_turns = 0`` (no cap) by default, or an operator's
#     ``sites_app_build_max_turns`` N > 0; a finished plan, no plan, another
#     engine or another surface leave it None (no override);
#   * the override, 0 included, reaches ``pool.run`` and ``pool.prewarm`` as
#     ``max_turns`` only when set;
#   * a run stopped by its turn limit (or the free run's usage limit) mid-plan ends
#     with an error that names the next milestone; any other error is unchanged.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, SurfaceMeta
from pocketpaw_ee.cloud.surface.domain import SurfaceProfile
from pocketpaw_ee.cloud.surface.service import compose_entity_profile
from pocketpaw_ee.sites.app_plan import next_milestone, next_milestone_in

from pocketpaw.agents.claude_sdk import _result_error_text

NESTED = """# Plan
## 9. Milestones
- [x] **M1 Core loop**
  - [x] player moves
  - [x] restart works
- [ ] **M2 World and assets**
  - [ ] level geometry
- [ ] M3 Feel
"""

HEADINGS = """## Milestones
### M1 Core loop
- [x] movement
- [x] lose state
### M2 World and assets
- [x] lighting
- [ ] real models
"""

DONE = """## Milestones
- [x] M1 Core loop
- [x] M2 World
- [x] M3 Feel
- [x] M4 Polish
"""


def test_next_milestone_reads_the_shapes_agents_write() -> None:
    assert next_milestone(NESTED) == "M2 World and assets"
    assert next_milestone(HEADINGS) == "M2 World and assets"
    assert next_milestone("- [x] a\n- [ ] add sound\n") == "add sound"
    assert next_milestone(DONE) is None
    assert next_milestone("# Plan\nno checklist yet\n") is None
    assert next_milestone_in({"PLAN.md": NESTED}) == "M2 World and assets"
    assert next_milestone_in({"index.html": "<p>"}) is None
    assert next_milestone_in(None) is None


def _sites_ctx(pocket_id: str | None = "p1", kind: SurfaceKind = SurfaceKind.SITES):
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        surface_context=SurfaceContext(
            workspace_id="w1",
            user_id="u1",
            kind=kind,
            meta=SurfaceMeta(route_path="/sites", pocket_id=pocket_id),
            preamble="",
        ),
    )


def _pocket(plan: str | None, engine: str = "project") -> Any:
    source = {"package.json": "{}"}
    if plan is not None:
        source["PLAN.md"] = plan
    return SimpleNamespace(engine=engine, source=source, workspace="w1")


async def _resolve(monkeypatch, pocket: Any, ctx: ScopeContext | None = None) -> SurfaceProfile:
    async def _fake_pocket(_ctx):
        return pocket

    async def _no_override(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(run_core, "_sites_refine_pocket", _fake_pocket)
    monkeypatch.setattr(run_core, "_load_entity_profile_override", _no_override)
    return await run_core._resolve_entity_profile(ctx or _sites_ctx())


@pytest.mark.asyncio
async def test_an_unfinished_app_plan_lifts_the_turn_cap(monkeypatch) -> None:
    from pocketpaw.config import get_settings

    # 0 = no cap: the agent builds the whole approved plan in this turn.
    assert get_settings().sites_app_build_max_turns == 0
    profile = await _resolve(monkeypatch, _pocket(NESTED))
    assert profile.max_turns == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pocket",
    [_pocket(DONE), _pocket(None), _pocket(NESTED, engine="react"), None],
    ids=["finished", "no-plan", "other-engine", "no-pocket"],
)
async def test_every_other_sites_turn_keeps_the_default_cap(monkeypatch, pocket) -> None:
    assert (await _resolve(monkeypatch, pocket)).max_turns is None


@pytest.mark.asyncio
async def test_an_operator_cap_still_caps_an_app_build_at_n(monkeypatch) -> None:
    from pocketpaw.config import get_settings

    monkeypatch.setattr(get_settings(), "sites_app_build_max_turns", 500)
    assert (await _resolve(monkeypatch, _pocket(NESTED))).max_turns == 500


@pytest.mark.asyncio
async def test_another_surface_never_reads_a_plan() -> None:
    ctx = _sites_ctx(kind=SurfaceKind.CHAT)
    assert await run_core._sites_refine_pocket(ctx) is None


def test_an_entity_override_keeps_the_surface_cap() -> None:
    base = SurfaceProfile(ripple_mode="off", max_turns=300)
    assert compose_entity_profile(base, {"skill_names": ["x"]}).max_turns == 300
    uncapped = SurfaceProfile(ripple_mode="off", max_turns=0)
    assert compose_entity_profile(uncapped, {"skill_names": ["x"]}).max_turns == 0


async def _drive(monkeypatch, ctx, events: list[Any]) -> tuple[dict[str, Any], list[Any]]:
    captured: dict[str, Any] = {}

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={}, agent_name="A")

        def run(self, agent_id, content, session_key, **run_kwargs):
            captured.update(run_kwargs)

            async def _gen():
                for event in events:
                    yield event

            return _gen()

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda ctx, backend_name=None: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda q: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda t: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda t: None)

    async def _never_cancelled():
        return False

    frames = [
        frame
        async for frame in run_core._drive_agent_loop(
            ctx,
            user_content="Build this plan",
            attachments_in=None,
            mentions_in=None,
            history=None,
            is_cancelled=_never_cancelled,
            emit_stream_start=False,
        )
    ]
    return captured, frames


@pytest.mark.asyncio
async def test_the_cap_reaches_pool_run_only_when_set(monkeypatch) -> None:
    ctx = _sites_ctx()
    ctx.resolved_profile = SurfaceProfile(ripple_mode="off", max_turns=300)
    run_kwargs, _ = await _drive(monkeypatch, ctx, [])
    assert run_kwargs["max_turns"] == 300

    # 0 is "no cap", not "no override": it must not be dropped as falsy.
    ctx.resolved_profile = SurfaceProfile(ripple_mode="off", max_turns=0)
    run_kwargs, _ = await _drive(monkeypatch, ctx, [])
    assert run_kwargs["max_turns"] == 0

    ctx.resolved_profile = SurfaceProfile(ripple_mode="off")
    run_kwargs, _ = await _drive(monkeypatch, ctx, [])
    assert "max_turns" not in run_kwargs


async def _prewarm_kwargs(monkeypatch, ctx) -> dict[str, Any] | None:
    captured: dict[str, Any] = {}

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={"backend": "claude_agent_sdk"})

        async def prewarm(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)
    await run_core._prewarm_session(ctx)
    return captured or None


@pytest.mark.asyncio
async def test_the_prewarm_carries_the_same_override_as_the_run(monkeypatch) -> None:
    """The turn cap is fixed at connect(), so it is in the warm-client key: an
    app-build prewarm that dropped the 0 would warm a 100-turn client the run
    then evicts."""
    ctx = _sites_ctx()
    ctx.resolved_profile = SurfaceProfile(ripple_mode="off", max_turns=0)
    kwargs = await _prewarm_kwargs(monkeypatch, ctx)
    assert kwargs is not None, "prewarm never ran: check its guards, not the assertion"
    assert kwargs["max_turns"] == 0

    ctx.resolved_profile = SurfaceProfile(ripple_mode="off")
    kwargs = await _prewarm_kwargs(monkeypatch, ctx)
    assert kwargs is not None
    assert "max_turns" not in kwargs


def _errors(frames: list[Any]) -> list[str]:
    return [f[1]["message"] for f in frames if isinstance(f, tuple) and f[0] == "error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop",
    [
        _result_error_text(
            SimpleNamespace(result=None, subtype="error_max_turns", terminal_reason=None),
            max_turns=300,
        ),
        "This reply reached its usage limit for one run and stopped.",
    ],
    ids=["claude-turn-cap", "free-run-limit"],
)
async def test_a_limit_stop_mid_plan_names_the_next_milestone(monkeypatch, stop) -> None:
    async def _fake_pocket(_ctx):
        return _pocket(NESTED)

    monkeypatch.setattr(run_core, "_sites_refine_pocket", _fake_pocket)
    ctx = _sites_ctx()
    ctx.resolved_profile = SurfaceProfile(ripple_mode="off", max_turns=300)
    _, frames = await _drive(monkeypatch, ctx, [SimpleNamespace(type="error", content=stop)])
    (message,) = _errors(frames)
    assert "M2 World and assets" in message
    assert "not finished" in message and "continue" in message


@pytest.mark.asyncio
async def test_other_errors_and_finished_plans_get_no_note(monkeypatch) -> None:
    async def _finished(_ctx):
        return _pocket(DONE)

    monkeypatch.setattr(run_core, "_sites_refine_pocket", _finished)
    ctx = _sites_ctx()
    ctx.resolved_profile = SurfaceProfile(ripple_mode="off")
    stop = "Stopped after reaching the tool-turn limit (100)."
    _, frames = await _drive(monkeypatch, ctx, [SimpleNamespace(type="error", content=stop)])
    assert "PLAN.md" not in _errors(frames)[0]

    async def _unfinished(_ctx):
        return _pocket(NESTED)

    monkeypatch.setattr(run_core, "_sites_refine_pocket", _unfinished)
    other = SimpleNamespace(type="error", content="The agent run failed.")
    _, frames = await _drive(monkeypatch, ctx, [other])
    assert "PLAN.md" not in _errors(frames)[0]
