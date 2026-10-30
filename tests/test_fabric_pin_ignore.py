# tests/test_fabric_pin_ignore.py
# Created: 2026-07-10 (FST-6 — the PIN / UNPIN / IGNORE steward verbs).
#
# Proves the three statement-layer steward verbs (siblings of FST-5's
# CHANGE/CORRECT — the operations the Instinct stewardship executor calls):
#
#   * PIN — sets pinned=True on one existing, non-deprecated statement; the
#     resolver's pinned short-circuit then makes it win outright, above every
#     ladder tier and immune to newer/higher-tier rivals (pinned-beats-
#     everything), while the losers stay intact for audit,
#   * UNPIN — retracts the flag; resolution falls back to the trust ladder,
#   * IGNORE — deprecates the statement with rank_reason=<reason> (struck
#     from resolution entirely, default reason "steward_ignored"),
#   * all three return the NEW Resolution and are mode-respecting on the
#     cache (enforce writes the new resolver winner; shadow/off leave the
#     cache alone),
#   * error paths: missing object, missing/cross-workspace statement, and
#     PIN on a deprecated statement all raise ValueError (clean seam
#     failures for the FST-6 executor),
#   * deprecating the ONLY live statement leaves a winner-less Resolution
#     and (in enforce) the cache keeps its last value.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pocketpaw.fabric.resolver import resolve
from pocketpaw.fabric.store import FabricStore
from pocketpaw.fabric.trust import default_trust_rules

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
    the cache stays LWW during setup): seed(connector, 120) + agent(150).
    The ladder ranks connector > agent, so the seed (120) is the winner."""
    _set_mode(monkeypatch, "shadow")
    await store.update_object(
        obj_id, {"arr": 150}, writer_class="agent", source_session_id="sess-setup"
    )
    assert len(await store.get_statements(obj_id, "arr")) == 2


async def _agent_statement_id(store: FabricStore, obj_id: str) -> str:
    stmts = await store.get_statements(obj_id, "arr")
    return next(s.id for s in stmts if s.writer_class == "agent")


async def _connector_statement_id(store: FabricStore, obj_id: str) -> str:
    stmts = await store.get_statements(obj_id, "arr")
    return next(s.id for s in stmts if s.writer_class == "connector")


# ---------------------------------------------------------------------------
# PIN — the durable "this one wins"
# ---------------------------------------------------------------------------


async def test_pin_makes_lower_tier_statement_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    agent_id = await _agent_statement_id(store, obj_id)
    _set_mode(monkeypatch, "shadow")

    resolution = await store.pin_statement(obj_id, "arr", agent_id)

    # The pinned agent statement short-circuits the ladder — it beats the
    # connector-tier winner outright.
    assert resolution.value == 150
    assert resolution.winner_statement is not None
    assert resolution.winner_statement.id == agent_id
    assert resolution.winner_statement.pinned is True
    assert resolution.unresolvable is False  # a pin is authoritative

    # The loser stays intact for audit (not deprecated, not closed).
    loser = next(s for s in resolution.losers if s.writer_class == "connector")
    assert loser.rank == "normal" and loser.valid_to is None

    # Shadow: the cache is untouched by the verb.
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 150  # LWW setup value


async def test_pin_enforce_updates_cache_to_pinned_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)  # cache holds the LWW 150
    connector_id = await _connector_statement_id(store, obj_id)
    agent_id = await _agent_statement_id(store, obj_id)
    _set_mode(monkeypatch, "enforce")

    # Pin the connector statement: the cache flips 150 → 120 (proves the
    # enforce write — the cache held the setup's LWW value before).
    resolution = await store.pin_statement(obj_id, "arr", connector_id)
    assert resolution.value == 120
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 120  # resolver-owned cache

    # Re-point the pin at the agent statement: the cache follows.
    await store.unpin_statement(obj_id, "arr", connector_id)
    resolution = await store.pin_statement(obj_id, "arr", agent_id)
    assert resolution.value == 150
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 150

    # Un-pinning reverts resolution (and the cache) to the ladder winner.
    reverted = await store.unpin_statement(obj_id, "arr", agent_id)
    assert reverted.value == 120
    assert reverted.winner_statement is not None
    assert reverted.winner_statement.id == connector_id
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 120


async def test_pinned_beats_everything_even_later_human_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned-beats-everything via the resolver: after the pin, a NEW
    human-tier (top of the ladder) statement still loses to the pin."""
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    agent_id = await _agent_statement_id(store, obj_id)
    _set_mode(monkeypatch, "shadow")
    await store.pin_statement(obj_id, "arr", agent_id)

    src = await store.upsert_source("human_actor", actor_id="user:bob")
    await store.append_statement(obj_id, "arr", 999, src.id, "human")

    stmts = await store.get_statements(obj_id, "arr")
    resolution = resolve(stmts, default_trust_rules(), object_type="Customer")
    assert resolution.winner_statement is not None
    assert resolution.winner_statement.id == agent_id
    assert resolution.value == 150
    # The human rival is an open, materially different survivor → disputed,
    # but never unresolvable on the pinned path.
    assert resolution.is_disputed is True
    assert resolution.unresolvable is False


async def test_pin_does_not_auto_unpin_other_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    agent_id = await _agent_statement_id(store, obj_id)
    connector_id = await _connector_statement_id(store, obj_id)
    _set_mode(monkeypatch, "shadow")

    await store.pin_statement(obj_id, "arr", agent_id)
    resolution = await store.pin_statement(obj_id, "arr", connector_id)

    # Both pins survive; two pins is a curation conflict the resolver
    # deliberately flags as disputed (newest recorded pin wins).
    stmts = await store.get_statements(obj_id, "arr")
    assert sum(1 for s in stmts if s.pinned) == 2
    assert resolution.is_disputed is True


# ---------------------------------------------------------------------------
# IGNORE — strike a bogus claim
# ---------------------------------------------------------------------------


async def test_ignore_deprecates_statement_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    connector_id = await _connector_statement_id(store, obj_id)
    _set_mode(monkeypatch, "shadow")

    resolution = await store.ignore_statement(obj_id, "arr", connector_id)

    # The ignored (connector) winner is struck; the agent statement wins.
    assert resolution.value == 150
    stmts = await store.get_statements(obj_id, "arr")
    ignored = next(s for s in stmts if s.id == connector_id)
    assert ignored.rank == "deprecated"
    assert ignored.rank_reason == "steward_ignored"  # the default reason
    # Struck entirely: not the winner, not a loser.
    assert connector_id not in {s.id for s in resolution.losers}

    # Shadow: cache untouched.
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 150


async def test_ignore_enforce_updates_cache_and_custom_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)  # cache holds the LWW 150
    agent_id = await _agent_statement_id(store, obj_id)
    _set_mode(monkeypatch, "enforce")

    # Strike the agent statement: the connector 120 is the only survivor and
    # the cache flips 150 → 120 (proves the enforce write).
    resolution = await store.ignore_statement(obj_id, "arr", agent_id, reason="stale export")

    assert resolution.value == 120
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 120
    stmts = await store.get_statements(obj_id, "arr")
    ignored = next(s for s in stmts if s.id == agent_id)
    assert ignored.rank_reason == "stale export"


async def test_ignore_only_statement_leaves_cache_and_no_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deprecating the ONLY live statement → winner-less Resolution; in
    enforce the cache keeps its last value (reads never lose a key)."""
    store, obj_id = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")
    # Track "name" via an explicit human change (seed + preferred change).
    await store.change_property(
        obj_id, "name", "Acme Corp", writer_class="human", source_actor_id="user:alice"
    )
    stmts = await store.get_statements(obj_id, "name")
    assert len(stmts) == 2
    _set_mode(monkeypatch, "enforce")
    # Strike both statements, one at a time (oldest first: the promotion
    # seed, then the human change).
    first = await store.ignore_statement(obj_id, "name", stmts[0].id, reason="bogus")
    assert first.winner_statement is not None  # one live statement remains
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["name"] == "Acme Corp"  # enforce write
    second = await store.ignore_statement(obj_id, "name", stmts[1].id, reason="bogus")
    assert second.winner_statement is None
    assert second.value is None

    # No winner → no cache write: the last enforced value is kept.
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# Error paths — clean ValueErrors for the FST-6 executor
# ---------------------------------------------------------------------------


async def test_verbs_raise_on_missing_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")
    with pytest.raises(ValueError, match="not found"):
        await store.pin_statement("obj-missing", "arr", "stm-x")
    with pytest.raises(ValueError, match="not found"):
        await store.unpin_statement("obj-missing", "arr", "stm-x")
    with pytest.raises(ValueError, match="not found"):
        await store.ignore_statement("obj-missing", "arr", "stm-x")


async def test_verbs_raise_on_missing_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    _set_mode(monkeypatch, "shadow")
    with pytest.raises(ValueError, match="statement"):
        await store.pin_statement(obj_id, "arr", "stm-nope")
    with pytest.raises(ValueError, match="statement"):
        await store.ignore_statement(obj_id, "arr", "stm-nope")
    # A statement id that exists but under ANOTHER property is equally invalid.
    agent_id = await _agent_statement_id(store, obj_id)
    with pytest.raises(ValueError, match="statement"):
        await store.pin_statement(obj_id, "name", agent_id)


async def test_pin_raises_on_deprecated_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id = await _crm_object(tmp_path)
    await _tracked_arr(store, obj_id, monkeypatch)
    connector_id = await _connector_statement_id(store, obj_id)
    _set_mode(monkeypatch, "shadow")
    await store.ignore_statement(obj_id, "arr", connector_id)
    with pytest.raises(ValueError, match="deprecated"):
        await store.pin_statement(obj_id, "arr", connector_id)


async def test_verbs_respect_workspace_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement stamped for workspace w1 is invisible from w2's scope —
    the verb fails with a clean ValueError, no cross-tenant curation."""
    store, obj_id = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")
    src = await store.upsert_source("connector_run", connector="crm", workspace_id="w1")
    stmt = await store.append_statement(obj_id, "arr", 500, src.id, "connector", workspace_id="w1")
    # Visible in w1's scope: the pin succeeds.
    resolution = await store.pin_statement(obj_id, "arr", stmt.id, workspace_id="w1")
    assert resolution.winner_statement is not None
    await store.unpin_statement(obj_id, "arr", stmt.id, workspace_id="w1")
    # Invisible from w2's scope: ValueError.
    with pytest.raises(ValueError, match="statement"):
        await store.pin_statement(obj_id, "arr", stmt.id, workspace_id="w2")
    with pytest.raises(ValueError, match="statement"):
        await store.ignore_statement(obj_id, "arr", stmt.id, workspace_id="w2")
