# tests/test_fabric_mode_ladder.py
# Created: 2026-07-11 (FST-8 — the operational proof kit).
#
# THE FULL ROLLOUT LADDER, end to end: ONE store walked through
# off → shadow → enforce → off, asserting the mode contract at every rung:
#
#   rung 1 (off)     — writes are pure LWW, ZERO statements, ZERO log lines;
#   rung 2 (shadow)  — multi-source writes produce statements + divergence
#                      lines while the cache still takes the LWW value;
#   rung 3 (enforce) — the resolver owns the cache (a lower-trust write no
#                      longer lands), and the CHANGE curation verb works;
#   rung 4 (off)     — reads serve the last-resolved cache, LWW resumes with
#                      the statement machinery untouched, history intact,
#                      nothing errors.
#
# Plus the harness integration: the REAL divergence lines captured at rung 2
# are fed through parse_divergence_lines + summarize — the FST-8 report
# consumes actual store emissions, so this doubles as the contract
# integration test (if the line format drifts, THIS breaks, not production
# rollouts).

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pocketpaw.fabric.divergence_report import (
    format_report,
    parse_divergence_lines,
    summarize,
)
from pocketpaw.fabric.store import FabricStore

STORE_LOGGER = "pocketpaw.fabric.store"


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


def _shadow_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The divergence lines (excludes the failure-shield warning)."""
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


async def test_full_mode_ladder_off_shadow_enforce_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)

    # A connector-owned object: the crm connector is the property baseline.
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(
        obj_type.id,
        {"name": "Acme", "industry": "fintech", "arr": 120},
        source_connector="crm",
        source_id="c-1",
    )

    # ---------------- rung 1: mode=off — pure LWW, zero statements --------
    _set_mode(monkeypatch, "off")
    updated = await store.update_object(obj.id, {"arr": 150}, writer_class="agent")
    assert updated is not None
    assert updated.properties == {"name": "Acme", "industry": "fintech", "arr": 150}
    assert _table_count(db_path, "fabric_statements") == 0
    assert _table_count(db_path, "fabric_sources") == 0
    assert _shadow_lines(caplog) == []

    # ---------------- rung 2: flip shadow — statements + lines, cache LWW -
    _set_mode(monkeypatch, "shadow")
    caplog.clear()

    # A second distinct source (agent session vs the crm baseline) touching
    # two connector-held properties → both get PROMOTED (seed + incoming
    # statement each) and each produces one divergence line.
    updated = await store.update_object(
        obj.id,
        {"arr": 200, "industry": "crypto"},
        writer_class="agent",
        source_session_id="sess-1",
    )
    assert updated is not None

    # The cache still takes the LWW value in shadow — resolver is advisory.
    assert updated.properties["arr"] == 200
    assert updated.properties["industry"] == "crypto"

    # Statements exist: one promotion seed + one incoming, per property.
    assert _table_count(db_path, "fabric_statements") == 4
    arr_stmts = await store.get_statements(obj.id, "arr")
    assert {s.writer_class for s in arr_stmts} == {"connector", "agent"}
    assert {s.value for s in arr_stmts} == {150, 200}

    # One divergence line per statement-producing property, and the resolver
    # disagreed with LWW (connector seed outranks the agent write) — a
    # DISPUTED, therefore EXPLAINED, divergence on both.
    rung2_lines = _shadow_lines(caplog)
    assert len(rung2_lines) == 2
    assert all(" diverged=True disputed=True unresolvable=False" in ln for ln in rung2_lines)

    # ---------------- rung 3: flip enforce — the resolver owns the cache --
    _set_mode(monkeypatch, "enforce")
    caplog.clear()

    # A lower-trust write (inferred < connector seed) NO LONGER lands in the
    # cache: LWW would have written 300; enforce keeps the resolved winner.
    updated = await store.update_object(
        obj.id, {"arr": 300}, writer_class="inferred", source_session_id="sess-2"
    )
    assert updated is not None
    assert updated.properties["arr"] == 150  # the connector seed's value, not 300
    reread = await store.get_object(obj.id)
    assert reread is not None and reread.properties["arr"] == 150

    # The losing claim is recorded, not dropped, and the line shows the
    # override (lww=what LWW would have kept, resolver=what the cache holds).
    assert any(s.value == 300 for s in await store.get_statements(obj.id, "arr"))
    [enforce_line] = _shadow_lines(caplog)
    assert "property=arr lww=300 resolver=150 diverged=True" in enforce_line

    # The CHANGE curation verb works in enforce: a human preferred statement
    # takes the top trust tier and the cache follows the new winner.
    resolution = await store.change_property(
        obj.id, "arr", 500, writer_class="human", source_actor_id="captain"
    )
    assert resolution.value == 500
    assert resolution.winner_statement is not None
    assert resolution.winner_statement.rank == "preferred"
    reread = await store.get_object(obj.id)
    assert reread is not None and reread.properties["arr"] == 500

    # ---------------- rung 4: flip back off — LWW resumes, history intact -
    _set_mode(monkeypatch, "off")
    caplog.clear()
    statements_before = _table_count(db_path, "fabric_statements")

    # Reads serve the last-RESOLVED cache.
    obj_after = await store.get_object(obj.id)
    assert obj_after is not None
    assert obj_after.properties["arr"] == 500  # the enforced CHANGE result
    assert obj_after.properties["industry"] == "crypto"  # shadow-era LWW cache

    # Subsequent writes are pure LWW and never touch the statement verbs.
    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("statements path touched in mode=off")

    monkeypatch.setattr(store, "get_statements", _boom)
    monkeypatch.setattr(store, "append_statement", _boom)
    monkeypatch.setattr(store, "upsert_source", _boom)

    updated = await store.update_object(obj.id, {"arr": 700, "name": "Acme Corp"})
    assert updated is not None
    assert updated.properties["arr"] == 700  # LWW resumed
    assert updated.properties["name"] == "Acme Corp"
    assert _table_count(db_path, "fabric_statements") == statements_before  # history intact
    assert _shadow_lines(caplog) == []

    # ---------------- the harness consumes the REAL rung-2 emissions ------
    records = parse_divergence_lines(rung2_lines)
    assert len(records) == 2
    assert all(r.ok for r in records), [r.parse_error for r in records]
    by_prop = {r.property: r for r in records}
    assert set(by_prop) == {"arr", "industry"}
    assert by_prop["arr"].lww == 200 and by_prop["arr"].resolver == 150
    assert by_prop["industry"].lww == "crypto" and by_prop["industry"].resolver == "fintech"
    assert all(r.object_id == obj.id for r in records)
    assert all(r.diverged and r.disputed and not r.unresolvable for r in records)
    assert all(r.freshness == "fresh" for r in records)

    # Both divergences are EXPLAINED (flagged disputes — multi-source
    # ordering doing its job), so the shadow run reads as enforce-ready.
    summary = summarize(records)
    assert summary.total == 2
    assert summary.diverged == 2
    assert summary.disputed == 2
    assert summary.unexplained == 0
    assert summary.parse_errors == 0
    assert summary.enforce_ready is True
    report = format_report(summary)
    assert report.splitlines()[-1] == "ENFORCE-READY: yes (0 unexplained)"
