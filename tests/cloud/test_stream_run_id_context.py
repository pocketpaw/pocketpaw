# tests/cloud/test_stream_run_id_context.py — pins the per-stream belt run-id
# ContextVar added 2026-09-12 (feat/belt-factory integration).
#
# WHY THIS EXISTS: the belt stage/entity events carry a ``run_id`` join key so the
# /belt console can attribute them to a run. ``belt_propose_change`` runs inside an
# MCP tool with no run argument in scope, so without this ContextVar its ``verify``
# stage emit would publish with neither ``run_id`` nor ``action_id`` (the Instinct
# Action does not exist until propose time) and land as an unattributable pulse.
# These tests pin the bind/reset contract and, critically, that the value does not
# leak across runs.
from __future__ import annotations

import asyncio

import pytest

from pocketpaw_ee.cloud.chat.agent_service import (
    bind_stream_run_id,
    current_stream_run_id,
    unbind_stream_run_id,
)


def test_default_is_none_outside_a_run() -> None:
    assert current_stream_run_id() is None


def test_bind_then_reset_restores_the_previous_value() -> None:
    token = bind_stream_run_id("run-1")
    assert current_stream_run_id() == "run-1"
    unbind_stream_run_id(token)
    assert current_stream_run_id() is None


def test_nested_binds_restore_the_outer_value() -> None:
    outer = bind_stream_run_id("run-outer")
    inner = bind_stream_run_id("run-inner")
    assert current_stream_run_id() == "run-inner"
    unbind_stream_run_id(inner)
    assert current_stream_run_id() == "run-outer"
    unbind_stream_run_id(outer)
    assert current_stream_run_id() is None


def test_binding_none_clears_it() -> None:
    outer = bind_stream_run_id("run-1")
    inner = bind_stream_run_id(None)
    assert current_stream_run_id() is None
    unbind_stream_run_id(inner)
    unbind_stream_run_id(outer)


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_leak_into_each_other() -> None:
    """Two runs in flight must each see their OWN id. asyncio copies the context
    per task, so a bind inside one task is invisible to its sibling."""
    seen: dict[str, str | None] = {}

    async def one_run(run_id: str, hold: float) -> None:
        token = bind_stream_run_id(run_id)
        try:
            await asyncio.sleep(hold)
            seen[run_id] = current_stream_run_id()
        finally:
            unbind_stream_run_id(token)

    await asyncio.gather(one_run("run-a", 0.02), one_run("run-b", 0.01))

    assert seen == {"run-a": "run-a", "run-b": "run-b"}
    assert current_stream_run_id() is None
