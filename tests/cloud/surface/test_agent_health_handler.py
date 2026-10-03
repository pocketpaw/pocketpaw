# tests/cloud/surface/test_agent_health_handler.py — the agent_health preamble.
#
# The handler emits ``<surface kind="agent_health" run_id="…"/>`` plus the
# lens-tools instruction, echoes run_id only when it is a 32-lowercase-hex trace
# id (anything else is dropped, so client input can't inject markup), keys the
# cache on the run id alone, and the dispatcher routes the "agent_health" kind
# string to it.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import agent_health
from pocketpaw_ee.cloud.surface.service import resolve_surface_context

TRACE = "0123456789abcdef" * 2


async def test_tag_carries_run_id_and_instruction():
    pre = await agent_health.build_preamble("ws", "u", SurfaceMeta(run_id=TRACE))
    assert pre.text.startswith(f'<surface kind="agent_health" run_id="{TRACE}" />')
    assert "mcp__pocketpaw_lens__lens_overview" in pre.text
    assert f"run {TRACE} open" in pre.text


@pytest.mark.parametrize("bad", ['x" /><evil>', "A" * 32, "a" * 31, "a" * 33, "../" + "a" * 29, ""])
async def test_bad_run_id_dropped(bad):
    pre = await agent_health.build_preamble("ws", "u", SurfaceMeta(run_id=bad))
    assert pre.text.startswith('<surface kind="agent_health" />')
    assert "run_id=" not in pre.text and "<evil>" not in pre.text
    plain = await agent_health.build_preamble("ws", "u", SurfaceMeta())
    assert pre.cache_key == plain.cache_key


async def test_cache_key_moves_with_run():
    a = await agent_health.build_preamble("ws", "u", SurfaceMeta(run_id=TRACE))
    b = await agent_health.build_preamble("ws", "u", SurfaceMeta(run_id="f" * 32))
    assert a.cache_key != b.cache_key


async def test_dispatcher_routes_kind():
    ctx = await resolve_surface_context(
        "ws", "u", {"surface": "agent_health", "meta": {"run_id": TRACE}}
    )
    assert ctx.kind is SurfaceKind.AGENT_HEALTH
    assert f'run_id="{TRACE}"' in ctx.preamble
