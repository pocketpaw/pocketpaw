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
#   * a database error fails CLOSED.
#
# Mutations: tests/mutations/abuse_budgets.json.

from __future__ import annotations

import uuid

import pytest
from mongomock_motor import AsyncMongoMockClient

from pocketpaw_ee.cloud.chat.runs import turn_budget
from pocketpaw_ee.cloud.models.workspace_turn_usage import WorkspaceTurnUsage
from pocketpaw_ee.cloud.models.workspace_upload_usage import WorkspaceUploadUsage
from pocketpaw_ee.cloud.uploads import upload_budget


@pytest.fixture
async def budget_db():
    from beanie import init_beanie

    client = AsyncMongoMockClient()
    db = client[f"test_budgets_{uuid.uuid4().hex[:8]}"]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[WorkspaceUploadUsage, WorkspaceTurnUsage])
    try:
        yield db
    finally:
        for model in (WorkspaceUploadUsage, WorkspaceTurnUsage):
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

    results = [await turn_budget.try_spend(ws) for _ in range(4)]

    assert [r[0] for r in results] == [True, True, True, False]
    assert [r[1] for r in results[:3]] == [1, 2, 3]


async def test_a_refused_turn_does_not_hold_the_slot(budget_db, monkeypatch):
    """The rollback. Without it the counter climbs on every refusal, so a
    workspace that hits the cap once can never spend again even after the cap
    is raised — and the stored number stops meaning "turns used today".

    Mutation that must break this: delete the rollback ``update_one``.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "1")
    ws = "w-rollback"

    assert (await turn_budget.try_spend(ws))[0] is True
    for _ in range(5):
        assert (await turn_budget.try_spend(ws))[0] is False

    doc = await WorkspaceTurnUsage.get_pymongo_collection().find_one({"workspace": ws})
    assert doc["used"] == 1, "a refused turn left the counter inflated"


async def test_zero_means_uncapped_not_blocked(budget_db, monkeypatch):
    """The divergence from ``comprehension_budget``, where 0 blocks everything.

    Chat is the product: an env typo that reads as 0 must not take it off the
    air for every tenant at once.

    Mutation that must break this: return ``False`` when the cap is 0.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "0")

    for _ in range(50):
        assert (await turn_budget.try_spend("w-uncapped"))[0] is True


async def test_a_run_with_no_workspace_is_refused(budget_db, monkeypatch):
    """No tenant means no counter to charge, which is the hole this closes."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "10")

    assert (await turn_budget.try_spend(None))[0] is False
    assert (await turn_budget.try_spend(""))[0] is False


async def test_an_unreadable_counter_fails_closed(budget_db, monkeypatch):
    """The database raises. Fails CLOSED, and that costs nothing extra: a run
    persists its messages to the same database, so one that cannot serve this
    counter cannot serve the run either.

    Raising explicitly rather than leaving Beanie unbound — an unbound document
    happens to raise today, which makes the test pass for a reason that is not
    the one it names.

    Mutation that must break this: return ``(True, 0, cap)`` from the except.
    """
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "10")

    def _boom():
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(WorkspaceTurnUsage, "get_pymongo_collection", _boom)

    allowed, _spent, cap = await turn_budget.try_spend("w-no-db")

    assert allowed is False
    assert cap == 10


async def test_an_unreadable_upload_counter_fails_closed(budget_db, monkeypatch):
    """The upload sibling of the gate above."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", "10")

    def _boom():
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(WorkspaceUploadUsage, "get_pymongo_collection", _boom)

    allowed, over = await upload_budget.try_spend("w-no-db", 1, 1)

    assert allowed is False
    assert over == "unavailable"


def test_a_bad_env_value_uses_the_default_not_zero(monkeypatch):
    """``"five hundred"`` read as 0 would silently remove the ceiling."""
    monkeypatch.setenv("POCKETPAW_WORKSPACE_TURNS_DAILY", "five hundred")

    assert turn_budget.daily_cap() == 500


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

    doc = await WorkspaceUploadUsage.get_pymongo_collection().find_one({"workspace": ws})
    assert (doc["used"], doc["bytes_used"]) == (2, 500)


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
