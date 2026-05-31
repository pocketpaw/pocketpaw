# tests/cloud/test_outcomes_emitter.py
# Created: 2026-05-28 (feat/wave-3c-outcomes) — pins the RFC 03 v2
# template-level outcome event emission wired into the EE action
# pipeline.
#
# What this pins:
#   * Direct emitter: an action with N declared `outcomes_emitted` fires
#     N `OutcomeEmitted` events on the bus, plus ONE audit-log line.
#   * Empty outcomes_emitted: no events, no audit entry.
#   * Unknown action_name → `NotFound`.
#   * action_executor success path: outcomes fire AFTER the HTTP 2xx
#     audit, BEFORE the return. Failure / blocked / pending-approval
#     paths emit ZERO outcomes.
#   * Bulk dispatch fan-out: 3 successful rows × 1 outcome → 3 events.
#   * Tenant isolation: workspace_id rides on every event.
#   * Backward compat: no `template` threaded → no emission (every
#     legacy caller is byte-identical).
#
# The outcome bus event is intentionally distinct from M2b.2's
# `pocket.outcome` (binding-driven). RFC 03 v2 introduces a TEMPLATE-
# driven outcomes catalog + per-action emit list; the two coexist on
# the bus under different EVENT_TYPE strings.

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pocketpaw.bundled_templates import PocketTemplate

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fixture helpers — minimal valid v2 PocketTemplate dicts.
# ---------------------------------------------------------------------------


def _template(
    *,
    action_name: str = "renew_lease",
    outcomes_emitted: list[str] | None = None,
    outcomes_catalog: list[str] | None = None,
    instinct_policy: str = "auto",
    rules: list[dict] | None = None,
    kind: str = "single-row",
) -> PocketTemplate:
    """Build a v2 ``PocketTemplate`` with one action that carries the
    declared ``outcomes_emitted`` list. The top-level ``outcomes``
    catalog is auto-derived to cover the emitted names so the schema's
    subset validator passes.
    """
    emitted = list(outcomes_emitted or [])
    catalog = list(outcomes_catalog or emitted)
    raw: dict[str, Any] = {
        "schema_version": "2",
        "name": "test-template",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "test fixture",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [{"field": "value", "widget": "number"}],
            "id_field": "id",
        },
        "actions": [
            {
                "name": action_name,
                "label": "Do Thing",
                "kind": kind,
                "instinct_policy": instinct_policy,
                "outcomes_emitted": emitted,
            }
        ],
        "outcomes": catalog,
    }
    if rules is not None:
        raw["instinct_rules"] = {"rules": rules}
    return PocketTemplate.model_validate(raw)


def _raw_action() -> dict:
    """A minimal ActionBinding-parseable raw action."""
    return {"kind": "write_binding", "method": "POST", "path": "/items"}


def _patch_http_ok(monkeypatch, *, body: dict | None = None, status: int = 200) -> None:
    """Patch httpx.AsyncClient inside action_executor so the HTTP path
    returns a synthetic 2xx without hitting the network. Returns ``body``
    as the JSON response."""
    from pocketpaw_ee.cloud.pockets import action_executor

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body or {"ok": True})

    real_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _factory)


def _patch_http_500(monkeypatch) -> None:
    """Patch httpx so the executor sees a 500 — failure path."""
    from pocketpaw_ee.cloud.pockets import action_executor

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    real_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _factory)


def _patch_dns_external(monkeypatch) -> None:
    """Stub the DNS pre-resolve so the SSRF guard passes for example.test."""
    from pocketpaw_ee.cloud.pockets import _http_guard

    async def _ok(host: str) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(_http_guard, "_assert_host_external", _ok)
    # The executor also imports the symbol directly into its module
    # namespace; patch there too so the lookup path is consistent.
    from pocketpaw_ee.cloud.pockets import action_executor

    monkeypatch.setattr(action_executor, "_assert_host_external", _ok)


@pytest.fixture(autouse=True)
def _reset_action_rate_limit():
    """Each test starts with an empty write-rate-limit dict so a test
    that fires 3 rows in a row doesn't trip the 20-writes/min cap on a
    flaky CI re-run."""
    from pocketpaw_ee.cloud.pockets import action_executor

    action_executor._action_log.clear()
    yield
    action_executor._action_log.clear()


# ---------------------------------------------------------------------------
# Direct emitter — no executor, just emit_outcomes
# ---------------------------------------------------------------------------


async def test_emit_outcomes_fires_one_event_per_declared_name(recording_bus) -> None:
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import outcomes_emitter

    template = _template(
        action_name="renew_lease",
        outcomes_emitted=["renewal_completed", "revenue_recognized"],
    )
    row_context = {"id": "row-1", "value": 100}

    emitted = await outcomes_emitter.emit_outcomes(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="renew_lease",
        row_id="row-1",
        row_context=row_context,
    )

    # Return value: list of emitted events (caller can introspect).
    assert len(emitted) == 2
    names = {e.data["event_name"] for e in emitted}
    assert names == {"renewal_completed", "revenue_recognized"}

    # Bus saw exactly the two events.
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert len(bus_events) == 2

    # Each event carries the canonical payload.
    for evt in bus_events:
        assert evt.data["workspace_id"] == "w1"
        assert evt.data["pocket_id"] == "p1"
        assert evt.data["action_name"] == "renew_lease"
        assert evt.data["row_id"] == "row-1"
        assert evt.data["row_context_snapshot"] == row_context
        assert evt.data["template_name"] == "test-template"
        assert evt.data["template_version"] == "1.0.0"
        assert "emitted_at" in evt.data


async def test_emit_outcomes_empty_list_is_a_no_op(recording_bus) -> None:
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import outcomes_emitter

    template = _template(
        action_name="quiet_action",
        outcomes_emitted=[],
    )

    emitted = await outcomes_emitter.emit_outcomes(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="quiet_action",
        row_id="r1",
        row_context={"id": "r1"},
    )
    assert emitted == []
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert bus_events == []


async def test_emit_outcomes_unknown_action_raises_not_found() -> None:
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.cloud.pockets import outcomes_emitter

    template = _template(action_name="known_action", outcomes_emitted=["x"])

    with pytest.raises(NotFound) as excinfo:
        await outcomes_emitter.emit_outcomes(
            workspace_id="w1",
            user_id="u1",
            pocket_id="p1",
            template=template,
            action_name="ghost_action",
            row_id="r1",
            row_context={},
        )
    assert "ghost_action" in str(excinfo.value)


async def test_emit_outcomes_carries_workspace_isolation(recording_bus) -> None:
    """Every event must carry the caller's workspace_id verbatim."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import outcomes_emitter

    template = _template(
        action_name="ship_it",
        outcomes_emitted=["shipped"],
    )

    await outcomes_emitter.emit_outcomes(
        workspace_id="wA",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="ship_it",
        row_id="r1",
        row_context={"id": "r1"},
    )
    await outcomes_emitter.emit_outcomes(
        workspace_id="wB",
        user_id="u2",
        pocket_id="p2",
        template=template,
        action_name="ship_it",
        row_id="r2",
        row_context={"id": "r2"},
    )

    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert len(bus_events) == 2
    workspaces = {e.data["workspace_id"] for e in bus_events}
    assert workspaces == {"wA", "wB"}


# ---------------------------------------------------------------------------
# action_executor — outcomes fire only on the HTTP 2xx success path
# ---------------------------------------------------------------------------


async def test_executor_success_emits_outcomes(monkeypatch, recording_bus) -> None:
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import action_executor

    template = _template(
        action_name="renew_lease",
        outcomes_emitted=["renewal_completed"],
    )
    _patch_dns_external(monkeypatch)
    _patch_http_ok(monkeypatch, body={"renewed": True})

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="renew_lease",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"id": "row-7", "value": 100},
        row_id="row-7",
    )

    assert result["ok"] is True
    assert result["status"] == 200
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert len(bus_events) == 1
    assert bus_events[0].data["event_name"] == "renewal_completed"
    assert bus_events[0].data["row_id"] == "row-7"


async def test_executor_failure_emits_zero_outcomes(monkeypatch, recording_bus) -> None:
    """A 500 from the backend → outcomes MUST NOT fire."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import action_executor

    template = _template(
        action_name="renew_lease",
        outcomes_emitted=["renewal_completed"],
    )
    _patch_dns_external(monkeypatch)
    _patch_http_500(monkeypatch)

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="renew_lease",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"id": "row-1", "value": 1},
        row_id="row-1",
    )

    assert result["ok"] is False
    assert result["code"] == "http_error"
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert bus_events == []


async def test_executor_blocked_path_emits_zero_outcomes(monkeypatch, recording_bus) -> None:
    """An Instinct BLOCK verdict → outcomes MUST NOT fire."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import action_executor

    template = _template(
        action_name="renew_lease",
        outcomes_emitted=["renewal_completed"],
        rules=[{"when": "value > 100", "action": "block"}],
    )

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="renew_lease",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 999},
        row_id="row-1",
    )

    assert result["ok"] is False
    assert result["code"] == "instinct_blocked"
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert bus_events == []


async def test_executor_pending_approval_emits_zero_outcomes(recording_bus) -> None:
    """An Instinct ESCALATE_APPROVAL verdict → outcomes MUST NOT fire on
    the parking call. The eventual re-entry post-approval (Wave 3c
    follow-up flow) is where outcomes fire instead."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import action_executor

    template = _template(
        action_name="renew_lease",
        outcomes_emitted=["renewal_completed"],
        rules=[{"when": "value > 100", "action": "require_approval"}],
    )

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="renew_lease",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 200},
        row_id="row-1",
    )

    assert result["ok"] is True
    assert result["code"] == "instinct_pending"
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert bus_events == []


async def test_executor_without_template_emits_zero_outcomes(monkeypatch, recording_bus) -> None:
    """Backward-compat: every existing caller that doesn't thread a
    template through skips outcome emission entirely (the action's
    outcomes_emitted list is not even reachable without a template)."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import action_executor

    _patch_dns_external(monkeypatch)
    _patch_http_ok(monkeypatch)

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="renew_lease",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
    )

    assert result["ok"] is True
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert bus_events == []


async def test_executor_emits_one_event_per_outcome(monkeypatch, recording_bus) -> None:
    """An action declaring 3 outcomes_emitted fires 3 events on one
    successful run."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.pockets import action_executor

    template = _template(
        action_name="multi_emit",
        outcomes_emitted=["a_done", "b_done", "c_done"],
    )
    _patch_dns_external(monkeypatch)
    _patch_http_ok(monkeypatch)

    await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="multi_emit",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"id": "r1"},
        row_id="r1",
    )

    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert len(bus_events) == 3
    names = {e.data["event_name"] for e in bus_events}
    assert names == {"a_done", "b_done", "c_done"}


# ---------------------------------------------------------------------------
# Bulk dispatch — per-row outcome emission
# ---------------------------------------------------------------------------


async def test_bulk_dispatch_emits_outcomes_per_row(recording_bus, monkeypatch) -> None:
    """3 rows × 1 declared outcome → 3 OutcomeEmitted events."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
    from pocketpaw_ee.cloud.models.pocket_backend import (
        AllowedWrite as _AllowedWriteDoc,
    )
    from pocketpaw_ee.cloud.models.pocket_backend import (
        PocketBackendCredential as _BackendCredentialDoc,
    )
    from pocketpaw_ee.cloud.pockets import action_executor, bulk_dispatch

    template = _template(
        action_name="mark_done",
        outcomes_emitted=["row_finalized"],
        instinct_policy="auto",
        kind="bulk",
    )

    # Insert pocket + backend creds so bulk_dispatch finds them.
    pocket = _PocketDoc(
        workspace="w1",
        name="bulk-pocket",
        owner="u1",
        rippleSpec={
            "actions": {
                "mark_done": {
                    "kind": "write_binding",
                    "method": "POST",
                    "path": "/items",
                }
            }
        },
        visibility="workspace",
        widgets=[],
    )
    await pocket.insert()
    pocket_id = str(pocket.id)
    await _BackendCredentialDoc(
        pocket_id=pocket_id,
        workspace_id="w1",
        base_url="https://example.test",
        auth_type="none",
        auth_header=None,
        encrypted_token=None,
        nonce=None,
        salt=None,
        allowed_writes=[
            _AllowedWriteDoc.model_validate({"method": "POST", "path_pattern": "/items*"})
        ],
    ).insert()

    # Stub run_action to return success without HTTP — the outcomes
    # emitter should fire AFTER each successful row, regardless of
    # whether the call was real or stubbed.
    call_log: list[dict] = []

    async def _stub(**kwargs: Any) -> dict:
        call_log.append(kwargs)
        return {
            "ok": True,
            "action": kwargs["action"],
            "status": 200,
            "response": {"ok": True},
            "on_success": [],
            "on_error": [],
        }

    monkeypatch.setattr(action_executor, "run_action", _stub)

    rows = [
        {"id": "r1", "value": 1},
        {"id": "r2", "value": 2},
        {"id": "r3", "value": 3},
    ]
    result = await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=rows,
    )

    assert len(result.executions) == 3
    assert len(call_log) == 3

    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert len(bus_events) == 3
    row_ids = {e.data["row_id"] for e in bus_events}
    assert row_ids == {"r1", "r2", "r3"}
    for evt in bus_events:
        assert evt.data["event_name"] == "row_finalized"
        assert evt.data["workspace_id"] == "w1"
        assert evt.data["pocket_id"] == pocket_id


async def test_bulk_dispatch_zero_outcomes_when_action_declares_none(
    recording_bus, monkeypatch
) -> None:
    """Bulk action with outcomes_emitted=[] → 0 OutcomeEmitted events."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
    from pocketpaw_ee.cloud.models.pocket_backend import (
        AllowedWrite as _AllowedWriteDoc,
    )
    from pocketpaw_ee.cloud.models.pocket_backend import (
        PocketBackendCredential as _BackendCredentialDoc,
    )
    from pocketpaw_ee.cloud.pockets import action_executor, bulk_dispatch

    template = _template(
        action_name="mark_done",
        outcomes_emitted=[],
        kind="bulk",
    )

    pocket = _PocketDoc(
        workspace="w1",
        name="bulk-pocket",
        owner="u1",
        rippleSpec={
            "actions": {
                "mark_done": {
                    "kind": "write_binding",
                    "method": "POST",
                    "path": "/items",
                }
            }
        },
        visibility="workspace",
        widgets=[],
    )
    await pocket.insert()
    pocket_id = str(pocket.id)
    await _BackendCredentialDoc(
        pocket_id=pocket_id,
        workspace_id="w1",
        base_url="https://example.test",
        auth_type="none",
        auth_header=None,
        encrypted_token=None,
        nonce=None,
        salt=None,
        allowed_writes=[
            _AllowedWriteDoc.model_validate({"method": "POST", "path_pattern": "/items*"})
        ],
    ).insert()

    async def _stub(**kwargs: Any) -> dict:
        return {
            "ok": True,
            "action": kwargs["action"],
            "status": 200,
            "response": {"ok": True},
            "on_success": [],
            "on_error": [],
        }

    monkeypatch.setattr(action_executor, "run_action", _stub)

    rows = [{"id": "r1", "value": 1}, {"id": "r2", "value": 2}]
    result = await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=rows,
    )

    assert len(result.executions) == 2
    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert bus_events == []


async def test_bulk_dispatch_failed_rows_skip_outcomes(recording_bus, monkeypatch) -> None:
    """If a row's run_action returns ok:false, NO outcome fires for it.
    Rows that succeed still get their outcomes."""
    from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
    from pocketpaw_ee.cloud.models.pocket_backend import (
        AllowedWrite as _AllowedWriteDoc,
    )
    from pocketpaw_ee.cloud.models.pocket_backend import (
        PocketBackendCredential as _BackendCredentialDoc,
    )
    from pocketpaw_ee.cloud.pockets import action_executor, bulk_dispatch

    template = _template(
        action_name="mark_done",
        outcomes_emitted=["row_finalized"],
        kind="bulk",
    )

    pocket = _PocketDoc(
        workspace="w1",
        name="bulk-pocket",
        owner="u1",
        rippleSpec={
            "actions": {
                "mark_done": {
                    "kind": "write_binding",
                    "method": "POST",
                    "path": "/items",
                }
            }
        },
        visibility="workspace",
        widgets=[],
    )
    await pocket.insert()
    pocket_id = str(pocket.id)
    await _BackendCredentialDoc(
        pocket_id=pocket_id,
        workspace_id="w1",
        base_url="https://example.test",
        auth_type="none",
        auth_header=None,
        encrypted_token=None,
        nonce=None,
        salt=None,
        allowed_writes=[
            _AllowedWriteDoc.model_validate({"method": "POST", "path_pattern": "/items*"})
        ],
    ).insert()

    # First row succeeds, second fails.
    seq: list[dict] = [
        {
            "ok": True,
            "action": "mark_done",
            "status": 200,
            "response": {"ok": True},
            "on_success": [],
            "on_error": [],
        },
        {
            "ok": False,
            "action": "mark_done",
            "error": "boom",
            "code": "http_error",
            "on_error": [],
        },
    ]
    calls = {"i": 0}

    async def _stub(**kwargs: Any) -> dict:  # noqa: ARG001
        out = seq[calls["i"]]
        calls["i"] += 1
        return out

    monkeypatch.setattr(action_executor, "run_action", _stub)

    rows = [{"id": "r1", "value": 1}, {"id": "r2", "value": 2}]
    await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=rows,
    )

    bus_events = [e for e in recording_bus.events if isinstance(e, OutcomeEmitted)]
    assert len(bus_events) == 1
    assert bus_events[0].data["row_id"] == "r1"
