# tests/cloud/test_instinct_approvals_governance.py
# Created: 2026-08-06 (feat/coupling-template-approvals, T-5) — pins the
# governance record for TEMPLATE-level Instinct approvals: the one act in the
# product where a human authorises a whole CLASS of future writes, and which
# until now left no trace on /decisions, /activity, or any inbox.
#
# What each group protects (and the mutation that breaks it — every one was
# run via tests/mutations/instinct_approval_governance.json, not assumed):
#   * chain — a decided approval lands a real Decision row joinable by
#     correlation_id. Deleting the create-time ``agent.proposed`` open, or the
#     decide-time ``decision.completed`` terminal, kills the row: the projection
#     only materialises on a terminal AND drops any chain that never saw a
#     proposal.
#   * audit — the decision writes ``instinct_approval.<status>`` to the
#     workspace audit, which is what puts it on /activity.
#   * notify — creating an approval notifies the workspace owner + admins with
#     a PERSISTED row, so an owner who was offline still learns a decision is
#     waiting. Recipients resolve through the real ``list_admin_ids`` against
#     real membership rows, so the cross-tenant case is a real query, not a
#     stubbed one.
#   * resilience — a notification, journal, or audit failure never breaks the
#     approval flow. Removing any of those try/except wrappers fails here.
#   * tenancy — an approval in workspace A never notifies workspace B and never
#     appears in workspace B's audit or its Decision-Graph scope.
#   * wiring — ``mount_cloud`` actually subscribes the bridge. Without this a
#     reviewer could delete the registration and the whole suite would stay
#     green, because every other test in this file self-registers.
#
# Updated: 2026-08-06 (integration/coupling-sprint) — the wiring pin at the
# bottom now takes ``clean_bus_slate`` and drops its own restore. Four tests in
# this file (the three that count notifications, plus the control that asserts
# the UNregistered behaviour) failed when tests/cloud/test_integration.py ran
# first: its mount-pins each restored only their own topic, leaving this
# bridge's handler — and every other bridge ``mount_cloud`` registers —
# subscribed on the module-singleton bus. Restoration is now the autouse
# ``_isolated_bus_subscriptions`` fixture's job for every mounting test at once.

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service
from pocketpaw_ee.cloud.instinct_approvals.bridges import notifications as approval_bridge
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.notifications import service as notifications_service
from pocketpaw_ee.cloud.shared.events import event_bus
from soul_protocol.engine.journal import open_journal

import pocketpaw.journal_dep as journal_dep

pytestmark = pytest.mark.usefixtures("mongo_db")

WS_A = "ws-alpha"
WS_B = "ws-beta"
USER = "u-requester"
DECIDER = "u-decider"


# ---------------------------------------------------------------------------
# Fixtures — real journal + real projection singletons, real bridge subscriber
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    """Fresh on-disk journal wired into the lazy ``get_journal`` lookup that
    ``journal_writer`` resolves, so production code and the test read the SAME
    singleton (shape borrowed from tests/cloud/test_belt_trace.py)."""
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
def graph(tmp_path: Path):
    """Fresh DecisionGraph installed as the process-global singleton."""
    from pocketpaw_ee.cloud.decisions.service import (
        get_decision_graph,
        reset_projection_for_tests,
    )
    from pocketpaw_ee.cloud.decisions.store import set_db_path

    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def bridge_subscribed():
    """Subscribe the REAL bridge handler for the duration of one test.

    Registered explicitly rather than relying on ``mount_cloud`` so these tests
    stay unit-level — which is exactly why ``test_mount_cloud_subscribes_the_
    approval_notification_bridge`` below has to exist separately.
    """
    approval_bridge.register_instinct_approval_notification_listeners()
    yield
    event_bus.unsubscribe(
        approvals_service.CREATED_TOPIC, approval_bridge._on_instinct_approval_created
    )


def _create_body(**overrides) -> dict:
    body: dict = {
        "pocket_id": "pocket-1",
        "action_name": "refund_customer",
        "row_id": "row-9",
        "row_data": {"amount": 500},
        "verdict": "ESCALATE_APPROVAL",
        "reason": "operator_overlay_escalated",
        "matched_rules": [{"when": "amount > 100", "action": "require_approval"}],
        "park": {"method": "POST", "path": "/refunds"},
    }
    body.update(overrides)
    return body


async def _seed_members(workspace_id: str, *, prefix: str) -> dict[str, str]:
    """Create one owner, one admin and one plain member in ``workspace_id``.

    Real ``User`` rows with real memberships so ``list_admin_ids`` runs its
    actual ``$elemMatch`` query — a monkeypatched resolver would make the
    cross-tenant assertion prove nothing.
    """
    ids: dict[str, str] = {}
    for role in ("owner", "admin", "member"):
        user = User(
            email=f"{prefix}-{role}@test.local",
            hashed_password="x",
            workspaces=[WorkspaceMembership(workspace=workspace_id, role=role)],
        )
        await user.insert()
        ids[role] = str(user.id)
    return ids


def _chain(journal, correlation_id: UUID) -> list:
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


async def _audit_actions(workspace_id: str) -> list[str]:
    from pocketpaw_ee.cloud.audit import service as audit_service

    page = await audit_service.list_events(workspace_id, {})
    return [e.action for e in page.items]


# ---------------------------------------------------------------------------
# (a) the decision lands a journal chain joinable by correlation_id
# ---------------------------------------------------------------------------


async def test_create_stamps_a_correlation_id_on_the_row(journal, graph) -> None:
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())

    assert wire["correlation_id"], "create must mint a chain id — without one nothing joins"
    UUID(wire["correlation_id"])  # parses, i.e. it is a real chain id

    fetched = await approvals_service.get_approval(WS_A, USER, wire["id"])
    assert fetched["correlation_id"] == wire["correlation_id"], "the id must be PERSISTED"


async def test_approve_writes_a_journal_chain_joinable_by_correlation_id(journal, graph) -> None:
    """Mutation: drop the ``agent.proposed`` open in ``_open_chain`` and the
    terminal is discarded ("closed without proposed event"), so no Decision row
    exists. Drop ``record_decision_completed`` and the chain never closes."""
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    corr = UUID(wire["correlation_id"])

    await approvals_service.approve(WS_A, DECIDER, wire["id"], {"note": "cleared by finance"})

    actions = [e.action for e in _chain(journal, corr)]
    assert "agent.proposed" in actions
    assert "human.corrected" in actions
    assert "decision.completed" in actions

    corrected = next(e for e in _chain(journal, corr) if e.action == "human.corrected")
    assert corrected.payload["disposition"] == "accepted"
    assert corrected.payload["approval_id"] == wire["id"]
    assert corrected.actor.id == f"user:{DECIDER}", "the DECIDING user is the actor"
    assert f"workspace:{WS_A}" in corrected.scope

    # The chain actually materialised a Decision — this is what /decisions reads.
    decisions = [d for d in graph.store.iter_decisions() if d.correlation_id == corr]
    assert len(decisions) == 1, "a decided approval must be joinable on /decisions"
    assert any(a.actor.id == f"user:{DECIDER}" for a in decisions[0].approvers)


async def test_approved_decision_reads_policy_passed(journal, graph) -> None:
    """An APPROVED template approval must not read as blocked forever.

    The projection keeps the LAST ``policy.evaluated`` before the terminal
    (``_fold_policy``, last-seen-wins), and create's honest ``passed=False``
    encodes "template escalated to human". Without the approve-side
    ``passed=True`` flip, the explain narrator says the gate "blocked at
    decision time" for a decision a human explicitly cleared, and the
    outcome-hint ranker files it under the rejected hint. fail→pass is the
    projection's own encoding of "asked for human → human approved", and the
    row-level approve path emits the same flip.

    Mutation: drop the ``record_policy_evaluated`` flip in ``_close_chain``.
    """
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    corr = UUID(wire["correlation_id"])
    await approvals_service.approve(WS_A, DECIDER, wire["id"], {})

    # The chain carries the full fail→pass arc for the narrator...
    policy_events = [e for e in _chain(journal, corr) if e.action == "policy.evaluated"]
    assert [bool(e.payload.get("passed")) for e in policy_events] == [False, True]
    assert policy_events[-1].payload["policy"] == "template_instinct_gate"
    assert policy_events[-1].payload["reason"] == "approved_by_human"

    # ...and the folded Decision row reads as passed, not blocked.
    decision = next(d for d in graph.store.iter_decisions() if d.correlation_id == corr)
    assert decision.instinct_policy_passed is True, (
        "an approved template approval must not read as policy-blocked"
    )


async def test_rejected_decision_reads_policy_failed(journal, graph) -> None:
    """The flip is approve-only: on reject, create's ``passed=False`` stands,
    which IS the correct final policy state for a rejection.

    Mutation: make ``_close_chain`` emit the flip unconditionally (``approved
    = True``) — this asserts a rejected Decision would then wrongly read
    passed.
    """
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    corr = UUID(wire["correlation_id"])
    await approvals_service.reject(WS_A, DECIDER, wire["id"], {"note": "no"})

    policy_events = [e for e in _chain(journal, corr) if e.action == "policy.evaluated"]
    assert [bool(e.payload.get("passed")) for e in policy_events] == [False]

    decision = next(d for d in graph.store.iter_decisions() if d.correlation_id == corr)
    assert decision.instinct_policy_passed is False, (
        "a rejected template approval must keep reading as policy-blocked"
    )


async def test_reject_closes_the_chain_as_rejected(journal, graph) -> None:
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    corr = UUID(wire["correlation_id"])

    await approvals_service.reject(WS_A, DECIDER, wire["id"], {"note": "too risky"})

    terminal = next(e for e in _chain(journal, corr) if e.action == "decision.completed")
    assert terminal.payload["passed"] is False
    assert terminal.payload["action_outcome"] == "rejected"

    corrected = next(e for e in _chain(journal, corr) if e.action == "human.corrected")
    assert corrected.payload["disposition"] == "rejected"

    assert len([d for d in graph.store.iter_decisions() if d.correlation_id == corr]) == 1


# ---------------------------------------------------------------------------
# (b) the decision writes a workspace audit event (this is /activity)
# ---------------------------------------------------------------------------


async def test_approve_writes_an_audit_event(journal, graph) -> None:
    """Mutation: delete the ``_record_audit_safe`` call in ``_decide`` and the
    decision vanishes from /activity."""
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    await approvals_service.approve(WS_A, DECIDER, wire["id"], {})

    from pocketpaw_ee.cloud.audit import service as audit_service

    page = await audit_service.list_events(WS_A, {"action": "instinct_approval.approved"})
    assert len(page.items) == 1
    row = page.items[0]
    assert row.actor_id == DECIDER
    assert row.target_id == wire["id"]
    assert row.metadata["correlation_id"] == wire["correlation_id"]
    assert row.metadata["action_name"] == "refund_customer"


async def test_reject_writes_an_audit_event(journal, graph) -> None:
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    await approvals_service.reject(WS_A, DECIDER, wire["id"], {"note": "no"})

    assert "instinct_approval.rejected" in await _audit_actions(WS_A)


# ---------------------------------------------------------------------------
# (c) creating an approval notifies the workspace owner + admins
# ---------------------------------------------------------------------------


async def test_create_notifies_owner_and_admins(bridge_subscribed) -> None:
    """Mutation: delete the ``_publish_created_safe`` call in ``create_approval``
    and an offline owner is never told a decision is waiting."""
    ids = await _seed_members(WS_A, prefix="alpha")

    wire = await approvals_service.create_approval(WS_A, USER, _create_body())

    for role in ("owner", "admin"):
        got = await notifications_service.list_for_user(ids[role])
        assert len(got) == 1, f"{role} must be notified"
        note = got[0]
        assert note.kind == "approval_pending"
        assert note.workspace_id == WS_A
        assert note.source is not None
        assert note.source.type == "instinct_action"
        assert note.source.id == wire["id"]
        assert note.source.pocket_id == "pocket-1"

    assert await notifications_service.list_for_user(ids["member"]) == [], (
        "a plain member is not an approver — notifying them is noise, not governance"
    )


async def test_create_does_not_notify_without_the_bridge_registered() -> None:
    """Control for the test above: with no subscriber nothing is written, which
    is what makes the mount-wiring pin below meaningful."""
    ids = await _seed_members(WS_A, prefix="alpha")
    await approvals_service.create_approval(WS_A, USER, _create_body())
    assert await notifications_service.list_for_user(ids["owner"]) == []


# ---------------------------------------------------------------------------
# (d) no governance write may break the approval flow
# ---------------------------------------------------------------------------


async def test_notification_failure_does_not_break_create(bridge_subscribed, monkeypatch) -> None:
    """Mutation: remove the try/except in ``bridges.notifications._create`` and
    a dead notification sink starts failing approvals."""
    await _seed_members(WS_A, prefix="alpha")

    async def _boom(**_kwargs):
        raise RuntimeError("notification sink down")

    monkeypatch.setattr(notifications_service, "create", _boom)

    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    assert wire["status"] == "pending"
    # The row is really there — the approval was not rolled back.
    assert (await approvals_service.get_approval(WS_A, USER, wire["id"]))["id"] == wire["id"]


async def test_one_failing_recipient_does_not_cost_the_others(
    bridge_subscribed, monkeypatch
) -> None:
    """The per-recipient wrap earns its keep here, and ONLY here.

    Every other resilience test in this file is also satisfied by the bus's own
    handler guard, so none of them can tell whether ``_create``'s try/except
    exists. This one can: if the first recipient's failure propagates, the
    fan-out loop aborts and the remaining admins are never notified.

    Mutation: narrow ``bridges.notifications._create``'s ``except Exception``
    and the second admin loses their notification.
    """
    await _seed_members(WS_A, prefix="alpha")

    real_create = notifications_service.create
    calls = {"n": 0}

    async def _first_call_explodes(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("notification sink down for this recipient")
        return await real_create(**kwargs)

    monkeypatch.setattr(notifications_service, "create", _first_call_explodes)

    await approvals_service.create_approval(WS_A, USER, _create_body())

    assert calls["n"] == 2, "both admins must be attempted"


async def test_recipient_lookup_failure_is_contained_inside_the_bridge(monkeypatch) -> None:
    """Called DIRECTLY, not through the bus, on purpose.

    Through the bus, ``event_bus.emit``'s guard would swallow the exception and
    the test would pass whether or not ``_workspace_admin_ids`` has its own
    try/except — a gate nothing can break is a gate nothing proves. Invoking the
    handler directly removes the outer net.

    Mutation: narrow ``_workspace_admin_ids``'s ``except Exception``.
    """
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def _boom(_workspace_id):
        raise RuntimeError("membership query down")

    monkeypatch.setattr(workspace_service, "list_admin_ids", _boom)

    # Must return normally — no exception reaches the caller.
    await approval_bridge._on_instinct_approval_created(
        {"workspace_id": WS_A, "id": "approval-1", "pocket_id": "pocket-1"}
    )


async def test_recipient_lookup_failure_does_not_break_create(
    bridge_subscribed, monkeypatch
) -> None:
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def _boom(_workspace_id):
        raise RuntimeError("membership query down")

    monkeypatch.setattr(workspace_service, "list_admin_ids", _boom)

    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    assert wire["status"] == "pending"


async def test_a_broken_subscriber_does_not_starve_the_next_one() -> None:
    """A raising subscriber must not cost the approval OR its siblings.

    The sibling half is what pins the bus's own per-handler guard: with the
    guard narrowed, the first raiser aborts the fan-out loop and the bridge
    (registered after it) never runs — which is precisely how a notification
    would go missing without anything looking broken.
    """
    seen: list[dict] = []

    async def _boom(_data):
        raise RuntimeError("subscriber exploded")

    async def _after(data):
        seen.append(data)

    event_bus.subscribe(approvals_service.CREATED_TOPIC, _boom)
    event_bus.subscribe(approvals_service.CREATED_TOPIC, _after)
    try:
        wire = await approvals_service.create_approval(WS_A, USER, _create_body())
        assert wire["status"] == "pending"
        assert len(seen) == 1, "a raising subscriber must not stop the next one"
        assert seen[0]["id"] == wire["id"]
    finally:
        event_bus.unsubscribe(approvals_service.CREATED_TOPIC, _boom)
        event_bus.unsubscribe(approvals_service.CREATED_TOPIC, _after)


async def test_a_bus_level_failure_does_not_break_create(monkeypatch) -> None:
    """The bus guards its handlers, but nothing guards the bus call itself —
    that is ``_publish_created_safe``'s job.

    Mutation: narrow ``_publish_created_safe``'s ``except Exception``.
    """

    async def _boom(_topic, _data):
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus, "emit", _boom)

    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    assert wire["status"] == "pending"


async def test_journal_failure_does_not_break_decide(journal, graph, monkeypatch) -> None:
    """Mutation: remove the try/except in ``_safe_chain_emit`` and a
    Decision-Graph hiccup starts rejecting operators' approvals."""
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())

    import pocketpaw_ee.cloud.decisions.journal_writer as jw

    def _boom(**_kwargs):
        raise RuntimeError("journal locked")

    monkeypatch.setattr(jw, "record_human_corrected", _boom)
    monkeypatch.setattr(jw, "record_decision_completed", _boom)

    out = await approvals_service.approve(WS_A, DECIDER, wire["id"], {})
    assert out["status"] == "approved"
    assert (await approvals_service.get_approval(WS_A, USER, wire["id"]))["status"] == "approved"


async def test_audit_failure_does_not_break_decide(journal, graph, monkeypatch) -> None:
    """Mutation: remove the try/except in ``_record_audit_safe``."""
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())

    from pocketpaw_ee.cloud.audit import service as audit_service

    async def _boom(**_kwargs):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(audit_service, "record", _boom)

    out = await approvals_service.approve(WS_A, DECIDER, wire["id"], {})
    assert out["status"] == "approved"


async def test_chain_emit_failure_at_create_does_not_break_create(monkeypatch) -> None:
    import pocketpaw_ee.cloud.decisions.journal_writer as jw

    def _boom(**_kwargs):
        raise RuntimeError("journal locked")

    monkeypatch.setattr(jw, "record_agent_proposed", _boom)
    monkeypatch.setattr(jw, "record_policy_evaluated", _boom)

    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    assert wire["status"] == "pending"


async def test_decide_survives_a_row_with_no_correlation_id(journal, graph) -> None:
    """Rows written before T-5 carry ``correlation_id=""``. Deciding one must
    still work — it simply has no chain to close."""
    from pocketpaw_ee.cloud.models.instinct_approval import InstinctApproval as _Doc

    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    doc = await _Doc.find_one({"workspace": WS_A})
    doc.correlation_id = ""
    await doc.save()

    out = await approvals_service.approve(WS_A, DECIDER, wire["id"], {})
    assert out["status"] == "approved"
    assert [e for e in journal.replay_from(0) if e.action == "human.corrected"] == []


# ---------------------------------------------------------------------------
# (e) cross-tenant — workspace B learns nothing about workspace A's approval
# ---------------------------------------------------------------------------


async def test_approval_in_workspace_a_never_notifies_workspace_b(bridge_subscribed) -> None:
    """Mutation: point ``_workspace_admin_ids`` at ``list_member_ids`` for a
    hard-coded workspace, or drop the workspace filter, and B's owner is paged
    about A's decision."""
    a_ids = await _seed_members(WS_A, prefix="alpha")
    b_ids = await _seed_members(WS_B, prefix="beta")

    await approvals_service.create_approval(WS_A, USER, _create_body())

    assert len(await notifications_service.list_for_user(a_ids["owner"])) == 1
    for role in ("owner", "admin", "member"):
        assert await notifications_service.list_for_user(b_ids[role]) == [], (
            f"workspace B's {role} must not see workspace A's approval"
        )


async def test_decision_in_workspace_a_never_appears_in_workspace_b(journal, graph) -> None:
    wire = await approvals_service.create_approval(WS_A, USER, _create_body())
    corr = UUID(wire["correlation_id"])
    await approvals_service.approve(WS_A, DECIDER, wire["id"], {})

    # Audit: the row is filed under A only.
    assert "instinct_approval.approved" in await _audit_actions(WS_A)
    assert await _audit_actions(WS_B) == []

    # Chain: every event is scoped to A, which is what the projection's
    # visibility filter intersects against a requester's scopes.
    for entry in _chain(journal, corr):
        assert f"workspace:{WS_A}" in entry.scope
        assert f"workspace:{WS_B}" not in entry.scope
        assert f"workspace:{WS_B}" not in entry.actor.scope_context


async def test_a_workspace_b_decider_cannot_decide_a_workspace_a_approval(journal, graph) -> None:
    """Pre-existing tenancy guard, re-pinned here because the new journal +
    audit writes now hang off the decide path — a leak would forge governance
    records in the victim's workspace, not just flip a row."""
    from pocketpaw_ee.cloud._core.errors import NotFound

    wire = await approvals_service.create_approval(WS_A, USER, _create_body())

    with pytest.raises(NotFound):
        await approvals_service.approve(WS_B, DECIDER, wire["id"], {})

    assert await _audit_actions(WS_B) == []
    assert [e for e in journal.replay_from(0) if e.action == "human.corrected"] == []


# ---------------------------------------------------------------------------
# wiring — mount_cloud really subscribes the bridge
# ---------------------------------------------------------------------------


def test_mount_cloud_subscribes_the_approval_notification_bridge(
    journal, graph, clean_bus_slate
) -> None:
    """Every other test here self-registers the bridge, so deleting the
    ``mount_cloud`` registration would leave the suite green and the feature
    dead in production. This asserts the real handler is subscribed to the real
    topic AFTER mount.

    Mutation: remove the
    ``register_instinct_approval_notification_listeners()`` call from
    ``ee/pocketpaw_ee/cloud/__init__.py`` — this test fails.

    It takes ``journal`` + ``graph`` for HERMETICITY, not for the assertion:
    ``mount_cloud`` runs ``init_decisions_projection(rebuild_from_journal=True)``,
    which without those fixtures replays the DEVELOPER'S real
    ``~/.soul/journal.db`` from seq 0 (this test measured 416s that way against
    5s with them — and it was reading a real machine's journal to do it).

    ``clean_bus_slate`` empties the topic so the assertion measures THIS mount:
    without it a handler left by an earlier test satisfies the ``in`` check and
    the pin passes with the production registration deleted. Restoring the
    subscribers is the autouse ``_isolated_bus_subscriptions`` fixture's job —
    the local ``finally`` this replaces put back only ``CREATED_TOPIC`` and
    left every other bridge ``mount_cloud`` registers subscribed for the rest
    of the session.
    """
    from fastapi import FastAPI
    from pocketpaw_ee.cloud import mount_cloud

    mount_cloud(FastAPI())

    handlers = event_bus._handlers.get(approvals_service.CREATED_TOPIC, [])
    assert approval_bridge._on_instinct_approval_created in handlers, (
        "mount_cloud must subscribe the approval → notification bridge"
    )
