# tests/cloud/test_outcome_value_metering.py — gap-3 outcome VALUE metering.
# Created: 2026-06-11 — coverage for the "pay for governed outcomes"
# pricing primitive: a governed outcome carrying a real billable
# value/unit, persisted on the ledger row (replacing the hardcoded
# Layer-4 `None`), and the `meter_outcomes` aggregation surface that sums
# value BY unit per workspace over a period.
#
# What this pins:
#   - emit_pocket_outcome threads outcome_value/outcome_unit onto the event.
#   - record_outcome persists a WHOLE value/unit pair on the ledger row,
#     and drops a torn half-pair (value-no-unit / unit-no-value) to a
#     count-only row so the aggregation can never total a lone value.
#   - meter_outcomes sums value by unit, counts metered vs total outcomes,
#     honours the pocket_id / since / until window (until is EXCLUSIVE),
#     and is WORKSPACE-ISOLATED.
#   - the ActionBinding validator rejects a half-declared value/unit pair
#     and a value with no `outcome` name, so a row the meter can't total
#     never gets authored.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.realtime.events import PocketOutcomeEvent  # noqa: E402
from pocketpaw_ee.cloud.outcomes import service as outcomes_service  # noqa: E402
from pocketpaw_ee.cloud.outcomes.dto import MeterOutcomesRequest  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path):
    """Point the outcomes ledger at a tmp dir so tests never touch ~/.pocketpaw."""
    outcomes_service.set_ledger_dir(tmp_path / "outcomes")
    yield
    outcomes_service.set_ledger_dir("~/.pocketpaw/outcomes")


def _valued_event(
    *,
    outcome: str = "renewal_completed",
    workspace_id: str = "w1",
    pocket_id: str = "p1",
    action: str = "mark_renewed",
    outcome_value: float | None = 1200.0,
    outcome_unit: str | None = "usd",
    occurred_at: str = "2026-06-01T10:00:00+00:00",
) -> PocketOutcomeEvent:
    return PocketOutcomeEvent(
        data={
            "outcome": outcome,
            "pocket_id": pocket_id,
            "workspace_id": workspace_id,
            "action": action,
            "actor": "u1",
            "via_instinct": True,
            "instinct_action_id": "a1",
            "occurred_at": occurred_at,
            "outcome_value": outcome_value,
            "outcome_unit": outcome_unit,
        }
    )


# ---------------------------------------------------------------------------
# emit_pocket_outcome — value/unit ride the event
# ---------------------------------------------------------------------------


async def test_emit_threads_value_and_unit(monkeypatch):
    emitted = []

    async def _emit(event):
        emitted.append(event)

    monkeypatch.setattr(outcomes_service, "emit", _emit)
    await outcomes_service.emit_pocket_outcome(
        outcome="ticket_resolved",
        pocket_id="p1",
        workspace_id="w1",
        action="close_ticket",
        actor="u1",
        via_instinct=True,
        outcome_value=42.5,
        outcome_unit="usd",
    )
    assert len(emitted) == 1
    assert emitted[0].data["outcome_value"] == 42.5
    assert emitted[0].data["outcome_unit"] == "usd"


async def test_emit_defaults_value_unit_to_none(monkeypatch):
    """A count-only outcome (no value/unit passed) emits null Layer-4 slots."""
    emitted = []

    async def _emit(event):
        emitted.append(event)

    monkeypatch.setattr(outcomes_service, "emit", _emit)
    await outcomes_service.emit_pocket_outcome(
        outcome="email_sent",
        pocket_id="p1",
        workspace_id="w1",
        action="send",
        actor="u1",
        via_instinct=False,
    )
    assert emitted[0].data["outcome_value"] is None
    assert emitted[0].data["outcome_unit"] is None


# ---------------------------------------------------------------------------
# record_outcome — persists a whole pair, drops a torn half-pair
# ---------------------------------------------------------------------------


async def test_record_persists_value_and_unit():
    await outcomes_service.record_outcome(_valued_event(outcome_value=1200.0, outcome_unit="usd"))
    meter = await outcomes_service.meter_outcomes("w1")
    assert meter.metered_count == 1
    assert meter.by_unit["usd"].total_value == 1200.0
    assert meter.by_unit["usd"].count == 1


async def test_record_drops_value_with_no_unit():
    """A value with no unit cannot be summed — persisted as count-only."""
    await outcomes_service.record_outcome(_valued_event(outcome_value=999.0, outcome_unit=None))
    meter = await outcomes_service.meter_outcomes("w1")
    # The outcome counts, but contributes no billable figure.
    assert meter.total_outcomes == 1
    assert meter.metered_count == 0
    assert meter.by_unit == {}


async def test_record_drops_unit_with_no_value():
    """A unit with no value is likewise count-only."""
    await outcomes_service.record_outcome(_valued_event(outcome_value=None, outcome_unit="usd"))
    meter = await outcomes_service.meter_outcomes("w1")
    assert meter.total_outcomes == 1
    assert meter.metered_count == 0
    assert meter.by_unit == {}


async def test_record_rejects_bool_value():
    """A bool value (int subclass) must not silently become 1.0."""
    await outcomes_service.record_outcome(
        _valued_event(outcome_value=True, outcome_unit="usd")  # type: ignore[arg-type]
    )
    meter = await outcomes_service.meter_outcomes("w1")
    assert meter.metered_count == 0


# ---------------------------------------------------------------------------
# meter_outcomes — the aggregation surface
# ---------------------------------------------------------------------------


async def test_meter_sums_value_by_unit():
    await outcomes_service.record_outcome(
        _valued_event(outcome="renewal", outcome_value=1000.0, outcome_unit="usd")
    )
    await outcomes_service.record_outcome(
        _valued_event(outcome="renewal", outcome_value=500.0, outcome_unit="usd")
    )
    await outcomes_service.record_outcome(
        _valued_event(outcome="tickets", outcome_value=3.0, outcome_unit="ticket_resolved")
    )
    meter = await outcomes_service.meter_outcomes("w1")
    assert meter.total_outcomes == 3
    assert meter.metered_count == 3
    assert meter.by_unit["usd"].count == 2
    assert meter.by_unit["usd"].total_value == 1500.0
    assert meter.by_unit["ticket_resolved"].count == 1
    assert meter.by_unit["ticket_resolved"].total_value == 3.0


async def test_meter_excludes_count_only_outcomes_from_rollup():
    """Count-only outcomes raise total_outcomes but not metered_count/by_unit."""
    await outcomes_service.record_outcome(_valued_event(outcome_value=200.0, outcome_unit="usd"))
    await outcomes_service.record_outcome(
        _valued_event(outcome="count_only", outcome_value=None, outcome_unit=None)
    )
    meter = await outcomes_service.meter_outcomes("w1")
    assert meter.total_outcomes == 2
    assert meter.metered_count == 1
    assert meter.by_unit["usd"].total_value == 200.0


async def test_meter_is_workspace_isolated():
    """One workspace's value never leaks into another's billable figure."""
    await outcomes_service.record_outcome(
        _valued_event(workspace_id="w1", outcome_value=1000.0, outcome_unit="usd")
    )
    await outcomes_service.record_outcome(
        _valued_event(workspace_id="w2", outcome_value=9999.0, outcome_unit="usd")
    )
    w1 = await outcomes_service.meter_outcomes("w1")
    w2 = await outcomes_service.meter_outcomes("w2")
    assert w1.by_unit["usd"].total_value == 1000.0
    assert w2.by_unit["usd"].total_value == 9999.0


async def test_meter_period_window_until_is_exclusive():
    """`until` is an EXCLUSIVE upper bound; `since` an inclusive lower bound."""
    await outcomes_service.record_outcome(
        _valued_event(
            outcome_value=10.0, outcome_unit="usd", occurred_at="2026-05-31T23:59:00+00:00"
        )
    )
    await outcomes_service.record_outcome(
        _valued_event(
            outcome_value=20.0, outcome_unit="usd", occurred_at="2026-06-01T00:00:00+00:00"
        )
    )
    await outcomes_service.record_outcome(
        _valued_event(
            outcome_value=40.0, outcome_unit="usd", occurred_at="2026-07-01T00:00:00+00:00"
        )
    )
    # June only: since inclusive 06-01, until exclusive 07-01.
    june = await outcomes_service.meter_outcomes(
        "w1",
        MeterOutcomesRequest(since="2026-06-01T00:00:00+00:00", until="2026-07-01T00:00:00+00:00"),
    )
    assert june.metered_count == 1
    assert june.by_unit["usd"].total_value == 20.0


async def test_meter_pocket_filter():
    await outcomes_service.record_outcome(
        _valued_event(pocket_id="p1", outcome_value=100.0, outcome_unit="usd")
    )
    await outcomes_service.record_outcome(
        _valued_event(pocket_id="p2", outcome_value=300.0, outcome_unit="usd")
    )
    p1 = await outcomes_service.meter_outcomes("w1", MeterOutcomesRequest(pocket_id="p1"))
    assert p1.by_unit["usd"].total_value == 100.0
    assert p1.metered_count == 1


async def test_meter_empty_ledger_is_zero():
    meter = await outcomes_service.meter_outcomes("never-seen")
    assert meter.total_outcomes == 0
    assert meter.metered_count == 0
    assert meter.by_unit == {}


# ---------------------------------------------------------------------------
# ActionBinding validator — a half-declared pair never gets authored
# ---------------------------------------------------------------------------


def _make_binding(**kw):
    from pocketpaw_ee.cloud.pockets.action_executor import ActionBinding

    base = {"method": "POST", "path": "/x"}
    base.update(kw)
    return ActionBinding(**base)


def test_binding_accepts_whole_value_unit_pair():
    b = _make_binding(outcome="renewal", outcome_value=1200.0, outcome_unit="usd")
    assert b.outcome_value == 1200.0
    assert b.outcome_unit == "usd"


def test_binding_accepts_count_only():
    """outcome with no value/unit is the count-only binding — still valid."""
    b = _make_binding(outcome="email_sent")
    assert b.outcome_value is None
    assert b.outcome_unit is None


def test_binding_rejects_value_without_unit():
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="declared together"):
        _make_binding(outcome="renewal", outcome_value=1200.0)


def test_binding_rejects_unit_without_value():
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="declared together"):
        _make_binding(outcome="renewal", outcome_unit="usd")


def test_binding_rejects_value_without_outcome_name():
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="named outcome"):
        _make_binding(outcome_value=1200.0, outcome_unit="usd")


def test_binding_rejects_negative_value():
    """A billable figure cannot be negative (gap-housekeeping)."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="must not be negative"):
        _make_binding(outcome="refund", outcome_value=-50.0, outcome_unit="usd")


def test_binding_accepts_zero_value():
    """Zero is a valid (free but named+metered) outcome — only negative is rejected."""
    b = _make_binding(outcome="free_trial", outcome_value=0.0, outcome_unit="usd")
    assert b.outcome_value == 0.0


# ---------------------------------------------------------------------------
# meter_outcomes — window comparison is on instants, not raw ISO strings
# ---------------------------------------------------------------------------


async def test_meter_window_normalizes_timezone_representations():
    """A row stamped `...Z` and a bound stamped `...+00:00` are the same instant
    and must compare correctly — a lexicographic string compare gets this wrong
    (gap-housekeeping). The `Z` row sits AT the inclusive `since` boundary, so it
    must be counted; byte-wise, "...Z" > "...+00:00" would exclude it."""
    # Same instant, different textual forms.
    await outcomes_service.record_outcome(
        _valued_event(outcome_value=100.0, outcome_unit="usd", occurred_at="2026-06-01T00:00:00Z")
    )
    # since == that instant in +00:00 form (inclusive lower bound).
    meter = await outcomes_service.meter_outcomes(
        "w1",
        MeterOutcomesRequest(since="2026-06-01T00:00:00+00:00"),
    )
    assert meter.metered_count == 1
    assert meter.by_unit["usd"].total_value == 100.0


async def test_meter_until_exclusive_across_timezone_forms():
    """`until` stays EXCLUSIVE even when the row uses `Z` and the bound uses an
    offset for the SAME instant: the boundary row is excluded, the earlier one in."""
    await outcomes_service.record_outcome(
        _valued_event(
            outcome="early",
            outcome_value=10.0,
            outcome_unit="usd",
            occurred_at="2026-06-30T23:00:00Z",
        )
    )
    # This row is AT the exclusive `until` instant (Z form) — must be excluded.
    await outcomes_service.record_outcome(
        _valued_event(
            outcome="boundary",
            outcome_value=20.0,
            outcome_unit="usd",
            occurred_at="2026-07-01T00:00:00Z",
        )
    )
    meter = await outcomes_service.meter_outcomes(
        "w1",
        MeterOutcomesRequest(until="2026-07-01T00:00:00+00:00"),
    )
    assert meter.metered_count == 1
    assert meter.by_unit["usd"].total_value == 10.0


async def test_meter_window_handles_offset_timestamps():
    """An offset timestamp (`-04:00`) is compared on its UTC instant, not its
    local digits. 20:00-04:00 == 00:00Z next day, so it falls in July, not June."""
    await outcomes_service.record_outcome(
        _valued_event(
            outcome_value=77.0,
            outcome_unit="usd",
            occurred_at="2026-06-30T20:00:00-04:00",  # == 2026-07-01T00:00:00Z
        )
    )
    june = await outcomes_service.meter_outcomes(
        "w1",
        MeterOutcomesRequest(since="2026-06-01T00:00:00Z", until="2026-07-01T00:00:00Z"),
    )
    # Excluded from June: its UTC instant is the July boundary (exclusive until).
    assert june.metered_count == 0
