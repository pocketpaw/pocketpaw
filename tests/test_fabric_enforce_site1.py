# tests/test_fabric_enforce_site1.py
# Created: 2026-07-10 (FST-5 — ENFORCE mode: the resolver owns the cache).
#
# Proves the enforce cache semantics at merge site 1 (store.update_object):
#
#   * a TRACKED property lands in the flat properties dict as the RESOLVER'S
#     winner, not the blind LWW value; untracked properties keep LWW (they
#     have no statements — nothing to resolve), including inside one mixed
#     update,
#   * the divergence line keeps its exact FST-8 shape; in enforce ``lww=`` is
#     what LWW would have kept and ``resolver=`` is what the cache now holds,
#   * a statement-pass failure degrades THAT write to plain LWW (warning
#     logged) — the cache write never breaks,
#   * when the incoming write IS the resolver's winner, enforce and LWW agree,
#
# plus THE SZD TEST (TestSzdProof — the payoff of the whole source-truth
# chain) and the REVERSAL PROOF (enforce → off leaves a fully working store).

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pocketpaw.fabric.resolver import resolve
from pocketpaw.fabric.store import FabricStore
from pocketpaw.fabric.trust import default_trust_rules

STORE_LOGGER = "pocketpaw.fabric.store"

DIVERGENCE_RE = re.compile(
    r"^fabric shadow: object=\S+ property=\S+ lww=.+ resolver=.+"
    r" diverged=(True|False) disputed=(True|False) unresolvable=(True|False)$"
)


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


def _shadow_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == STORE_LOGGER and r.getMessage().startswith("fabric shadow: object=")
    ]


def _table_count(db_path: Path, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    finally:
        con.close()


async def _crm_object(tmp_path: Path, **props: Any) -> tuple[FabricStore, str, Path]:
    """A store with one connector-owned object; returns (store, object_id, db_path)."""
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(
        obj_type.id,
        props or {"name": "Acme", "arr": 120},
        source_connector="crm",
        source_id="c-1",
    )
    return store, obj.id, db_path


# ---------------------------------------------------------------------------
# Tracked properties: the resolver's winner is what the cache receives
# ---------------------------------------------------------------------------


async def test_enforce_tracked_property_cache_gets_resolver_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")

    # Agent write (second source): promotes arr — seed(connector, 120) +
    # agent(150). The connector-tier seed wins the ladder, so the cache gets
    # 120, NOT the blind LWW 150.
    updated = await store.update_object(
        obj_id, {"arr": 150}, writer_class="agent", source_session_id="sess-1"
    )

    assert updated is not None
    assert updated.properties["arr"] == 120  # resolver's winner, not LWW
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 120
    # Both claims exist regardless — the losing write is preserved as history.
    stmts = await store.get_statements(obj_id, "arr")
    assert {s.value for s in stmts} == {120, 150}


async def test_enforce_untracked_properties_keep_lww(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")

    # Same source as the object's owner: no promotion, no statements — the
    # property stays scalar and enforce has nothing to resolve → plain LWW.
    updated = await store.update_object(
        obj_id, {"arr": 999}, writer_class="connector", source_connector="crm"
    )

    assert updated is not None and updated.properties["arr"] == 999
    assert _table_count(db_path, "fabric_statements") == 0


async def test_enforce_mixed_update_tracked_resolved_untracked_lww(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path, name="Acme", arr=120, note="x")
    _set_mode(monkeypatch, "enforce")

    # One update carrying a promotable property (arr — materially different,
    # second source) and a brand-new key (label — no prior claim, stays
    # untracked): arr gets the resolver's winner, label gets LWW.
    updated = await store.update_object(
        obj_id,
        {"arr": 150, "label": "vip"},
        writer_class="agent",
        source_session_id="sess-1",
    )

    assert updated is not None
    assert updated.properties["arr"] == 120  # resolved (connector seed wins)
    assert updated.properties["label"] == "vip"  # untracked → LWW
    assert await store.get_statements(obj_id, "label") == []


async def test_enforce_incoming_winner_lands_in_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the incoming write IS the resolver's winner (a human write beats
    the connector seed), enforce agrees with LWW — same cache value, but now
    it's the resolver's decision, not blind recency."""
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")

    updated = await store.update_object(
        obj_id, {"arr": 175}, writer_class="human", source_actor_id="user:alice"
    )

    assert updated is not None and updated.properties["arr"] == 175
    stmts = await store.get_statements(obj_id, "arr")
    resolution = resolve(stmts, default_trust_rules(), object_type="Customer")
    assert resolution.value == 175 and resolution.winner_statement is not None
    assert resolution.winner_statement.writer_class == "human"


# ---------------------------------------------------------------------------
# The divergence line in enforce: lww=what LWW would have kept,
# resolver=what the cache now holds
# ---------------------------------------------------------------------------


async def test_enforce_divergence_line_logs_lww_vs_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    await store.update_object(
        obj_id, {"arr": 150}, writer_class="agent", source_session_id="sess-1"
    )

    lines = _shadow_lines(caplog)
    assert lines == [
        f"fabric shadow: object={obj_id} property=arr lww=150 resolver=120"
        " diverged=True disputed=True unresolvable=False"
    ]
    assert DIVERGENCE_RE.fullmatch(lines[0])
    # And "resolver" really is what the cache holds now (enforce semantics).
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 120


# ---------------------------------------------------------------------------
# Failure shield: a broken statement pass degrades to LWW, never blocks
# ---------------------------------------------------------------------------


async def test_enforce_statement_pass_failure_falls_back_to_lww(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, _ = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated statement-pass failure")

    monkeypatch.setattr(store, "append_statement", _explode)
    caplog.set_level(logging.WARNING, logger=STORE_LOGGER)

    updated = await store.update_object(
        obj_id, {"arr": 150}, writer_class="agent", source_session_id="sess-1"
    )

    assert updated is not None and updated.properties["arr"] == 150  # LWW fallback
    assert any(
        "falling back to LWW" in r.getMessage() for r in caplog.records if r.name == STORE_LOGGER
    )


# ---------------------------------------------------------------------------
# THE SZD TEST — the payoff of the whole source-truth chain
# ---------------------------------------------------------------------------


class TestSzdProof:
    """THE SZD TEST: what the whole Fabric source-truth chain exists to prove.

    Before FST, the flat properties dict was blind last-write-wins: a
    discovery-INFERRED value written after a connector sync silently
    overwrote the connector's FACT, and nothing recorded that it happened.
    With ``fabric_source_truth_mode=enforce``:

    * a discovery-inferred value can NO LONGER beat a connector fact in the
      cache — the trust ladder (connector > inferred) decides, not recency;
    * the inferred claim is NOT lost — it lands as a rank="normal" statement,
      the conflict is flagged (``is_disputed=True``), and the divergence line
      records exactly what LWW would have done and what enforce did instead;
    * order doesn't matter — when the inferred value came FIRST (the object's
      creator) and the connector fact arrives SECOND, the connector still
      wins the cache.
    """

    async def test_inferred_value_cannot_beat_connector_fact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A connector-written fact...
        store, obj_id, db_path = await _crm_object(tmp_path, industry="fintech")
        _set_mode(monkeypatch, "enforce")
        caplog.set_level(logging.INFO, logger=STORE_LOGGER)

        # ...hit by a discovery-style inferred write with a different value.
        updated = await store.update_object(
            obj_id,
            {"industry": "crypto"},
            writer_class="inferred",
            source_session_id="discovery-run-1",
        )

        # The cache STILL shows the connector fact.
        assert updated is not None and updated.properties["industry"] == "fintech"
        obj = await store.get_object(obj_id)
        assert obj is not None and obj.properties["industry"] == "fintech"

        # The inferred claim exists as a rank="normal" statement — recorded,
        # not silently discarded.
        stmts = await store.get_statements(obj_id, "industry")
        inferred = next(s for s in stmts if s.writer_class == "inferred")
        assert inferred.value == "crypto" and inferred.rank == "normal"

        # The conflict is flagged.
        resolution = resolve(stmts, default_trust_rules(), object_type="Customer")
        assert resolution.value == "fintech"
        assert resolution.is_disputed is True

        # And the divergence line shows it: LWW would have kept "crypto",
        # enforce wrote "fintech".
        lines = _shadow_lines(caplog)
        assert lines == [
            f'fabric shadow: object={obj_id} property=industry lww="crypto"'
            ' resolver="fintech" diverged=True disputed=True unresolvable=False'
        ]

    async def test_reversed_order_connector_fact_still_wins_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The inferred value comes FIRST: discovery CREATED the object (no
        # source_connector — an unattributed creator, so the promotion seed
        # derives the object-level agent baseline; connector outranks both
        # agent and inferred, so the ordering below is decided by trust
        # either way).
        db_path = tmp_path / "fabric.db"
        store = FabricStore(db_path)
        obj_type = await store.define_type(name="Customer", properties=[])
        obj = await store.create_object(obj_type.id, {"industry": "crypto"})
        _set_mode(monkeypatch, "enforce")

        # The connector fact arrives second.
        updated = await store.update_object(
            obj.id,
            {"industry": "fintech"},
            writer_class="connector",
            source_connector="crm",
            source_run_id="run-1",
        )

        # The connector wins the cache — enforce is order-independent.
        assert updated is not None and updated.properties["industry"] == "fintech"

        # Both claims exist: the seeded pre-connector value + the fact.
        stmts = await store.get_statements(obj.id, "industry")
        assert {s.value for s in stmts} == {"crypto", "fintech"}
        resolution = resolve(stmts, default_trust_rules(), object_type="Customer")
        assert resolution.winner_statement is not None
        assert resolution.winner_statement.writer_class == "connector"


# ---------------------------------------------------------------------------
# REVERSAL PROOF — enforce → off leaves a fully working, plain-LWW store
# ---------------------------------------------------------------------------


async def test_mode_flipped_back_to_off_after_enforce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A store that ran enforce (statements on disk + a resolver-owned cache),
    flipped back to 'off': every read works and serves the last-RESOLVED
    values, subsequent writes are plain LWW with ZERO statement machinery,
    and nothing errors."""
    store, obj_id, db_path = await _crm_object(tmp_path, industry="fintech", arr=120)
    _set_mode(monkeypatch, "enforce")

    # Enforce run: the inferred write loses; the cache holds the resolved
    # connector fact and two statements are on disk.
    await store.update_object(
        obj_id,
        {"industry": "crypto"},
        writer_class="inferred",
        source_session_id="discovery-run-1",
    )
    assert _table_count(db_path, "fabric_statements") == 2

    # --- Flip back to off ---
    _set_mode(monkeypatch, "off")

    # Reads work and serve the last-resolved values.
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["industry"] == "fintech"

    # Subsequent writes: pure LWW, no statement verb is ever touched.
    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("statements path touched in mode=off")

    monkeypatch.setattr(store, "get_statements", _boom)
    monkeypatch.setattr(store, "append_statement", _boom)
    monkeypatch.setattr(store, "upsert_source", _boom)
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    updated = await store.update_object(obj_id, {"industry": "web3", "arr": 500})
    assert updated is not None
    assert updated.properties["industry"] == "web3"  # LWW resumed
    assert updated.properties["arr"] == 500
    assert _table_count(db_path, "fabric_statements") == 2  # untouched history
    assert _shadow_lines(caplog) == []
