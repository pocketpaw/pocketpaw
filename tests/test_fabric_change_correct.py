# tests/test_fabric_change_correct.py
# Created: 2026-07-10 (FST-5 — the CHANGE / CORRECT curation verbs).
#
# Proves the two statement-layer curation verbs (the seams FST-6's PIN/IGNORE
# executor calls):
#
#   * CHANGE — closes the current winner's validity (valid_to = now, it stays
#     auditable history) and appends the new value as rank="preferred", open
#     validity; returns the NEW Resolution,
#   * CORRECT — marks the current winner rank="deprecated" with
#     rank_reason=<reason> (struck from resolution entirely) and appends the
#     corrected value as rank="normal"; returns the NEW Resolution,
#   * both verbs auto-promote an UNTRACKED property first (seed the current
#     cache value with the FST-3 baseline provenance + touch-time
#     observed_at) so pre-verb history is preserved; a property absent from
#     both statements and cache just appends,
#   * mode-respecting cache: enforce writes the new resolver winner into the
#     flat properties dict; shadow and off leave the cache alone (the verbs
#     write statements in EVERY mode — they are statement-layer operations),
#   * a missing / cross-tenant object raises ValueError (a clean seam
#     failure), and statements/sources are workspace-stamped.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pocketpaw.fabric.store import FabricStore

pytestmark = pytest.mark.asyncio


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


async def _crm_object(tmp_path: Path, **props: Any) -> tuple[FabricStore, str]:
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(
        obj_type.id,
        props or {"name": "Acme", "arr": 120},
        source_connector="crm",
        source_id="c-1",
    )
    return store, obj.id


async def _tracked_arr(store: FabricStore, obj_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Track ``arr`` via the FST-3 promotion (agent write in shadow mode so
    the cache stays LWW during setup): seed(connector, 120) + agent(150)."""
    _set_mode(monkeypatch, "shadow")
    await store.update_object(
        obj_id, {"arr": 150}, writer_class="agent", source_session_id="sess-setup"
    )
    assert len(await store.get_statements(obj_id, "arr")) == 2


# ---------------------------------------------------------------------------
# CHANGE — close the winner, append preferred
# ---------------------------------------------------------------------------


async def test_change_closes_winner_and_appends_preferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    _set_mode(monkeypatch, "shadow")

    resolution = await store.change_property(
        obj_id, "arr", 200, writer_class="human", source_actor_id="user:alice"
    )

    # The NEW Resolution: the human-preferred statement wins.
    assert resolution.value == 200
    assert resolution.winner_statement is not None
    assert resolution.winner_statement.rank == "preferred"
    assert resolution.winner_statement.writer_class == "human"
    assert resolution.winner_statement.valid_to is None  # open validity

    stmts = await store.get_statements(obj_id, "arr")
    assert len(stmts) == 3
    # The prior winner (the connector-tier seed, value 120) is CLOSED — a
    # superseded-history statement, still present for audit.
    old_winner = next(s for s in stmts if s.value == 120)
    assert old_winner.valid_to is not None
    # The other loser (the agent 150) is untouched.
    agent = next(s for s in stmts if s.value == 150)
    assert agent.valid_to is None and agent.rank == "normal"

    # Shadow: the cache is untouched by the verb (still the LWW setup value).
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 150


async def test_change_enforce_updates_cache_to_new_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    _set_mode(monkeypatch, "enforce")

    resolution = await store.change_property(
        obj_id, "arr", 200, writer_class="human", source_actor_id="user:alice"
    )

    assert resolution.value == 200
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 200  # resolver-owned cache


# ---------------------------------------------------------------------------
# CORRECT — deprecate the winner (with reason), append normal
# ---------------------------------------------------------------------------


async def test_correct_deprecates_winner_and_appends_normal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    _set_mode(monkeypatch, "shadow")

    resolution = await store.correct_property(
        obj_id,
        "arr",
        130,
        reason="connector reported the pre-discount figure",
        writer_class="human",
        source_actor_id="user:alice",
    )

    # The corrected value wins (human tier; the old connector winner is
    # struck entirely).
    assert resolution.value == 130
    assert resolution.winner_statement is not None
    assert resolution.winner_statement.rank == "normal"
    assert resolution.winner_statement.writer_class == "human"

    stmts = await store.get_statements(obj_id, "arr")
    assert len(stmts) == 3
    deprecated = next(s for s in stmts if s.value == 120)
    assert deprecated.rank == "deprecated"
    assert deprecated.rank_reason == "connector reported the pre-discount figure"
    # A deprecated statement never wins, loses, or disputes — it is not in
    # the Resolution at all.
    assert deprecated.id not in {s.id for s in resolution.losers}

    # Shadow: cache untouched.
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 150


async def test_correct_enforce_updates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    _set_mode(monkeypatch, "enforce")

    resolution = await store.correct_property(
        obj_id,
        "arr",
        130,
        reason="wrong unit",
        writer_class="human",
        source_actor_id="user:alice",
    )

    assert resolution.value == 130
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 130


# ---------------------------------------------------------------------------
# Untracked properties: promote-then-apply (history preserved)
# ---------------------------------------------------------------------------


async def test_change_on_untracked_property_promotes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path, name="Acme")
    _set_mode(monkeypatch, "shadow")

    resolution = await store.change_property(
        obj_id, "name", "Acme Corp", writer_class="human", source_actor_id="user:alice"
    )

    stmts = await store.get_statements(obj_id, "name")
    assert len(stmts) == 2  # the promotion seed + the change
    # The seed preserved the pre-change claim with the object's baseline
    # provenance (connector-owned object → writer "connector") and was
    # closed by the change (it WAS the current winner).
    seed = next(s for s in stmts if s.value == "Acme")
    assert seed.writer_class == "connector"
    assert seed.valid_to is not None
    assert resolution.value == "Acme Corp"


async def test_correct_on_untracked_property_promotes_then_deprecates_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path, name="Acmee")
    _set_mode(monkeypatch, "shadow")

    resolution = await store.correct_property(
        obj_id,
        "name",
        "Acme",
        reason="typo in the source",
        writer_class="human",
        source_actor_id="user:alice",
    )

    stmts = await store.get_statements(obj_id, "name")
    assert len(stmts) == 2
    seed = next(s for s in stmts if s.value == "Acmee")
    assert seed.rank == "deprecated" and seed.rank_reason == "typo in the source"
    assert resolution.value == "Acme"
    assert resolution.is_disputed is False  # the wrong claim is struck, not disputing


async def test_verb_on_absent_property_appends_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A property with no statements AND no cache value has no prior claim —
    nothing to seed, nothing to close; the verb just appends. In enforce the
    cache gains the property (the new winner)."""
    store, obj_id = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")

    resolution = await store.change_property(
        obj_id, "tier", "gold", writer_class="human", source_actor_id="user:alice"
    )

    stmts = await store.get_statements(obj_id, "tier")
    assert len(stmts) == 1 and stmts[0].rank == "preferred"
    assert resolution.value == "gold"
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["tier"] == "gold"


# ---------------------------------------------------------------------------
# The verbs are statement-layer operations in EVERY mode; only enforce
# touches the cache
# ---------------------------------------------------------------------------


async def test_change_in_off_mode_writes_statements_but_not_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "off")

    resolution = await store.change_property(
        obj_id, "arr", 200, writer_class="human", source_actor_id="user:alice"
    )

    assert resolution.value == 200
    assert len(await store.get_statements(obj_id, "arr")) == 2  # seed + change
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 120  # cache untouched (off)


# ---------------------------------------------------------------------------
# Failure + tenancy seams
# ---------------------------------------------------------------------------


async def test_verb_missing_object_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")

    with pytest.raises(ValueError, match="not found"):
        await store.change_property("obj-nope", "arr", 1, writer_class="human")
    with pytest.raises(ValueError, match="not found"):
        await store.correct_property("obj-nope", "arr", 1, reason="x", writer_class="human")


async def test_verbs_respect_workspace_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)
    obj_type = await store.define_type(name="Customer", properties=[], workspace_id="ws-a")
    obj = await store.create_object(
        obj_type.id,
        {"arr": 120},
        source_connector="crm",
        source_id="c-1",
        workspace_id="ws-a",
    )
    _set_mode(monkeypatch, "enforce")

    # Cross-tenant: the object is invisible to ws-b → clean ValueError.
    with pytest.raises(ValueError, match="not found"):
        await store.change_property(obj.id, "arr", 200, writer_class="human", workspace_id="ws-b")

    # Own tenant: works, and statements are workspace-stamped.
    resolution = await store.change_property(
        obj.id, "arr", 200, writer_class="human", source_actor_id="u:a", workspace_id="ws-a"
    )
    assert resolution.value == 200
    stmts = await store.get_statements(obj.id, "arr", workspace_id="ws-a")
    assert len(stmts) == 2 and all(s.workspace_id == "ws-a" for s in stmts)
    refreshed = await store.get_object(obj.id, workspace_id="ws-a")
    assert refreshed is not None and refreshed.properties["arr"] == 200
