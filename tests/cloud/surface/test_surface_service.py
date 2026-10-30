# tests/cloud/surface/test_surface_service.py — Resolver behavior.
#
# Created: 2026-05-24 — Covers the three guarantees the chat router
# relies on:
#   1. Unknown surface strings fall back to GENERIC instead of raising —
#      a client can ship a new surface name before the backend ships its
#      handler.
#   2. Invalid meta shapes (wrong types) collapse into GENERIC + empty
#      preamble instead of propagating a validation error.
#   3. A handler that raises does NOT propagate — the resolver swallows
#      and returns a GENERIC context with empty preamble so the chat
#      send always succeeds.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, resolve_surface_context
from pocketpaw_ee.cloud.surface import service as surface_service

pytestmark = pytest.mark.asyncio


async def test_resolve_unknown_surface_returns_generic() -> None:
    """A surface string the backend doesn't know maps to GENERIC."""
    ctx = await resolve_surface_context("w1", "u1", {"surface": "this_does_not_exist", "meta": {}})
    assert isinstance(ctx, SurfaceContext)
    assert ctx.kind is SurfaceKind.GENERIC
    # Generic handler still returns a small preamble — not empty.
    assert "generic" in ctx.preamble


async def test_resolve_invalid_meta_does_not_crash() -> None:
    """Bad body shape produces GENERIC with empty preamble, never raises."""
    ctx = await resolve_surface_context("w1", "u1", {"surface": "home", "meta": "not-a-dict"})
    assert ctx.kind is SurfaceKind.GENERIC
    # The validation failure short-circuits before any handler runs, so
    # the preamble is empty rather than the generic fall-back text.
    assert ctx.preamble == ""


async def test_resolve_handler_failure_returns_empty_preamble(monkeypatch) -> None:
    """When a handler raises, the resolver returns GENERIC + empty preamble."""

    async def _boom(workspace_id: str, user_id: str, meta) -> str:  # noqa: ARG001
        raise RuntimeError("simulated handler failure")

    # Force the registry to load with our broken handler in place of the
    # real home handler. The resolver should catch and downgrade.
    surface_service._HANDLERS = None  # invalidate any cache from prior tests
    real_loader = surface_service._load_handlers

    def _patched_loader():
        handlers = real_loader()
        handlers[SurfaceKind.HOME] = _boom
        return handlers

    monkeypatch.setattr(surface_service, "_load_handlers", _patched_loader)
    surface_service._HANDLERS = None  # ensure lazy reload picks up the patch

    ctx = await resolve_surface_context("w1", "u1", {"surface": "home", "meta": {}})

    # GENERIC fall-back, no propagation, empty preamble.
    assert ctx.kind is SurfaceKind.GENERIC
    assert ctx.preamble == ""

    # Reset the cache so other tests in the file don't see the patched
    # loader.
    surface_service._HANDLERS = None


async def test_resolve_none_body_is_treated_as_empty() -> None:
    """``body=None`` is equivalent to ``{}`` — GENERIC handler with placeholder."""
    ctx = await resolve_surface_context("w1", "u1", None)
    assert ctx.kind is SurfaceKind.GENERIC
    assert "generic" in ctx.preamble
