# tests/cloud/uploads/test_abuse_budgets.py — the always-on daily ceilings.
#
# Created 2026-09-11 (feat/abuse-budgets). Covers both counters and the
# owned-workspace cap that makes them mean anything.
#
# What these tests are actually guarding, in order of how badly it would bite:
#   * the ceilings are NOT gated on ``billing_enforced`` — that flag is off by
#     default, and a ceiling that only works on a billed deployment is not a
#     ceiling on the deployment that has not turned billing on yet;
#   * an over-cap claim is rolled BACK, so a refused batch does not permanently
#     consume the slot a later one could have used;
#   * ``0`` means UNCAPPED here, the opposite of comprehension_budget, so an
#     env typo cannot take upload or chat off the air;
#   * a database error fails OPEN, with a warning — see the two tests for why.
#
# Mutations: tests/mutations/abuse_budgets.json.
#
# Updated 2026-10-01 (CN-3): the turn and upload counters are the shared
# ``metering.service`` daily primitive now. ``_turn`` claims exactly the way
# ``run_core._reject_if_over_daily_turns`` does, the pre-check tests call
# ``agent_router._assert_under_daily_turns`` (``turn_budget.is_over_cap`` is
# gone), and rows are read through ``metering.used``. Every assertion is the
# one that held against the old modules.

from __future__ import annotations

import uuid

import pytest
from mongomock_motor import AsyncMongoMockClient
from pocketpaw_ee.cloud._core.errors import DailyTurnLimitError
from pocketpaw_ee.cloud.chat.agent_router import _assert_under_daily_turns
from pocketpaw_ee.cloud.metering import service as metering
from pocketpaw_ee.cloud.metering.domain import DailyMeter
from pocketpaw_ee.cloud.models.daily_usage import DailyUsage
from pocketpaw_ee.cloud.uploads import upload_budget


async def _turn(ws: str | None) -> bool:
    """One run-start claim, exactly as ``run_core`` makes it."""
    return await metering.try_spend(
        subject_type="workspace",
        subject_id=ws,
        meter=DailyMeter.WORKSPACE_TURNS,
        cap=metering.workspace_turns_cap(),
        fail_open=True,
    )


async def _used(ws: str, meter: DailyMeter) -> int:
    return await metering.used(subject_type="workspace", subject_id=ws, meter=meter)


async def _is_over_cap(ws: str) -> bool:
    try:
        await _assert_under_daily_turns(ws)
    except DailyTurnLimitError:
        return True
    return False


@pytest.fixture
async def budget_db():
    from beanie import init_beanie

    client = AsyncMongoMockClient()
    db = client[f"test_budgets_{uuid.uuid4().hex[:8]}"]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[DailyUsage])
    try:
        yield db
    finally:
        for model in (DailyUsage,):
            for attr in ("_document_settings", "_settings"):
                if hasattr(model, attr):
                    try:
                        delattr(model, attr)
                    except Exception:
                        pass


# ── turn budget ──────────────────────────────────────────────────────────


async def test_turns_are_refused_once_the_cap_is_reached(budget_db, monkeypatch):
    """Mutation that must break this: drop the ``spent > cap`` comparison."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "3")
    ws = "w-turns"

    results = [await _turn(ws) for _ in range(4)]

    assert results == [True, True, True, False]
    assert await _used(ws, DailyMeter.WORKSPACE_TURNS) == 3


async def test_a_refused_turn_does_not_hold_the_slot(budget_db, monkeypatch):
    """The rollback. Without it the counter climbs on every refusal, so a
    workspace that hits the cap once can never spend again even after the cap
    is raised — and the stored number stops meaning "turns used today".

    Mutation that must break this: delete the rollback ``update_one``.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "1")
    ws = "w-rollback"

    assert await _turn(ws) is True
    for _ in range(5):
        assert await _turn(ws) is False

    assert await _used(ws, DailyMeter.WORKSPACE_TURNS) == 1, (
        "a refused turn left the counter inflated"
    )


async def test_zero_means_uncapped_not_blocked(budget_db, monkeypatch):
    """The legacy divergence from file comprehension, where 0 blocks everything.

    Chat is the product: an env typo that reads as 0 must not take it off the
    air for every tenant at once.

    Mutation that must break this: return ``False`` when the cap is 0.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "0")

    for _ in range(50):
        assert await _turn("w-uncapped") is True


async def test_a_run_with_no_workspace_is_refused(budget_db, monkeypatch):
    """No tenant means no counter to charge, which is the hole this closes."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "10")

    assert await _turn(None) is False
    assert await _turn("") is False


async def test_an_unreadable_counter_fails_open(budget_db, monkeypatch):
    """The database raises. Fails OPEN, with a warning.

    This test asserted the opposite until 2026-09-12, before the change ever
    shipped: the fail-closed draft refused every run in
    ``test_run_core_plan_surface``, a hermetic harness with no Beanie binding,
    with a cap message and no hint why. An unreadable counter is not an
    attack, and the run's own message persistence fails on the same database
    a statement later, so refusing here protected nothing. The guest gate
    beside this one already answers "not a guest" on any error.

    Raising explicitly rather than leaving Beanie unbound, so the test passes
    for the reason it names.

    Mutation that must break this: answer False from the except.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "10")

    def _boom():
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(DailyUsage, "get_pymongo_collection", _boom)

    assert await _turn("w-no-db") is True


async def test_an_unreadable_upload_counter_fails_open(budget_db, monkeypatch):
    """The upload sibling of the gate above, same reversal."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", "10")

    def _boom():
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(DailyUsage, "get_pymongo_collection", _boom)

    allowed, over = await upload_budget.try_spend("w-no-db", 1, 1)

    assert allowed is True
    assert over == ""


def test_a_bad_env_value_uses_the_default_not_zero(monkeypatch):
    """``"five hundred"`` read as 0 would silently remove the ceiling."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "five hundred")

    assert metering.workspace_turns_cap() == 500


# ── upload budget ────────────────────────────────────────────────────────


async def test_the_file_count_ceiling_trips(budget_db, monkeypatch):
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", "5")
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", "0")
    ws = "w-files"

    assert (await upload_budget.try_spend(ws, 5, 10))[0] is True
    allowed, over = await upload_budget.try_spend(ws, 1, 10)

    assert allowed is False
    assert over == "files"


async def test_the_byte_ceiling_trips_independently(budget_db, monkeypatch):
    """A count cap alone is beaten by fifty big files; a byte cap alone is
    beaten by a hundred thousand tiny ones. Both have to bite on their own.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", "0")
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", "1000")
    ws = "w-bytes"

    assert (await upload_budget.try_spend(ws, 1, 900))[0] is True
    allowed, over = await upload_budget.try_spend(ws, 1, 200)

    assert allowed is False
    assert over == "bytes"


async def test_an_over_cap_batch_rolls_back_both_counters(budget_db, monkeypatch):
    """A refused batch must leave the row exactly as it found it — otherwise
    one oversized upload permanently eats the day's allowance.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", "100")
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", "1000")
    ws = "w-both"

    await upload_budget.try_spend(ws, 2, 500)
    assert (await upload_budget.try_spend(ws, 3, 900))[0] is False

    assert await _used(ws, DailyMeter.UPLOAD_FILES) == 2
    assert await _used(ws, DailyMeter.UPLOAD_BYTES) == 500


async def test_both_zero_means_uncapped(budget_db, monkeypatch):
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", "0")
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", "0")

    allowed, _ = await upload_budget.try_spend("w-free", 10_000, 10**12)

    assert allowed is True


async def test_an_upload_with_no_workspace_is_refused(budget_db, monkeypatch):
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", "10")

    allowed, over = await upload_budget.try_spend(None, 1, 1)

    assert allowed is False
    assert over == "workspace"


# ── the check-only half ──────────────────────────────────────────────────


async def test_the_pre_check_does_not_increment(budget_db, monkeypatch):
    """The HTTP route's fast-reject must not charge a turn. If it did, a turn
    would cost two, and the cap would bite at half the number it advertises.

    Mutation that must break this: claim with ``try_spend`` in the pre-check.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "5")
    ws = "w-precheck"

    await _turn(ws)
    for _ in range(10):
        assert await _is_over_cap(ws) is False

    assert await _used(ws, DailyMeter.WORKSPACE_TURNS) == 1, (
        "the read-only pre-check charged a turn"
    )


async def test_the_pre_check_reports_a_capped_workspace(budget_db, monkeypatch):
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "2")
    ws = "w-precheck-full"

    await _turn(ws)
    assert await _is_over_cap(ws) is False
    await _turn(ws)

    assert await _is_over_cap(ws) is True


async def test_the_pre_check_fails_open(budget_db, monkeypatch):
    """Like the claim itself, and deliberate: this seam only saves a
    capped account a run doc and a stream. The executor's gate is the one that
    has to be right, so a database blip here costs latency, not correctness.

    Mutation that must break this: return ``True`` from the except.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "2")

    def _boom():
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(DailyUsage, "get_pymongo_collection", _boom)

    assert await _is_over_cap("w-blip") is False
