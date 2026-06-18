# test_agent_router_sites_window.py — PERF-6 regression suite.
#
# Created: 2026-06-18 (PERF-6, feat/sites-minimal-context) — proves the
# sites-edit/refine surface ships a WINDOWED history to the executor (last N
# turns only) instead of the full accumulating scope history, while every
# non-sites surface keeps shipping the FULL history unchanged. The progressive
# slowdown on the sites builder came from feeding the whole growing chat to the
# LLM on every refine; windowing the refine surface fixes it without touching
# general pocket/dm/group chat.

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as router_mod
from pocketpaw_ee.cloud.chat import agent_service
from pocketpaw_ee.cloud.chat.agent_router import (
    SITES_REFINE_HISTORY_TURNS,
    _is_sites_refine_surface,
    _window_history_for_surface,
)
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, SurfaceMeta


class _StubTransport:
    """Yields one ``stream_end`` so the SSE generator finishes immediately."""

    async def request_cancel(self, run_id: str) -> None:  # noqa: ARG002
        return None

    def read_events(self, run_id: str, *, after: str = "0", block_ms: int = 15000) -> AsyncIterator:  # noqa: ARG002
        async def _gen() -> AsyncIterator:
            from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent

            yield StreamEvent(
                entry_id="1-0",
                event="stream_end",
                data={"assistant_message_id": None, "usage": {}, "cancelled": False},
            )

        return _gen()


def _sites_refine_surface() -> SurfaceContext:
    """A /sites/[siteId] refine surface: kind=SITES + meta.pocket_id set."""
    return SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=SurfaceKind.SITES,
        meta=SurfaceMeta(pocket_id="pkt-1", site_id="site-1"),
        preamble='<surface kind="sites" mode="refine" />',
    )


def _sites_create_surface() -> SurfaceContext:
    """A /sites gallery create surface: kind=SITES but NO pocket_id."""
    return SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=SurfaceKind.SITES,
        meta=SurfaceMeta(),
        preamble='<surface kind="sites" />',
    )


def _pocket_surface() -> SurfaceContext:
    """A general pocket chat surface (anchored to a pocket, NOT a site)."""
    return SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=SurfaceKind.POCKET,
        meta=SurfaceMeta(pocket_id="pkt-1"),
        preamble='<surface kind="pocket" />',
    )


def _long_history(turns: int) -> list[dict[str, str]]:
    """A long accumulating history: ``turns`` user+assistant pairs."""
    out: list[dict[str, str]] = []
    for i in range(turns):
        out.append({"role": "user", "content": f"edit request {i}"})
        out.append({"role": "assistant", "content": f"applied edit {i}"})
    return out


# ---------------------------------------------------------------------------
# Unit: the surface detector
# ---------------------------------------------------------------------------


def test_is_sites_refine_surface_true_only_for_sites_with_pocket_id():
    assert _is_sites_refine_surface(_sites_refine_surface()) is True
    # create-mode sites (no pocket_id) is NOT refine — full history.
    assert _is_sites_refine_surface(_sites_create_surface()) is False
    # a general pocket chat is NOT a sites refine even WITH a pocket_id.
    assert _is_sites_refine_surface(_pocket_surface()) is False
    # no surface context at all (legacy client) — never refine.
    assert _is_sites_refine_surface(None) is False


# ---------------------------------------------------------------------------
# Unit: the windowing helper
# ---------------------------------------------------------------------------


def test_window_history_caps_sites_refine_to_last_n_turns():
    history = _long_history(20)  # 40 messages
    windowed = _window_history_for_surface(history, _sites_refine_surface())
    # ≤ N turns ⇒ ≤ 2*N messages, taken from the TAIL (most recent).
    assert len(windowed) <= 2 * SITES_REFINE_HISTORY_TURNS
    assert windowed == history[-2 * SITES_REFINE_HISTORY_TURNS :]
    # the most recent turn must be present for pronoun referents.
    assert windowed[-1] == history[-1]


def test_window_history_unchanged_for_general_pocket_surface():
    history = _long_history(20)
    assert _window_history_for_surface(history, _pocket_surface()) == history


def test_window_history_unchanged_for_sites_create_surface():
    history = _long_history(20)
    assert _window_history_for_surface(history, _sites_create_surface()) == history


def test_window_history_unchanged_when_no_surface_context():
    history = _long_history(20)
    assert _window_history_for_surface(history, None) == history


def test_window_history_short_history_is_passed_through():
    history = _long_history(1)  # 2 messages, under the cap
    assert _window_history_for_surface(history, _sites_refine_surface()) == history


# ---------------------------------------------------------------------------
# Integration: post_agent_chat ships the WINDOWED history on the RunSpec for
# a sites-refine surface, and the FULL history for a non-sites surface.
# ---------------------------------------------------------------------------


async def _drive_post_agent_chat(
    cloud_app_client: AsyncClient,
    *,
    surface: str | None,
    surface_meta: dict | None,
    history: list[dict[str, str]],
) -> list:
    """POST one agent-chat turn with a stubbed loader returning ``history`` and
    capture the RunSpec handed to the executor. Returns ``captured_specs``."""
    captured_specs: list = []

    # A fake scope ctx so the test never depends on a real session in Mongo.
    # ``surface_context`` starts None; the router sets it from the REAL
    # ``resolve_surface_context`` using the request's surface / surface_meta —
    # exactly the production path that decides whether windowing kicks in.
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="session"),
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
        surface_context=None,
    )

    class _RecordingExecutor:
        async def submit(self, spec):
            captured_specs.append(spec)

    async def fake_resolver(**_):
        return fake_ctx

    async def fake_persist(_ctx, _body):
        return "user_msg_id_1"

    async def fake_ensure_session(_ctx):
        return None

    async def fake_loader(ctx, *, limit=50):  # noqa: ARG001
        return list(history)

    body: dict = {"content": "make the hero bigger", "client_message_id": "c-window"}
    if surface is not None:
        body["surface"] = surface
    if surface_meta is not None:
        body["surface_meta"] = surface_meta

    with (
        patch.object(router_mod, "resolve_scope_context", fake_resolver),
        patch.object(router_mod, "load_history_for_scope", fake_loader),
        patch.object(agent_service, "load_history_for_scope", fake_loader),
        patch.object(router_mod, "_persist_user_message", fake_persist),
        patch.object(router_mod, "_ensure_scope_session", fake_ensure_session),
        patch.object(router_mod, "get_executor", lambda: _RecordingExecutor()),
        patch.object(router_mod, "get_stream_transport", lambda: _StubTransport()),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json=body,
        )

    assert resp.status_code == 200, resp.text
    return captured_specs


@pytest.mark.asyncio
async def test_post_agent_chat_windows_history_for_sites_refine(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — Beanie init for create_run
):
    """A sites-refine surface (kind=sites + pocket_id) must ship a WINDOWED
    history (≤ N turns from the tail), even when the stored scope history is long."""
    history = _long_history(20)  # 40 messages

    specs = await _drive_post_agent_chat(
        cloud_app_client,
        surface="sites",
        surface_meta={"pocket_id": "pkt-1", "site_id": "site-1"},
        history=history,
    )

    assert len(specs) == 1
    shipped = specs[0].history
    assert len(shipped) <= 2 * SITES_REFINE_HISTORY_TURNS, (
        "sites refine must window the history so the LLM stops re-processing the "
        "whole accumulating chat on every edit (the progressive-slowdown cause)."
    )
    assert shipped == history[-2 * SITES_REFINE_HISTORY_TURNS :]


@pytest.mark.asyncio
async def test_post_agent_chat_keeps_full_history_for_general_chat(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — Beanie init for create_run
):
    """A non-sites surface (a plain session chat with no surface hint) must ship
    the FULL history — windowing is a no-op off the sites-refine surface."""
    history = _long_history(20)

    specs = await _drive_post_agent_chat(
        cloud_app_client,
        surface=None,
        surface_meta=None,
        history=history,
    )

    assert len(specs) == 1
    assert specs[0].history == history, (
        "general chat history must be unchanged — windowing only applies to the "
        "sites-refine surface."
    )
