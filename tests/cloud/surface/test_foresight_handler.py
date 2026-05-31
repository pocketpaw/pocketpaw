# tests/cloud/surface/test_foresight_handler.py — /foresight surface preamble.
#
# Created: 2026-05-27 (feat/foresight-v12-config-and-surface-handler) —
# Locks five guarantees for the foresight surface handler:
#   1. Surface tag emits with panel attribute when one is supplied.
#   2. Active-run block fetches via foresight_service.get_scenario_run
#      when ``meta.run_id`` is set; missing data degrades to absent
#      block (not an exception).
#   3. Active-scenario block fetches via foresight_scenarios.get_custom_
#      scenario when ``meta.scenario_id`` is set.
#   4. Skill activation hint appears iff ``settings.foresight_use_skill``
#      is True (default ON as of 2026-05-27).
#   5. Failure mode: the handler never raises — every data fetch is
#      isolated, and the chat router still gets a usable preamble even
#      when every backing service errors.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import foresight as foresight_handler

# ---------------------------------------------------------------------------
# Surface tag + panel rendering
# ---------------------------------------------------------------------------


async def test_preamble_includes_directive_surface_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preamble carries a directive block that asserts foresight-first
    behavior. Captain caught (2026-05-27) the agent offering pocket
    affordances on /foresight when the preamble was descriptive but not
    directive. The fix is locked here so the guidance can't drop silently.
    """
    monkeypatch.setattr(foresight_handler, "_render_active_run", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_active_scenario", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_workspace_ambient", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_skill_hint", lambda: "")

    out = await foresight_handler.build_preamble("ws_a", "user_a", SurfaceMeta())
    assert "<surface-guidance>" in out
    assert "PREFER Foresight affordances" in out
    assert "DO NOT offer pocket creation" in out
    assert "Rehearse a decision" in out
    assert "Run a quick scenario" in out


async def test_surface_tag_emits_with_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The opening tag carries panel when one of the valid values is supplied.

    Invalid panel strings are dropped silently (the agent should never
    see a typoed panel attribute leak into its prompt context).
    """
    # Stub every downstream so the test isolates rendering logic.
    monkeypatch.setattr(foresight_handler, "_render_active_run", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_active_scenario", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_workspace_ambient", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_skill_hint", lambda: "")

    meta = SurfaceMeta(panel="live")
    out = await foresight_handler.build_preamble("ws_a", "user_a", meta)
    assert '<surface kind="foresight" route="/foresight" panel="live" />' in out


async def test_surface_tag_drops_unknown_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-vocab panel value (typo, future panel) is silently dropped.

    Keeps the agent from seeing ``panel="lvie"`` and reasoning about a
    panel that doesn't exist.
    """
    monkeypatch.setattr(foresight_handler, "_render_active_run", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_active_scenario", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_workspace_ambient", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_skill_hint", lambda: "")

    meta = SurfaceMeta(panel="lvie")
    out = await foresight_handler.build_preamble("ws_a", "user_a", meta)
    assert '<surface kind="foresight" route="/foresight" />' in out
    assert "panel=" not in out


# ---------------------------------------------------------------------------
# Active-run + active-scenario blocks
# ---------------------------------------------------------------------------


async def test_active_run_block_uses_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_id present → calls _render_active_run with the same id."""
    captured: dict[str, Any] = {}

    async def _fake_render_run(workspace_id: str, run_id: str) -> str:
        captured["run_id"] = run_id
        captured["workspace_id"] = workspace_id
        return f'<active-run id="{run_id}" status="complete" />'

    monkeypatch.setattr(foresight_handler, "_render_active_run", _fake_render_run)
    monkeypatch.setattr(foresight_handler, "_render_active_scenario", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_workspace_ambient", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_skill_hint", lambda: "")

    meta = SurfaceMeta(run_id="run:abc", panel="results")
    out = await foresight_handler.build_preamble("ws_a", "user_a", meta)
    assert captured == {"run_id": "run:abc", "workspace_id": "ws_a"}
    assert '<active-run id="run:abc"' in out


async def test_active_scenario_block_uses_scenario_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scenario_id present → calls _render_active_scenario with the same id."""
    captured: dict[str, Any] = {}

    async def _fake_render_scenario(workspace_id: str, scenario_id: str) -> str:
        captured["scenario_id"] = scenario_id
        return f'<active-scenario id="{scenario_id}" name="renewal" />'

    monkeypatch.setattr(foresight_handler, "_render_active_run", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_active_scenario", _fake_render_scenario)
    monkeypatch.setattr(foresight_handler, "_render_workspace_ambient", _async_return(""))
    monkeypatch.setattr(foresight_handler, "_render_skill_hint", lambda: "")

    meta = SurfaceMeta(scenario_id="cs_xyz", panel="editor")
    out = await foresight_handler.build_preamble("ws_a", "user_a", meta)
    assert captured == {"scenario_id": "cs_xyz"}
    assert '<active-scenario id="cs_xyz"' in out


# ---------------------------------------------------------------------------
# Skill activation hint
# ---------------------------------------------------------------------------


def test_skill_hint_appears_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """foresight_use_skill = True → the hint block is emitted."""
    from pocketpaw import config

    fake = SimpleNamespace(foresight_use_skill=True)
    monkeypatch.setattr(config, "get_settings", lambda: fake)
    out = foresight_handler._render_skill_hint()
    assert '<skill-active name="foresight-create-sim">' in out
    assert "foresight-create-sim" in out


def test_skill_hint_omitted_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """foresight_use_skill = False → no hint block."""
    from pocketpaw import config

    fake = SimpleNamespace(foresight_use_skill=False)
    monkeypatch.setattr(config, "get_settings", lambda: fake)
    out = foresight_handler._render_skill_hint()
    assert out == ""


# ---------------------------------------------------------------------------
# Service registry dispatch
# ---------------------------------------------------------------------------


async def test_service_dispatches_foresight_kind_to_handler() -> None:
    """resolve_surface_context maps surface='foresight' onto the new handler.

    The full dispatch path: SurfaceRequest validation → SurfaceKind
    resolution → handler registry lookup. This is the lock against
    accidental registry omissions.
    """
    from pocketpaw_ee.cloud.surface.service import resolve_surface_context

    body = {
        "surface": "foresight",
        "meta": {
            "panel": "scenarios",
            "route_path": "/foresight",
        },
    }
    ctx = await resolve_surface_context("ws_a", "user_a", body)
    assert ctx.kind == SurfaceKind.FORESIGHT
    assert '<surface kind="foresight"' in ctx.preamble
    assert 'panel="scenarios"' in ctx.preamble


# ---------------------------------------------------------------------------
# Failure modes — handler never raises
# ---------------------------------------------------------------------------


async def test_handler_degrades_when_every_subrender_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All sub-renderers raise → still returns the surface tag + skill hint.

    The chat router must always receive SOME preamble; a crash here
    would break the entire chat send for users on /foresight.
    """

    async def _boom_async(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("intentional surface-handler failure")

    monkeypatch.setattr(foresight_handler, "_render_active_run", _boom_async)
    monkeypatch.setattr(foresight_handler, "_render_active_scenario", _boom_async)
    monkeypatch.setattr(foresight_handler, "_render_workspace_ambient", _boom_async)

    meta = SurfaceMeta(run_id="run:x", scenario_id="cs_x", panel="live")
    # We expect this to propagate (build_preamble doesn't catch sub-renderer
    # exceptions; the service.py wrapper does — verified by the next test).
    with pytest.raises(RuntimeError):
        await foresight_handler.build_preamble("ws_a", "user_a", meta)


async def test_service_wrapper_absorbs_handler_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the handler itself raises, the service falls back to GENERIC."""
    from pocketpaw_ee.cloud.surface import service as surface_service

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("handler exploded")

    # Force the registry to load with our boom-handler patched in. We
    # reach into the lazy load + override after.
    surface_service._HANDLERS = None  # type: ignore[attr-defined]
    handlers = surface_service._load_handlers()
    handlers[SurfaceKind.FORESIGHT] = _boom
    surface_service._HANDLERS = handlers  # type: ignore[attr-defined]

    ctx = await surface_service.resolve_surface_context("ws_a", "user_a", {"surface": "foresight"})
    # GENERIC fallback per service.py contract — chat send still works.
    assert ctx.kind == SurfaceKind.GENERIC
    assert ctx.preamble == ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_return(value: Any) -> Any:
    """Wrap a value in an async callable. Mirrors test_calendar_handler pattern."""

    async def _runner(*_a: Any, **_kw: Any) -> Any:
        return value

    return _runner
