# tests/test_agent_ledger.py — the agent ledger spine (AL-1).
# Created: 2026-07-31. Covers the store, the vocabulary, the attribution field,
# and the Instinct emitter, in the layers that actually protect the design:
#
#   * Vocabulary: a core kind is accepted, a vendor kind is accepted, an unknown
#     paw.* kind and a malformed id are REJECTED. The closed core is a promise
#     the model boundary has to keep, not a comment.
#   * Store: round-trip, the UNIQUE(kind, ref) dedupe that makes a replayed
#     approve idempotent, workspace scoping, and the SQL aggregates (a board that
#     computed its counts differently from its rows is the chart-vs-wallet bug).
#   * Attribution: actor_agent_id survives propose -> read, and a LEGACY
#     instinct.db that predates the column migrates additively instead of raising
#     "no such column" on the next list.
#   * Emitter: an approval lands exactly one agent-keyed row, a reject and an
#     execute land theirs, a replayed approve does NOT double-write, and — the
#     load-bearing one — A RAISING LEDGER STORE DOES NOT BREAK THE APPROVAL.
#     That last test is the whole fail-soft contract; if it ever goes red, the
#     ledger has started charging an operator's click for its own bookkeeping.
#
# Store isolation: every test that goes through the FACTORY points
# ``stores._DATA_DIR`` at tmp_path and resets the caches, so nothing here ever
# reads or writes the developer's real ~/.pocketpaw.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from pocketpaw import stores
from pocketpaw.agent_ledger.models import (
    KIND_RUN_COMPLETED,
    ATTR_AGENT_ID,
    ATTR_APPROVAL_AUTO,
    ATTR_INSTINCT_EVENT,
    KIND_ACTION_APPROVED,
    KIND_ACTION_OUTCOME,
    KIND_ACTION_REJECTED,
    KIND_VISITOR_ACTION,
    SURFACE_BELT,
    SURFACE_CHAT,
    SURFACE_INSTINCT,
    SURFACE_PAW_BAR,
    LedgerActor,
    LedgerKindValidationError,
    LedgerRow,
    WindowParseError,
    parse_window,
    surface_from_trigger,
    validate_ledger_kind,
    window_start,
)
from pocketpaw.agent_ledger.store import AgentLedgerStore
from pocketpaw.instinct.models import (
    ActionStatus,
    ActionTrigger,
    OutcomeStatus,
    OutcomeVerdict,
)
from pocketpaw.instinct.store import InstinctStore

# --------------------------------------------------------------------------- #
# Fixtures + builders
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Point the store factory at tmp_path with a clean cache and no scope flag.

    Same shape as tests/test_instinct_workspace_isolation.py. Load-bearing for
    every emitter test: the emitter resolves its ledger through the FACTORY, so
    without this it would write into the developer's real home directory.
    """
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    stores.reset_store_caches()
    token = stores.current_workspace.set(None)
    try:
        yield tmp_path
    finally:
        stores.current_workspace.reset(token)
        stores.reset_store_caches()


def _row(**overrides) -> LedgerRow:
    base = dict(
        agent_id="agent-1",
        workspace_id="ws1",
        surface=SURFACE_PAW_BAR,
        kind=KIND_ACTION_APPROVED,
        ref="act_1",
        actor=LedgerActor.OWNER.value,
    )
    base.update(overrides)
    return LedgerRow(**base)


def _trigger(source: str = "paw_bar:widget-1") -> ActionTrigger:
    return ActionTrigger(type="connector", source=source, reason="ledger spine test")


async def _propose(store: InstinctStore, **overrides):
    kwargs = dict(
        pocket_id="pocket-1",
        title="Answer the customer",
        description="",
        recommendation="On it.",
        trigger=_trigger(),
        workspace_id="ws1",
        actor_agent_id="agent-1",
    )
    kwargs.update(overrides)
    return await store.propose(**kwargs)


# --------------------------------------------------------------------------- #
# Vocabulary — the closed core / open extension rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind",
    [
        KIND_ACTION_APPROVED,
        KIND_ACTION_REJECTED,
        KIND_ACTION_OUTCOME,
        KIND_VISITOR_ACTION,
        "paw.handoff.raised",
        "paw.conversation.takeover",
        "paw.run.completed",
    ],
)
def test_core_kinds_are_accepted(kind):
    assert validate_ledger_kind(kind) == kind


@pytest.mark.parametrize("kind", ["acme.crm.synced", "vendor.billing.charged"])
def test_vendor_kinds_are_accepted(kind):
    """The extension space is open — a third-party surface needs no core PR."""
    assert validate_ledger_kind(kind) == kind


@pytest.mark.parametrize(
    "kind",
    [
        "paw.action.invented",  # well-formed but NOT in the closed core
        "paw.action",  # too few segments
        "paw.Action.Approved",  # uppercase
        "notevenclose",
        "",
    ],
)
def test_garbage_kinds_are_rejected(kind):
    with pytest.raises(LedgerKindValidationError):
        validate_ledger_kind(kind)


def test_row_model_enforces_the_vocabulary():
    """Validation lives at the model boundary, so no emitter can skip it."""
    with pytest.raises(Exception, match="unknown core ledger kind"):
        _row(kind="paw.action.invented")


@pytest.mark.parametrize(
    ("trigger_type", "source", "expected"),
    [
        ("connector", "paw_bar:widget-9", SURFACE_PAW_BAR),
        ("automation", "belt:develop", SURFACE_BELT),
        ("agent", "claude", SURFACE_CHAT),
        ("user", "admin_action", SURFACE_INSTINCT),
        ("", "", SURFACE_INSTINCT),
    ],
)
def test_surface_is_derived_from_the_trigger(trigger_type, source, expected):
    """An unrecognised trigger falls back to `instinct` rather than guessing."""
    assert surface_from_trigger(trigger_type, source) == expected


@pytest.mark.parametrize("window", ["24h", "7d", "30d", "2w"])
def test_windows_parse(window):
    assert parse_window(window) is not None


def test_all_window_is_unbounded():
    assert parse_window("all") is None
    assert window_start("all") is None


@pytest.mark.parametrize("window", ["", "30", "d30", "0d", "-5d", "9999d", "30x"])
def test_bad_windows_raise_instead_of_falling_back(window):
    """A misread window must be a refusal, never a silently different answer."""
    with pytest.raises(WindowParseError):
        parse_window(window)


@pytest.mark.parametrize("window", ["99999999999d", "1000000000d", "999999999999w", "99999999999h"])
def test_absurd_windows_are_a_refusal_not_a_server_error(window):
    """An over-large window must be WindowParseError (422), never OverflowError (500).

    Regression, found in review: the max-window check ran AFTER the timedelta was
    built, and timedelta raises OverflowError — not ValueError — past C-int range
    or its own 999999999-day cap. OverflowError is not a WindowParseError, so it
    sailed through the router's 422 handler and surfaced as a 500. Verified live
    before the fix: '9999d' → 422 but '99999999999d' → 500. The parametrize above
    stopped one order of magnitude short of the bug, which is why this gets its
    own case rather than another entry in that list.
    """
    with pytest.raises(WindowParseError):
        parse_window(window)


def test_the_ledger_is_routed_beside_the_store_that_emitted_it(tmp_path):
    """Route by the sibling PATH, never by re-resolving an in-row workspace value.

    Regression, found in review: the emitter passed the action's in-row
    ``workspace_id`` back through the store factory. That column is not
    guaranteed to be a store-path token — the paw-bar decision loop deliberately
    lets it fall back to the widget OWNER label (colon-qualified) for in-row scope
    only. Feeding that to the factory either raises inside the path allowlist
    (swallowed by the emitter's fail-soft guard, so the row vanishes silently) or
    writes into a directory keyed by a user id no reader opens. Either way the
    primary producer under-counts and nothing says so.
    """
    from pocketpaw.stores import get_agent_ledger_store_beside

    instinct_db = tmp_path / "workspaces" / "wreal01" / "instinct.db"
    instinct_db.parent.mkdir(parents=True)

    store = get_agent_ledger_store_beside(instinct_db)

    assert Path(store._db_path).parent == instinct_db.parent
    assert Path(store._db_path).name == "agent_ledger.db"
    # A str path must resolve identically — the emitter holds ``self._db_path``.
    assert get_agent_ledger_store_beside(str(instinct_db))._db_path == store._db_path


# --------------------------------------------------------------------------- #
# Store — round-trip, dedupe, tenancy, aggregates
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_and_query_round_trip(tmp_path):
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    assert await store.append(_row(attrs={ATTR_AGENT_ID: "agent-1"})) is True

    rows = await store.query(agent_id="agent-1")
    assert len(rows) == 1
    row = rows[0]
    assert row.id is not None
    assert row.agent_id == "agent-1"
    assert row.workspace_id == "ws1"
    assert row.surface == SURFACE_PAW_BAR
    assert row.kind == KIND_ACTION_APPROVED
    assert row.ref == "act_1"
    assert row.attrs == {ATTR_AGENT_ID: "agent-1"}


@pytest.mark.asyncio
async def test_unique_kind_ref_dedupes_a_replayed_write(tmp_path):
    """A replayed approve must not double-count. The DB enforces it, not callers."""
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    assert await store.append(_row(ref="act_replay")) is True
    # Same (kind, ref), different everything else — still a duplicate beat.
    assert await store.append(_row(ref="act_replay", actor="system")) is False

    rows = await store.query(agent_id="agent-1")
    assert len(rows) == 1
    # The FIRST write wins; a replay never rewrites history in an append-only log.
    assert rows[0].actor == LedgerActor.OWNER.value


@pytest.mark.asyncio
async def test_same_ref_under_a_different_kind_is_a_different_beat(tmp_path):
    """The dedupe key is (kind, ref) — one action legitimately has several beats."""
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    assert await store.append(_row(kind=KIND_ACTION_APPROVED, ref="act_9")) is True
    assert await store.append(_row(kind=KIND_ACTION_OUTCOME, ref="act_9")) is True
    assert len(await store.query(agent_id="agent-1")) == 2


@pytest.mark.asyncio
async def test_workspace_scoping_is_strict(tmp_path):
    """One tenant's query never returns another tenant's rows."""
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    await store.append(_row(ref="a1", workspace_id="ws1"))
    await store.append(_row(ref="a2", workspace_id="ws2"))

    assert [r.ref for r in await store.query(workspace_id="ws1")] == ["a1"]
    assert [r.ref for r in await store.query(workspace_id="ws2")] == ["a2"]
    assert len(await store.query(workspace_id="ws-nobody")) == 0
    # Unscoped reads still see everything (the OSS single-tenant path).
    assert len(await store.query()) == 2


@pytest.mark.asyncio
async def test_query_filters_by_kind_and_window(tmp_path):
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    await store.append(_row(ref="old", ts="2020-01-01T00:00:00+00:00"))
    await store.append(_row(ref="new", kind=KIND_ACTION_REJECTED))

    assert [r.ref for r in await store.query(kinds=[KIND_ACTION_REJECTED])] == ["new"]
    recent = await store.query(since=window_start("30d"))
    assert [r.ref for r in recent] == ["new"]


@pytest.mark.asyncio
async def test_naive_timestamps_are_normalized_to_utc(tmp_path):
    """The window filter compares strings, so every row must share one offset."""
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    await store.append(_row(ref="naive", ts="2026-07-31T12:00:00"))
    rows = await store.query()
    assert rows[0].ts.endswith("+00:00")


@pytest.mark.asyncio
async def test_aggregates_match_the_rows_they_summarize(tmp_path):
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    await store.append(_row(ref="a1", kind=KIND_ACTION_APPROVED))
    await store.append(_row(ref="a2", kind=KIND_ACTION_REJECTED))
    await store.append(_row(ref="a3", kind=KIND_ACTION_OUTCOME, outcome=OutcomeStatus.SOLVED.value))
    await store.append(
        _row(
            ref="a4",
            kind=KIND_VISITOR_ACTION,
            value_cents=1250,
            currency="USD",
            actor=LedgerActor.VISITOR.value,
        )
    )

    counts = await store.counts_by_kind(agent_id="agent-1")
    assert counts == {
        KIND_ACTION_APPROVED: 1,
        KIND_ACTION_REJECTED: 1,
        KIND_ACTION_OUTCOME: 1,
        KIND_VISITOR_ACTION: 1,
    }
    # Rows WITHOUT a verdict stay out of the outcome denominator.
    assert await store.counts_by_outcome(agent_id="agent-1") == {OutcomeStatus.SOLVED.value: 1}
    assert await store.value_by_currency(agent_id="agent-1") == {"USD": 1250}


@pytest.mark.asyncio
async def test_value_is_grouped_by_currency_never_summed_across(tmp_path):
    """Adding cents to pence yields a number that is wrong invisibly."""
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    await store.append(_row(ref="v1", kind=KIND_VISITOR_ACTION, value_cents=100, currency="USD"))
    await store.append(_row(ref="v2", kind=KIND_VISITOR_ACTION, value_cents=200, currency="GBP"))
    assert await store.value_by_currency(agent_id="agent-1") == {"USD": 100, "GBP": 200}


@pytest.mark.asyncio
async def test_factory_gives_each_workspace_its_own_file(isolated_stores):
    """Physical isolation, inherited from the generic workspace-keyed factory."""
    a = stores.get_agent_ledger_store(workspace_id="wsA")
    b = stores.get_agent_ledger_store(workspace_id="wsB")
    assert a._db_path != b._db_path
    assert "wsA" in a._db_path and "wsB" in b._db_path
    # Same workspace resolves to the same cached handle.
    assert stores.get_agent_ledger_store(workspace_id="wsA") is a


@pytest.mark.asyncio
async def test_factory_fails_closed_without_a_workspace_in_cloud_mode(isolated_stores, monkeypatch):
    """Cloud mode must never fall back to a shared ledger across tenants."""
    monkeypatch.setenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", "1")
    stores.reset_store_caches()
    with pytest.raises(stores.WorkspaceScopeRequired):
        stores.get_agent_ledger_store()


# --------------------------------------------------------------------------- #
# Attribution — Action.actor_agent_id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_actor_agent_id_round_trips_through_propose(tmp_path):
    store = InstinctStore(tmp_path / "instinct.db")
    action = await _propose(store, actor_agent_id="agent-77")
    assert action.actor_agent_id == "agent-77"
    reloaded = await store.get_action(action.id)
    assert reloaded is not None
    assert reloaded.actor_agent_id == "agent-77"


@pytest.mark.asyncio
async def test_unattributed_proposals_stay_legal(tmp_path):
    """ "" is a permanent, supported value — never a validation failure."""
    store = InstinctStore(tmp_path / "instinct.db")
    action = await _propose(store, actor_agent_id="")
    reloaded = await store.get_action(action.id)
    assert reloaded is not None
    assert reloaded.actor_agent_id == ""


@pytest.mark.asyncio
async def test_legacy_instinct_db_migrates_the_new_column(tmp_path):
    """A DB created before AL-1 gains actor_agent_id via the additive ALTER.

    The trap this guards is the one that has bitten paw_bar twice: CREATE TABLE
    IF NOT EXISTS no-ops on a deployed table, so a row read afterwards raises on
    the missing column unless the migration ran first.
    """
    db_path = tmp_path / "instinct.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE instinct_actions ("
            " id TEXT PRIMARY KEY, pocket_id TEXT NOT NULL, title TEXT NOT NULL,"
            " description TEXT DEFAULT '', category TEXT DEFAULT 'workflow',"
            " status TEXT DEFAULT 'pending', priority TEXT DEFAULT 'medium',"
            " trigger TEXT NOT NULL, recommendation TEXT DEFAULT '',"
            " parameters TEXT DEFAULT '{}', context TEXT DEFAULT '{}',"
            " outcome TEXT, error TEXT, approved_by TEXT, approved_at TEXT,"
            " rejected_reason TEXT,"
            " created_at TEXT DEFAULT (datetime('now')),"
            " updated_at TEXT DEFAULT (datetime('now')), executed_at TEXT)"
        )
        await db.commit()

    store = InstinctStore(db_path)
    action = await _propose(store, actor_agent_id="agent-legacy")
    reloaded = await store.get_action(action.id)
    assert reloaded is not None
    assert reloaded.actor_agent_id == "agent-legacy"
    # And the list path (which rebuilds every row) survives too.
    assert len(await store.list_actions()) == 1


# --------------------------------------------------------------------------- #
# Emitter — the Instinct choke point
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_approval_lands_one_agent_keyed_row(isolated_stores):
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct, actor_agent_id="agent-42")

    approved = await instinct.approve(action.id, approver="user:maya")
    assert approved is not None
    assert approved.status == ActionStatus.APPROVED

    ledger = stores.get_agent_ledger_store(workspace_id="ws1")
    rows = await ledger.query(agent_id="agent-42")
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == KIND_ACTION_APPROVED
    assert row.ref == action.id
    assert row.workspace_id == "ws1"
    # The trigger was paw_bar:*, so the row knows which surface it came from.
    assert row.surface == SURFACE_PAW_BAR
    assert row.actor == LedgerActor.OWNER.value
    assert row.attrs[ATTR_AGENT_ID] == "agent-42"
    assert row.attrs[ATTR_INSTINCT_EVENT] == "action_approved"
    # Ops metrics are federated — they must never appear on a ledger row.
    assert not any(k in row.attrs for k in ("tokens", "cost", "latency", "gen_ai.usage"))


@pytest.mark.asyncio
async def test_a_replayed_approve_does_not_double_write(isolated_stores):
    """The second approve is a no-op upstream AND absorbed downstream."""
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct)
    assert await instinct.approve(action.id) is not None
    # approve() requires PENDING, so the replay returns None...
    assert await instinct.approve(action.id) is None
    # ...and even a direct re-emit of the same beat cannot duplicate the row.
    reloaded = await instinct.get_action(action.id)
    await instinct._emit_ledger(
        reloaded, event="action_approved", actor="user:maya", workspace_id="ws1"
    )

    ledger = stores.get_agent_ledger_store(workspace_id="ws1")
    assert len(await ledger.query(kinds=[KIND_ACTION_APPROVED])) == 1


@pytest.mark.asyncio
async def test_reject_and_outcome_land_their_own_kinds(isolated_stores):
    instinct = stores.get_instinct_store(workspace_id="ws1")
    rejected_action = await _propose(instinct)
    await instinct.reject(rejected_action.id, reason="wholesale", rejector="user:maya")

    executed_action = await _propose(instinct)
    await instinct.approve(executed_action.id)
    await instinct.mark_executed(
        executed_action.id,
        OutcomeVerdict(status=OutcomeStatus.SOLVED, summary="delivered"),
    )

    ledger = stores.get_agent_ledger_store(workspace_id="ws1")
    counts = await ledger.counts_by_kind(agent_id="agent-1")
    assert counts[KIND_ACTION_REJECTED] == 1
    assert counts[KIND_ACTION_APPROVED] == 1
    assert counts[KIND_ACTION_OUTCOME] == 1
    assert await ledger.counts_by_outcome(agent_id="agent-1") == {OutcomeStatus.SOLVED.value: 1}


@pytest.mark.asyncio
async def test_a_failed_action_records_a_not_solved_verdict(isolated_stores):
    """A board that counts only successes is one people stop trusting."""
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct)
    await instinct.approve(action.id)
    await instinct.mark_failed(action.id, error="connector timeout")

    ledger = stores.get_agent_ledger_store(workspace_id="ws1")
    assert await ledger.counts_by_outcome(agent_id="agent-1") == {OutcomeStatus.NOT_SOLVED.value: 1}


@pytest.mark.asyncio
async def test_auto_approval_shares_the_kind_and_flags_the_machine(isolated_stores):
    """An approval is an approval; the machine-ness rides as an attribute."""
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct)
    await instinct.auto_approve(action.id, verdict="allow", reasoning="low risk")

    ledger = stores.get_agent_ledger_store(workspace_id="ws1")
    rows = await ledger.query(kinds=[KIND_ACTION_APPROVED])
    assert len(rows) == 1
    assert rows[0].attrs[ATTR_APPROVAL_AUTO] is True
    assert rows[0].actor == LedgerActor.SYSTEM.value


@pytest.mark.asyncio
async def test_a_losing_concurrent_approve_emits_nothing(isolated_stores):
    """The TOCTOU loser is a no-op upstream, so it must be a no-op here too."""
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct)
    await instinct.reject(action.id, reason="no")
    # Already REJECTED, so approve() rolls back and returns None.
    assert await instinct.approve(action.id) is None

    ledger = stores.get_agent_ledger_store(workspace_id="ws1")
    assert await ledger.counts_by_kind(agent_id="agent-1") == {KIND_ACTION_REJECTED: 1}


@pytest.mark.asyncio
async def test_unattributed_approval_still_lands_a_row(isolated_stores):
    """Attribution gaps degrade to an honest bucket, never to a dropped row."""
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct, actor_agent_id="")
    await instinct.approve(action.id)

    ledger = stores.get_agent_ledger_store(workspace_id="ws1")
    rows = await ledger.query(agent_id="")
    assert len(rows) == 1
    assert rows[0].agent_id == ""
    assert ATTR_AGENT_ID not in rows[0].attrs


# --------------------------------------------------------------------------- #
# THE fail-soft contract
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_raising_ledger_store_does_not_break_the_approval(isolated_stores, monkeypatch):
    """The load-bearing guarantee: bookkeeping never costs anyone a decision.

    If this test goes red, an operator's approve click can now fail because an
    analytics table was locked, full, or misconfigured — which is precisely the
    trade this design refuses to make.
    """
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct)

    class _ExplodingStore:
        async def append(self, row):  # noqa: ARG002
            raise RuntimeError("ledger disk is on fire")

    monkeypatch.setattr(
        stores, "get_agent_ledger_store", lambda **_kw: _ExplodingStore(), raising=True
    )

    approved = await instinct.approve(action.id, approver="user:maya")
    assert approved is not None
    assert approved.status == ActionStatus.APPROVED
    # The audit trail — which IS allowed to be loud — is untouched.
    assert any(e.event == "action_approved" for e in await instinct.query_audit(limit=50))


@pytest.mark.asyncio
async def test_an_unresolvable_ledger_store_does_not_break_the_approval(
    isolated_stores, monkeypatch
):
    """Fail-CLOSED store resolution meets fail-SOFT emission, and soft wins.

    A NULL-workspace action on a cloud box makes the factory raise
    WorkspaceScopeRequired inside the emitter. That is correct behaviour for the
    factory and must still be invisible to the approver.
    """
    instinct = stores.get_instinct_store(workspace_id="ws1")
    action = await _propose(instinct, workspace_id=None)

    def _boom(**_kw):
        raise stores.WorkspaceScopeRequired("no workspace resolved")

    monkeypatch.setattr(stores, "get_agent_ledger_store", _boom, raising=True)

    approved = await instinct.approve(action.id)
    assert approved is not None
    assert approved.status == ActionStatus.APPROVED


# --------------------------------------------------------------------------- #
# activity_by_day — the trend line's data
# --------------------------------------------------------------------------- #


async def test_the_series_fills_silent_days_with_zeros(isolated_stores):
    """A day with no rows is a POINT AT ZERO, never a missing point.

    This is the whole correctness argument for the trend line. A series built
    only from days that have rows draws a straight line across a silent week and
    turns the x-axis into "days when something happened" — which is not time. It
    reads as steady activity when the truth is a gap.
    """
    store = stores.get_agent_ledger_store(workspace_id="ws1")
    today = datetime.now(UTC)
    for offset in (0, 4):  # two days of activity inside a 7-day window
        await store.append(
            LedgerRow(
                agent_id="agent-1", workspace_id="ws1", surface="chat",
                kind=KIND_RUN_COMPLETED, ref=f"r{offset}", actor="system",
                ts=(today - timedelta(days=offset)).isoformat(),
            )
        )

    series = await store.activity_by_day(agent_id="agent-1", workspace_id="ws1", days=7)

    assert len(series) == 7, "every day in the window gets a point, not just the busy ones"
    assert sum(p["count"] for p in series) == 2
    assert [p["count"] for p in series].count(0) == 5


async def test_the_series_runs_oldest_first(isolated_stores):
    """Chart order. Reversed, the line reads as the exact opposite trend."""
    store = stores.get_agent_ledger_store(workspace_id="ws1")
    series = await store.activity_by_day(workspace_id="ws1", days=5)
    days = [p["day"] for p in series]
    assert days == sorted(days)
    assert days[-1] == datetime.now(UTC).date().isoformat(), "last point is today"


async def test_the_series_is_workspace_scoped(isolated_stores):
    """Another tenant's rows never enter this line."""
    for ws in ("ws1", "ws2"):
        store = stores.get_agent_ledger_store(workspace_id=ws)
        await store.append(
            LedgerRow(
                agent_id="agent-1", workspace_id=ws, surface="chat",
                kind=KIND_RUN_COMPLETED, ref=f"run-{ws}", actor="system",
                ts=datetime.now(UTC).isoformat(),
            )
        )
    series = await stores.get_agent_ledger_store(workspace_id="ws1").activity_by_day(
        workspace_id="ws1", days=3
    )
    assert sum(p["count"] for p in series) == 1


@pytest.mark.parametrize("requested,expected", [(0, 1), (-5, 1), (10_000, 366)])
async def test_the_series_length_is_bounded(isolated_stores, requested, expected):
    """``days`` is clamped, so no caller can ask for a line with 10k points.

    An unbounded series is unreadable long before it is expensive, and "show me
    two years" is a rollup question, which v1 deliberately does not answer yet.
    """
    store = stores.get_agent_ledger_store(workspace_id="ws1")
    series = await store.activity_by_day(workspace_id="ws1", days=requested)
    assert len(series) == expected
