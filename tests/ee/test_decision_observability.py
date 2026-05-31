# tests/ee/test_decision_observability.py
# Created: 2026-05-26 (RFC 09 Slice 4 — feat/rfc-09-slice-4-reconciler)
#
# Slice 4 § Observability — heartbeat log + reconciler status admin
# endpoint + projection drift counters surfaced via the same status
# payload.
#
# Tests:
#   1. Heartbeat log fires on every tick at INFO with the right shape.
#   2. ``GET /api/v1/decisions/_reconciler/status`` returns the
#      expected shape (cursor, last_tick_ts, errors_last_hour, totals).
#   3. The counters increment correctly across multiple ticks.
#   4. errors_last_hour drops stale entries after the 1-hour window.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.decisions.reconciler import (  # noqa: E402
    DecisionReconciler,
    reset_reconciler_for_tests,
)
from pocketpaw_ee.cloud.decisions.router import router as decisions_router  # noqa: E402
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402
from soul_protocol.spec.journal import Actor, EventEntry  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    journal_dep.reset_journal_cache()
    original = journal_dep._cached_journal

    def _stub() -> object:
        return j

    journal_dep._cached_journal = _stub  # type: ignore[assignment]
    yield j
    journal_dep._cached_journal = original  # type: ignore[assignment]
    journal_dep.reset_journal_cache()
    j.close()


@pytest.fixture
def graph(tmp_path: Path) -> DecisionGraph:
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def reconciler() -> DecisionReconciler:
    reset_reconciler_for_tests()
    r = DecisionReconciler(interval_seconds=60)
    # Wire the singleton accessor so the admin endpoint reads this one.
    import pocketpaw_ee.cloud.decisions.reconciler as reconciler_module

    reconciler_module._RECONCILER = r
    yield r
    reset_reconciler_for_tests()


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _make_client(monkeypatch) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))
    # The admin guard calls ``check_workspace_action`` to verify the
    # user holds the action. The endpoint's ``_admin_dep`` lazy-
    # resolves the dep factory at call time so the FastAPI override
    # map cannot intercept it. The pragmatic fix is to stub the
    # underlying guard helper to a no-op.
    import pocketpaw_ee.cloud._core.deps as deps_module

    monkeypatch.setattr(deps_module, "check_workspace_action", lambda *_args, **_kwargs: None)

    user = _FakeUser("admin-1", "ws-A")
    app = FastAPI()
    add_error_handler(app)
    app.include_router(decisions_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app)


def _agent_actor() -> Actor:
    return Actor(kind="agent", id="did:soul:agent_1", scope_context=["org:nerve"])


def _proposed(correlation_id, ts) -> EventEntry:
    return EventEntry(
        id=uuid4(),
        ts=ts,
        actor=_agent_actor(),
        action="agent.proposed",
        scope=["org:nerve", "pocket:p1"],
        correlation_id=correlation_id,
        payload={"intent": "test intent", "action": "do_thing", "pocket_id": "p1"},
    )


def _completed(correlation_id, ts) -> EventEntry:
    return EventEntry(
        id=uuid4(),
        ts=ts,
        actor=_agent_actor(),
        action="decision.completed",
        scope=["org:nerve", "pocket:p1"],
        correlation_id=correlation_id,
        payload={"passed": True, "action_outcome": "landed"},
    )


async def test_heartbeat_log_fires_on_tick(
    journal, graph: DecisionGraph, reconciler: DecisionReconciler, caplog
) -> None:
    """A reconciler tick emits a heartbeat log line at INFO."""
    corr = uuid4()
    base_ts = datetime.now(UTC)
    journal.append(_proposed(corr, base_ts))
    journal.append(_completed(corr, base_ts + timedelta(seconds=1)))

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="pocketpaw_ee.cloud.decisions.reconciler"):
        await reconciler.tick()

    heartbeat_records = [
        r
        for r in caplog.records
        if r.name == "pocketpaw_ee.cloud.decisions.reconciler" and "tick cursor=" in r.getMessage()
    ]
    assert len(heartbeat_records) == 1
    msg = heartbeat_records[0].getMessage()
    assert "applied=2" in msg
    assert "errors=0" in msg
    assert "lag_seconds=" in msg


async def test_status_payload_shape(
    journal, graph: DecisionGraph, reconciler: DecisionReconciler
) -> None:
    """The status payload exposes every field the admin endpoint
    advertises."""
    payload = reconciler.status.to_payload()
    assert set(payload.keys()) == {
        "cursor",
        "last_tick_ts",
        "last_tick_applied",
        "last_tick_errors",
        "lag_seconds",
        "total_ticks",
        "total_applied",
        "total_errors",
        "errors_last_hour",
        "last_error_ts",
        "last_error_message",
        "started_at",
        "interval_seconds",
    }


async def test_status_counters_increment_across_ticks(
    journal, graph: DecisionGraph, reconciler: DecisionReconciler
) -> None:
    """Each tick advances ``total_ticks`` + ``total_applied``."""
    base_ts = datetime.now(UTC)

    # First tick — two events.
    corr1 = uuid4()
    journal.append(_proposed(corr1, base_ts))
    journal.append(_completed(corr1, base_ts + timedelta(seconds=1)))
    await reconciler.tick()
    assert reconciler.status.total_ticks == 1
    assert reconciler.status.total_applied == 2
    first_applied = reconciler.status.total_applied

    # Second tick — one new chain. On 0.3.1 the cursor stays at 0 so
    # the second tick re-walks the journal — total_applied grows by 4
    # (re-apply of the original 2 + the new 2). On 0.3.2+ it only
    # grows by 2 (the new chain). Either is acceptable; the
    # invariant is "monotonic non-decrease."
    corr2 = uuid4()
    journal.append(_proposed(corr2, base_ts + timedelta(seconds=10)))
    journal.append(_completed(corr2, base_ts + timedelta(seconds=11)))
    await reconciler.tick()
    assert reconciler.status.total_ticks == 2
    assert reconciler.status.total_applied >= first_applied + 2


async def test_errors_last_hour_window_drops_stale_entries(
    journal, graph: DecisionGraph, reconciler: DecisionReconciler
) -> None:
    """Errors recorded > 1h ago drop out of ``errors_last_hour`` on
    the next ``to_payload()`` call."""
    now = datetime.now(UTC)
    # Two old (>1h) + one fresh.
    reconciler.status.error_history.extend(
        [
            now - timedelta(hours=2),
            now - timedelta(hours=3),
            now - timedelta(minutes=10),
        ]
    )
    payload = reconciler.status.to_payload()
    assert payload["errors_last_hour"] == 1


async def test_admin_endpoint_returns_status_payload(
    monkeypatch, journal, graph: DecisionGraph, reconciler: DecisionReconciler
) -> None:
    """``GET /api/v1/decisions/_reconciler/status`` returns the
    singleton's status payload."""
    # Seed one tick so the status carries non-zero values.
    base_ts = datetime.now(UTC)
    corr = uuid4()
    journal.append(_proposed(corr, base_ts))
    journal.append(_completed(corr, base_ts + timedelta(seconds=1)))
    await reconciler.tick()

    client = _make_client(monkeypatch)
    resp = client.get("/api/v1/decisions/_reconciler/status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total_ticks"] == 1
    assert payload["total_applied"] == 2
    # cursor is wheel-dependent — 0.3.1 stays at 0, 0.3.2+ advances;
    # what the endpoint pins is that the field is present + an int.
    assert isinstance(payload["cursor"], int)
    assert payload["interval_seconds"] == 60
