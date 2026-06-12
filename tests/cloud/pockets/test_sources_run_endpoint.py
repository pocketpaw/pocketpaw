# tests/cloud/pockets/test_sources_run_endpoint.py
# Created: 2026-06-08 (fix/pocket-sources-run-400) — route-level coverage for
#   ``POST /pockets/{id}/sources/run`` (the handler ``run_pocket_sources``).
#
# Regression gate for the on-open 400 bug: the frontend runs a pocket's
# declared ``rippleSpec.sources`` on open (trigger="pocket_open"). For a
# blank/starter pocket — no backend bound and no runnable sources — that
# implicit run is semantically a NO-OP (nothing to fetch), yet the route used
# to raise ``CloudError(400, "pocket_backend.not_configured")`` BEFORE checking
# whether any source was selected. The browser surfaced it as
# ``pocket_open sources run failed: HttpError: Bad Request`` on every open of a
# backend-less pocket. Every other source-run call site (agent pocket_router,
# temporal_dispatcher, bulk_dispatch) already treats "no backend" as a soft,
# non-fatal condition — only this REST route hard-400'd.
#
# What's covered:
#   - blank/starter pocket (no backend, no sources) on pocket_open → clean
#     {"ran": [], "errors": []} 200, NOT a 400  [the RED→GREEN repro]
#   - empty ``sources: {}`` block, no backend → same clean no-op
#   - a configured backend still runs the executor unchanged (no regression)
#   - a pocket that DECLARES sources but has no backend still surfaces the
#     not-configured condition (an authored source genuinely can't run) — so
#     the fix scopes the no-op to "nothing to run", not "swallow every error"
#   - an explicit single-source request naming a source that exists but with
#     no backend still surfaces not-configured (explicit intent is honored)

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

import importlib  # noqa: E402

from pocketpaw_ee.cloud.pockets.dto import RunSourcesRequest  # noqa: E402

# ``pockets/__init__.py`` rebinds the name ``router`` to the APIRouter
# instance, so ``from pocketpaw_ee.cloud.pockets import router`` returns the
# instance, not the module. Pull the actual module object explicitly so we can
# patch the module-level ``pockets_service`` seam and call the handler.
pockets_router = importlib.import_module("pocketpaw_ee.cloud.pockets.router")

_WS = "w1"
_USER = "u1"
_PID = "507f1f77bcf86cd799439011"

# A source the executor would run — used in the "declares sources but no
# backend" and "configured backend" cases.
_REVENUE_SOURCE = {
    "method": "GET",
    "path": "/revenue/today",
    "bind": "state.revenue",
    "refresh": ["pocket_open", "manual"],
}


def _pocket(ripple_spec: dict | None) -> dict:
    """Minimal pocket wire dict as ``pockets_service.get`` returns it."""
    return {"_id": _PID, "workspace": _WS, "owner": _USER, "rippleSpec": ripple_spec}


def _patch_service(*, pocket: dict, creds):
    """Patch the two service seams the route calls: ``get`` (pocket fetch)
    and ``get_pocket_backend_for_executor`` (backend creds, or None)."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch.object(
            pockets_router.pockets_service,
            "get",
            new=AsyncMock(return_value=pocket),
        )
    )
    stack.enter_context(
        patch.object(
            pockets_router.pockets_service,
            "get_pocket_backend_for_executor",
            new=AsyncMock(return_value=creds),
        )
    )
    return stack


async def _run(body: RunSourcesRequest):
    return await pockets_router.run_pocket_sources(_PID, body, workspace_id=_WS, user_id=_USER)


# ---------------------------------------------------------------------------
# RED → GREEN repro: blank/starter pocket on pocket_open is a clean no-op.
# ---------------------------------------------------------------------------


async def test_pocket_open_no_backend_no_sources_is_clean_noop():
    """The bug: opening a blank/starter pocket (no backend bound, no sources
    declared) POSTs ``sources/run`` with trigger="pocket_open" and used to
    400. It must instead return a clean ``{"ran": [], "errors": []}`` 200."""
    pocket = _pocket({"version": "1.0", "ui": {"id": "n_root0000", "type": "flex"}})
    with _patch_service(pocket=pocket, creds=None):
        result = await _run(RunSourcesRequest(trigger="pocket_open"))
    assert result == {"ran": [], "errors": []}


async def test_pocket_open_empty_sources_block_no_backend_is_noop():
    """An explicit empty ``sources: {}`` with no backend is also a no-op."""
    pocket = _pocket({"version": "1.0", "sources": {}, "ui": {"id": "n_r", "type": "flex"}})
    with _patch_service(pocket=pocket, creds=None):
        result = await _run(RunSourcesRequest(trigger="pocket_open"))
    assert result == {"ran": [], "errors": []}


async def test_pocket_open_no_ripple_spec_no_backend_is_noop():
    """A pocket with a missing/None rippleSpec (truly empty) is a no-op."""
    pocket = _pocket(None)
    with _patch_service(pocket=pocket, creds=None):
        result = await _run(RunSourcesRequest(trigger="pocket_open"))
    assert result == {"ran": [], "errors": []}


# ---------------------------------------------------------------------------
# No regression: a configured backend still drives the executor.
# ---------------------------------------------------------------------------


async def test_configured_backend_runs_executor():
    """With a backend bound, the route delegates to the executor unchanged."""
    pocket = _pocket({"version": "1.0", "sources": {"revenue": dict(_REVENUE_SOURCE)}, "ui": {}})
    # connector-as-backend: the executor tuple is an 8-tuple — trailing
    # backend_type / connector_name (http / None for this http backend).
    creds = ("https://api.example.com", "bearer", None, "tok", [], None, "http", None)
    executor_result = {"ran": [{"source": "revenue", "bind": "revenue", "value": 42}], "errors": []}
    with _patch_service(pocket=pocket, creds=creds):
        with patch(
            "pocketpaw_ee.cloud.pockets.source_executor.run_sources",
            new=AsyncMock(return_value=executor_result),
        ) as run_mock:
            result = await _run(RunSourcesRequest(trigger="pocket_open"))
    assert result == executor_result
    run_mock.assert_awaited_once()
    # The executor got the pocket's spec + backend creds.
    kwargs = run_mock.await_args.kwargs
    assert kwargs["base_url"] == "https://api.example.com"
    assert kwargs["trigger"] == "pocket_open"


# ---------------------------------------------------------------------------
# Scope guard: the no-op is "nothing to run", not "swallow not-configured".
# A pocket that DECLARES runnable sources but has no backend still surfaces it.
# ---------------------------------------------------------------------------


async def test_declares_sources_but_no_backend_still_surfaces_not_configured():
    """A pocket that authored a real source but never bound a backend is a
    genuine misconfiguration — the fix must NOT swallow it into a silent
    no-op. The selected source is reported as a not-configured error."""
    pocket = _pocket({"version": "1.0", "sources": {"revenue": dict(_REVENUE_SOURCE)}, "ui": {}})
    with _patch_service(pocket=pocket, creds=None):
        result = await _run(RunSourcesRequest(trigger="pocket_open"))
    assert result["ran"] == []
    assert len(result["errors"]) == 1
    err = result["errors"][0]
    assert err["source"] == "revenue"
    assert err["code"] == "pocket_backend.not_configured"


async def test_explicit_named_source_no_backend_surfaces_not_configured():
    """An explicit single-source request (source="revenue") that selects a
    real declared source still surfaces not-configured when no backend is
    bound — explicit intent is honored, not silently dropped."""
    pocket = _pocket({"version": "1.0", "sources": {"revenue": dict(_REVENUE_SOURCE)}, "ui": {}})
    with _patch_service(pocket=pocket, creds=None):
        result = await _run(RunSourcesRequest(source="revenue"))
    assert result["ran"] == []
    assert [e["code"] for e in result["errors"]] == ["pocket_backend.not_configured"]


async def test_explicit_named_unknown_source_no_backend_is_noop():
    """An explicit source name that does NOT exist in the spec selects
    nothing, so even with no backend it is a clean no-op (no error)."""
    pocket = _pocket({"version": "1.0", "sources": {"revenue": dict(_REVENUE_SOURCE)}, "ui": {}})
    with _patch_service(pocket=pocket, creds=None):
        result = await _run(RunSourcesRequest(source="does_not_exist"))
    assert result == {"ran": [], "errors": []}
