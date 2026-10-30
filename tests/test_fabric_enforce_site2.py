# tests/test_fabric_enforce_site2.py
# Created: 2026-07-10 (FST-5 — the ENFORCE decision for merge site 2).
#
# Site 2 (the journal projection) stays EVENT-FAITHFUL in enforce — that is
# the deliberate FST-5 ruling documented at the top of fabric/projection.py:
# the projection folds exactly what the journal says (plain LWW), and enforce
# ownership of values applies at the STORE CACHE layer (the flat properties
# dict — the primary read path per the FST constraints). This file is the
# proof that the ruling is honest, not an accident:
#
#   * the store cache holds the RESOLVED value while the projection's
#     in-memory fold (event-faithfully) differs — and the divergence line was
#     emitted, so the gap is recorded telemetry, not a silent surprise,
#   * a projection rebuild in enforce reproduces the same event-faithful fold
#     (deterministic from the journal alone) with no statement double-append,
#     and the store cache still holds the resolved value.

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor

from pocketpaw.fabric.journal_store import FabricJournalStore
from pocketpaw.fabric.models import FabricObject
from pocketpaw.fabric.resolver import resolve
from pocketpaw.fabric.store import FabricStore
from pocketpaw.fabric.trust import default_trust_rules

STORE_LOGGER = "pocketpaw.fabric.store"


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


def _shadow_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == STORE_LOGGER and r.getMessage().startswith("fabric shadow: object=")
    ]


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    yield j
    j.close()


async def _mirrored_setup(tmp_path: Path, journal) -> tuple[FabricStore, FabricJournalStore, str]:
    """One connector-owned object living in BOTH read models: a row in the
    SQLite store (the flat-properties cache) and a created event in the
    journal (the projection's source), sharing one object id."""
    store = FabricStore(tmp_path / "fabric.db")
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(
        obj_type.id,
        {"industry": "fintech"},
        source_connector="crm",
        source_id="c-1",
    )
    js = FabricJournalStore(journal, statement_store=store)
    js.bootstrap()
    await js.create(
        FabricObject(
            id=obj.id,
            type_id=obj_type.id,
            type_name="Customer",
            properties={"industry": "fintech"},
            source_connector="crm",
            source_id="c-1",
        ),
        scope=["org"],
    )
    return store, js, obj.id


async def test_store_cache_holds_resolved_value_while_projection_fold_differs(
    tmp_path: Path,
    journal,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, js, obj_id = await _mirrored_setup(tmp_path, journal)
    _set_mode(monkeypatch, "enforce")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    # An agent writes a different value THROUGH THE JOURNAL (site 2).
    await js.update(
        obj_id,
        {"industry": "crypto"},
        scope=["org"],
        actor=Actor(kind="agent", id="did:soul:discovery", scope_context=[]),
    )

    # The projection is event-faithful: its fold shows the journal's LWW.
    projected = await js.get(obj_id)
    assert projected is not None and projected.properties["industry"] == "crypto"

    # The statements were recorded through the shared machinery, and the
    # resolver's winner is the connector fact.
    stmts = await store.get_statements(obj_id, "industry")
    assert {s.value for s in stmts} == {"fintech", "crypto"}
    resolution = resolve(stmts, default_trust_rules(), object_type="Customer")
    assert resolution.value == "fintech" and resolution.is_disputed is True

    # THE RULING'S PROOF: the store cache (the primary read path) holds the
    # resolved value even though the projection's in-memory fold differs.
    cached = await store.get_object(obj_id)
    assert cached is not None and cached.properties["industry"] == "fintech"
    assert cached.properties["industry"] != projected.properties["industry"]

    # ...and a site-1 write on the same object keeps the cache resolver-owned:
    # a second inferred claim still cannot displace the connector fact.
    updated = await store.update_object(
        obj_id,
        {"industry": "web3"},
        writer_class="inferred",
        source_session_id="discovery-run-2",
    )
    assert updated is not None and updated.properties["industry"] == "fintech"

    # The projection/resolver gap is RECORDED telemetry, not a silent hole:
    # the journal update's own divergence line was emitted at flush time.
    assert any(
        f"object={obj_id} property=industry" in line and 'lww="crypto"' in line
        for line in _shadow_lines(caplog)
    )


async def test_projection_rebuild_stays_event_faithful_in_enforce(
    tmp_path: Path, journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, js, obj_id = await _mirrored_setup(tmp_path, journal)
    _set_mode(monkeypatch, "enforce")

    await js.update(
        obj_id,
        {"industry": "crypto"},
        scope=["org"],
        actor=Actor(kind="agent", id="did:soul:discovery", scope_context=[]),
    )
    assert len(await store.get_statements(obj_id, "industry")) == 2

    # Rebuild from the journal: the fold is deterministic from the journal
    # ALONE (the event-faithful guarantee), the event-id dedupe prevents any
    # statement double-append, and the store cache stays resolver-owned.
    js.bootstrap()
    assert await js.flush_shadow() == 0  # already recorded — deduped
    projected = await js.get(obj_id)
    assert projected is not None and projected.properties["industry"] == "crypto"
    assert len(await store.get_statements(obj_id, "industry")) == 2
    cached = await store.get_object(obj_id)
    assert cached is not None and cached.properties["industry"] == "fintech"
