# tests/cloud/test_pocket_outcomes.py — RFC 05 M2b.2.
# Created: 2026-05-22 — coverage for the minimal outcome meter: the
# `pocket.outcome` event, the JSONL ledger subscriber, the count
# service, and the `GET /outcomes` route.
#
# What this pins:
#   - emit_pocket_outcome emits a PocketOutcomeEvent for a named outcome
#     and is a no-op when the binding declared none.
#   - record_outcome (the bus subscriber) appends one JSON line to a
#     workspace-scoped ledger.
#   - count_outcomes groups the ledger rows by name and pocket and
#     honors the pocket_id / since filters.
#   - GET /outcomes returns the grouped count.
#   - a broken subscriber does not fail the originating write (bus
#     isolation) — and Layer-4 fields stay null.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install.

from __future__ import annotations

import json

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.realtime.events import Event, PocketOutcomeEvent  # noqa: E402
from pocketpaw_ee.cloud.outcomes import service as outcomes_service  # noqa: E402
from pocketpaw_ee.cloud.outcomes.dto import CountOutcomesRequest  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path):
    """Point the outcomes ledger at a tmp dir so tests never touch ~/.pocketpaw."""
    outcomes_service.set_ledger_dir(tmp_path / "outcomes")
    yield
    # Restore the default so a later test in the same process isn't
    # surprised by a stale tmp path.
    outcomes_service.set_ledger_dir("~/.pocketpaw/outcomes")


def _outcome_event(
    *,
    outcome: str = "renewal_completed",
    workspace_id: str = "w1",
    pocket_id: str = "p1",
    action: str = "mark_renewed",
) -> PocketOutcomeEvent:
    return PocketOutcomeEvent(
        data={
            "outcome": outcome,
            "pocket_id": pocket_id,
            "workspace_id": workspace_id,
            "action": action,
            "actor": "u1",
            "via_instinct": False,
            "instinct_action_id": None,
            "occurred_at": "2026-05-22T10:00:00+00:00",
            "outcome_value": None,
            "outcome_unit": None,
        }
    )


# ---------------------------------------------------------------------------
# emit_pocket_outcome
# ---------------------------------------------------------------------------


async def test_emit_pocket_outcome_emits_for_named_outcome(monkeypatch):
    emitted = []

    async def _emit(event):
        emitted.append(event)

    monkeypatch.setattr(outcomes_service, "emit", _emit)
    await outcomes_service.emit_pocket_outcome(
        outcome="renewal_completed",
        pocket_id="p1",
        workspace_id="w1",
        action="mark_renewed",
        actor="u1",
        via_instinct=False,
    )
    assert len(emitted) == 1
    assert emitted[0].type == "pocket.outcome"
    assert emitted[0].data["outcome"] == "renewal_completed"
    # Layer 4 reserved — billing fields stay null.
    assert emitted[0].data["outcome_value"] is None
    assert emitted[0].data["outcome_unit"] is None


async def test_emit_pocket_outcome_no_outcome_is_noop(monkeypatch):
    """A binding with no `outcome` → emit_pocket_outcome emits nothing."""
    emitted = []

    async def _emit(event):
        emitted.append(event)

    monkeypatch.setattr(outcomes_service, "emit", _emit)
    await outcomes_service.emit_pocket_outcome(
        outcome=None,
        pocket_id="p1",
        workspace_id="w1",
        action="mark_renewed",
        actor="u1",
        via_instinct=False,
    )
    assert emitted == []


# ---------------------------------------------------------------------------
# record_outcome — the JSONL ledger subscriber
# ---------------------------------------------------------------------------


async def test_record_outcome_appends_to_ledger():
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w1", outcome="a"))
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w1", outcome="b"))
    counts = await outcomes_service.count_outcomes("w1")
    assert counts.total == 2
    assert counts.by_outcome == {"a": 1, "b": 1}


async def test_record_outcome_drops_event_missing_workspace():
    """An event with no workspace_id is dropped — nothing is recorded."""
    bad = PocketOutcomeEvent(data={"outcome": "x"})  # no workspace_id
    await outcomes_service.record_outcome(bad)
    # Nothing was written; a count on any workspace is zero.
    assert (await outcomes_service.count_outcomes("w1")).total == 0


async def test_count_outcomes_groups_by_pocket_and_filters():
    await outcomes_service.record_outcome(
        _outcome_event(workspace_id="w1", pocket_id="p1", outcome="x")
    )
    await outcomes_service.record_outcome(
        _outcome_event(workspace_id="w1", pocket_id="p1", outcome="x")
    )
    await outcomes_service.record_outcome(
        _outcome_event(workspace_id="w1", pocket_id="p2", outcome="y")
    )
    all_counts = await outcomes_service.count_outcomes("w1")
    assert all_counts.total == 3
    assert all_counts.by_pocket == {"p1": 2, "p2": 1}
    # Filter to one pocket.
    p1 = await outcomes_service.count_outcomes("w1", CountOutcomesRequest(pocket_id="p1"))
    assert p1.total == 2
    assert p1.by_outcome == {"x": 2}


async def test_count_outcomes_since_filter():
    """`since` is an inclusive ISO lower bound on occurred_at."""
    early = PocketOutcomeEvent(
        data={
            "outcome": "old",
            "workspace_id": "w1",
            "pocket_id": "p1",
            "occurred_at": "2026-05-01T00:00:00+00:00",
        }
    )
    late = PocketOutcomeEvent(
        data={
            "outcome": "new",
            "workspace_id": "w1",
            "pocket_id": "p1",
            "occurred_at": "2026-05-22T00:00:00+00:00",
        }
    )
    await outcomes_service.record_outcome(early)
    await outcomes_service.record_outcome(late)
    recent = await outcomes_service.count_outcomes(
        "w1", CountOutcomesRequest(since="2026-05-10T00:00:00+00:00")
    )
    assert recent.total == 1
    assert recent.by_outcome == {"new": 1}


async def test_count_outcomes_empty_ledger_is_zero():
    assert (await outcomes_service.count_outcomes("never-seen")).total == 0


async def test_ledger_is_workspace_isolated():
    """One workspace's outcomes never leak into another's count."""
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w1", outcome="a"))
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w2", outcome="b"))
    assert (await outcomes_service.count_outcomes("w1")).total == 1
    assert (await outcomes_service.count_outcomes("w2")).total == 1


# ---------------------------------------------------------------------------
# Bus subscriber wiring — record_outcome on a real InProcessBus
# ---------------------------------------------------------------------------


async def test_subscriber_appends_when_event_published():
    """Registering record_outcome on an InProcessBus and publishing a
    pocket.outcome event appends a ledger row."""
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus

    async def _no_members(*_a, **_k):
        return []

    class _NoConn:
        async def send_to_user(self, *_a, **_k):
            return None

    resolver = AudienceResolver(
        group_members=_no_members,
        workspace_members=_no_members,
        workspace_admins=_no_members,
        workspace_peers=_no_members,
    )
    bus = InProcessBus(resolver=resolver, conn_manager=_NoConn())
    bus.subscribe("pocket.outcome", outcomes_service.record_outcome)
    await bus.publish(_outcome_event(workspace_id="w1", outcome="z"))

    counts = await outcomes_service.count_outcomes("w1")
    assert counts.total == 1
    assert counts.by_outcome == {"z": 1}


async def test_broken_subscriber_does_not_break_publish():
    """A broken co-subscriber on the same event must not stop the ledger
    subscriber — the bus isolates each handler's failure."""
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus

    async def _no_members(*_a, **_k):
        return []

    class _NoConn:
        async def send_to_user(self, *_a, **_k):
            return None

    async def _broken(_event):
        raise RuntimeError("subscriber blew up")

    bus = InProcessBus(
        resolver=AudienceResolver(
            group_members=_no_members,
            workspace_members=_no_members,
            workspace_admins=_no_members,
            workspace_peers=_no_members,
        ),
        conn_manager=_NoConn(),
    )
    # Broken subscriber first, ledger subscriber second.
    bus.subscribe("pocket.outcome", _broken)
    bus.subscribe("pocket.outcome", outcomes_service.record_outcome)
    # publish must not raise — the broken handler is logged + swallowed.
    await bus.publish(_outcome_event(workspace_id="w1", outcome="survived"))
    # The ledger subscriber still ran.
    assert (await outcomes_service.count_outcomes("w1")).total == 1


# ---------------------------------------------------------------------------
# GET /outcomes route
# ---------------------------------------------------------------------------


@pytest.fixture
def outcomes_client():
    """A TestClient over the outcomes router with auth + RBAC bypassed.

    ``current_active_user`` is overridden to a SimpleNamespace member;
    ``check_workspace_action`` is stubbed on its consumer module
    (``ee.cloud._core.deps`` — patching the source module is too late,
    the import binding already points at the original) so the
    ``outcomes.read`` guard passes. Same pattern as test_audit_router.py.
    """
    from types import SimpleNamespace

    from pocketpaw_ee.cloud._core import deps as core_deps
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.outcomes.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None

    user = SimpleNamespace(
        id="u1",
        active_workspace="w1",
        workspaces=[SimpleNamespace(workspace="w1", role="admin")],
    )

    async def _fake_user_dep():
        return user

    app.dependency_overrides[current_active_user] = _fake_user_dep

    _orig = core_deps.check_workspace_action
    core_deps.check_workspace_action = lambda *a, **k: None

    with TestClient(app) as client:
        yield client

    core_deps.check_workspace_action = _orig


async def test_get_outcomes_counts(outcomes_client):
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w1", outcome="a"))
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w1", outcome="a"))
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w1", outcome="b"))
    res = outcomes_client.get("/api/v1/outcomes")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 3
    assert body["by_outcome"] == {"a": 2, "b": 1}


def test_get_outcomes_rejects_workspace_id_query(outcomes_client):
    """workspace_id comes from auth context — a query param is rejected."""
    res = outcomes_client.get("/api/v1/outcomes?workspace_id=w2")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "outcomes.workspace_id_forbidden"


def test_event_type_is_pocket_outcome():
    """The Event subclass pins the `pocket.outcome` type literal."""
    ev = PocketOutcomeEvent(data={})
    assert ev.type == "pocket.outcome"
    assert isinstance(ev, Event)


async def test_ledger_json_shape():
    """A ledger row is one JSON object per line carrying every documented
    field — including the null Layer-4 billing slots."""
    await outcomes_service.record_outcome(_outcome_event(workspace_id="w1"))
    path = outcomes_service._ledger_path("w1")
    line = path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["outcome"] == "renewal_completed"
    assert row["workspace_id"] == "w1"
    assert row["outcome_value"] is None
    assert row["outcome_unit"] is None
