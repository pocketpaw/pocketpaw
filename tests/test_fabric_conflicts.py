# tests/test_fabric_conflicts.py
# Created: 2026-07-10 (FST-6 — the open-conflict surface).
#
# Proves ``detect_open_conflicts`` (fabric/conflicts.py) — the recompute-from-
# statements scan the EE stewardship sweep consumes:
#
#   * finds EXACTLY the un-rankable conflicts (same tier + same rank + both
#     open validity + materially different + within the recency epsilon) —
#     mere rankable disputes (tier, rank, or recency orders them) are
#     excluded by design: policy already answered those,
#   * ``rivals`` names only the statements that TRIGGERED un-rankability
#     (lower-tier losers are policy-ranked, not human choices),
#   * the dedupe key ``(workspace_id, object_id, property)`` and the
#     conflict ``signature`` (sorted competing statement ids) are stable
#     across scans — the EE sweep's one-open-proposal-per-key guarantee
#     hangs off that stability,
#   * a steward verb CLOSES the conflict on the next scan (pin → the pinned
#     path is never un-rankable; ignore → the rival is struck) — no
#     persisted conflict state to invalidate,
#   * scoping: ``object_id`` narrows the scan; W4a workspace scope hides
#     other tenants' statements; a deleted object's orphan statements are
#     skipped.

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pocketpaw.fabric.conflicts import detect_open_conflicts
from pocketpaw.fabric.store import FabricStore

pytestmark = pytest.mark.asyncio


async def _store_with_object(tmp_path: Path, name: str = "fabric.db") -> tuple[FabricStore, str]:
    store = FabricStore(tmp_path / name)
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(
        obj_type.id, {"name": "Acme"}, source_connector="crm", source_id="c-1"
    )
    return store, obj.id


async def _unrankable_arr(
    store: FabricStore,
    obj_id: str,
    *,
    workspace_id: str | None = None,
    gap: timedelta = timedelta(hours=1),
) -> tuple[str, str]:
    """Two same-tier (connector), same-rank, open, materially different
    statements observed within the epsilon → un-rankable. Returns
    (winner_stmt_id, rival_stmt_id) — the newer observation wins."""
    now = datetime.now()
    src_a = await store.upsert_source(
        "connector_run", connector="crm", run_id="r1", workspace_id=workspace_id
    )
    src_b = await store.upsert_source(
        "connector_run", connector="billing", run_id="r9", workspace_id=workspace_id
    )
    rival = await store.append_statement(
        obj_id, "arr", 200, src_b.id, "connector", observed_at=now - gap, workspace_id=workspace_id
    )
    winner = await store.append_statement(
        obj_id, "arr", 120, src_a.id, "connector", observed_at=now, workspace_id=workspace_id
    )
    return winner.id, rival.id


# ---------------------------------------------------------------------------
# Exactly the un-rankable ones — not mere disputes
# ---------------------------------------------------------------------------


async def test_detect_finds_exactly_the_unresolvable_conflicts(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    winner_id, rival_id = await _unrankable_arr(store, obj_id)

    # A RANKABLE dispute on another property: human vs connector — the
    # ladder orders them, policy answers it, no human needed.
    now = datetime.now()
    human_src = await store.upsert_source("human_actor", actor_id="user:alice")
    crm_src = await store.upsert_source("connector_run", connector="crm", run_id="r1")
    await store.append_statement(obj_id, "tier", "gold", human_src.id, "human", observed_at=now)
    await store.append_statement(obj_id, "tier", "silver", crm_src.id, "connector", observed_at=now)

    # An AGREEING pair on a third property: same value, no dispute at all.
    await store.append_statement(obj_id, "region", "EU", human_src.id, "human", observed_at=now)
    await store.append_statement(obj_id, "region", "EU", crm_src.id, "connector", observed_at=now)

    conflicts = await detect_open_conflicts(store)

    assert [c.property for c in conflicts] == ["arr"]
    record = conflicts[0]
    assert record.object_id == obj_id
    assert record.object_type == "Customer"
    assert record.winner.id == winner_id and record.winner.value == 120
    assert [r.id for r in record.rivals] == [rival_id]


async def test_rank_or_recency_difference_is_rankable_not_a_conflict(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    now = datetime.now()
    src_a = await store.upsert_source("connector_run", connector="crm", run_id="r1")
    src_b = await store.upsert_source("connector_run", connector="billing", run_id="r9")

    # Same tier but a RANK difference (preferred vs normal) — a ranking.
    await store.append_statement(
        obj_id, "arr", 120, src_a.id, "connector", observed_at=now, rank="preferred"
    )
    await store.append_statement(obj_id, "arr", 200, src_b.id, "connector", observed_at=now)

    # Same tier + rank but observed OUTSIDE the 24h epsilon — recency ranks.
    await store.append_statement(
        obj_id, "mrr", 10, src_a.id, "connector", observed_at=now - timedelta(hours=30)
    )
    await store.append_statement(obj_id, "mrr", 20, src_b.id, "connector", observed_at=now)

    assert await detect_open_conflicts(store) == []


async def test_rivals_exclude_lower_tier_losers(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    winner_id, rival_id = await _unrankable_arr(store, obj_id)
    # A lower-tier (agent) statement, materially different and recent — a
    # loser the ladder already ranks, NOT a choice for the human.
    agent_src = await store.upsert_source("agent_session", session_id="sess-1")
    await store.append_statement(
        obj_id, "arr", 999, agent_src.id, "agent", observed_at=datetime.now()
    )

    conflicts = await detect_open_conflicts(store)

    assert len(conflicts) == 1
    record = conflicts[0]
    assert record.winner.id == winner_id
    assert [r.id for r in record.rivals] == [rival_id]  # the agent 999 is absent
    assert record.signature == sorted([winner_id, rival_id])


# ---------------------------------------------------------------------------
# Dedupe key + signature stability
# ---------------------------------------------------------------------------


async def test_dedupe_key_and_signature_stable_across_scans(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    await _unrankable_arr(store, obj_id, workspace_id="w1")

    first = await detect_open_conflicts(store, workspace_id="w1")
    second = await detect_open_conflicts(store, workspace_id="w1")

    assert len(first) == len(second) == 1
    assert first[0].dedupe_key == second[0].dedupe_key == ("w1", obj_id, "arr")
    assert first[0].signature == second[0].signature


# ---------------------------------------------------------------------------
# Steward verbs close the conflict — no persisted state to invalidate
# ---------------------------------------------------------------------------


async def test_pin_closes_the_conflict(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    winner_id, _ = await _unrankable_arr(store, obj_id)
    assert len(await detect_open_conflicts(store)) == 1

    await store.pin_statement(obj_id, "arr", winner_id)

    assert await detect_open_conflicts(store) == []


async def test_ignore_closes_the_conflict(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    _, rival_id = await _unrankable_arr(store, obj_id)
    assert len(await detect_open_conflicts(store)) == 1

    await store.ignore_statement(obj_id, "arr", rival_id)

    assert await detect_open_conflicts(store) == []


# ---------------------------------------------------------------------------
# Scoping — object filter, tenancy, deleted objects
# ---------------------------------------------------------------------------


async def test_object_id_filter_narrows_the_scan(tmp_path: Path) -> None:
    store, obj_a = await _store_with_object(tmp_path)
    obj_type = (await store.list_types())[0]
    obj_b = await store.create_object(
        obj_type.id, {"name": "Globex"}, source_connector="crm", source_id="c-2"
    )
    await _unrankable_arr(store, obj_a)
    await _unrankable_arr(store, obj_b.id)

    all_conflicts = await detect_open_conflicts(store)
    assert {c.object_id for c in all_conflicts} == {obj_a, obj_b.id}

    only_a = await detect_open_conflicts(store, object_id=obj_a)
    assert [c.object_id for c in only_a] == [obj_a]


async def test_workspace_scope_hides_other_tenants_conflicts(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    await _unrankable_arr(store, obj_id, workspace_id="w1")

    own = await detect_open_conflicts(store, workspace_id="w1")
    assert len(own) == 1
    assert own[0].workspace_id == "w1"

    # w2's scope sees own + legacy-NULL rows only — w1's statements are
    # invisible, so there is nothing to arbitrate.
    assert await detect_open_conflicts(store, workspace_id="w2") == []


async def test_deleted_object_statements_are_skipped(tmp_path: Path) -> None:
    store, obj_id = await _store_with_object(tmp_path)
    await _unrankable_arr(store, obj_id)
    assert len(await detect_open_conflicts(store)) == 1

    await store.remove_object(obj_id)

    # The orphan statements remain in the table (append-only audit) but the
    # object is gone — not a conflict anyone can arbitrate.
    assert await detect_open_conflicts(store) == []
