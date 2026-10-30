# tests/cloud/fabric_ingest/test_fabric_ingest_sweep.py
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
#
# Pins the sweep fan-out: run_ingest_sweep enumerates every configured
# (workspace, collection) pair across all enabled FabricIngestConfig rows and
# ingests each under a concurrency cap. The load-bearing property is ISOLATION
# — one collection raising must NOT abort the others. The scheduler's tick()
# delegates to the sweep and never raises.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.fabric_ingest import service as ingest_service  # noqa: E402
from pocketpaw_ee.cloud.models.fabric_ingest_state import (  # noqa: E402
    FabricFieldMapping,
    FabricIngestConfig,
)

pytestmark = pytest.mark.asyncio


async def _seed(workspace_id: str, collections: list[str]) -> None:
    mappings = [
        FabricFieldMapping(collection=c, object_type_id=f"ot-{c}", cursor_field="updated_at")
        for c in collections
    ]
    cfg = FabricIngestConfig(workspace=workspace_id, enabled=True, mappings=mappings)
    await cfg.insert()


async def test_sweep_enumerates_all_configured_sources(mongo_db):  # noqa: ARG001
    await _seed("w1", ["customers", "orders"])
    await _seed("w2", ["leads"])

    seen: list[tuple[str, str]] = []

    async def fake_ingest(workspace_id, source_id):
        seen.append((workspace_id, source_id))
        return {"status": "ok"}

    summary = await ingest_service.run_ingest_sweep(ingest_fn=fake_ingest)

    assert summary["sources"] == 3
    assert summary["ok"] == 3
    assert summary["errors"] == 0
    assert set(seen) == {("w1", "customers"), ("w1", "orders"), ("w2", "leads")}


async def test_sweep_isolates_a_failing_collection(mongo_db):  # noqa: ARG001
    await _seed("w1", ["good_a", "boom", "good_b"])

    async def fake_ingest(workspace_id, source_id):
        if source_id == "boom":
            raise RuntimeError("firestore exploded for this collection")
        return {"status": "ok"}

    summary = await ingest_service.run_ingest_sweep(ingest_fn=fake_ingest)

    # The one bad collection is counted as an error; the other two still ran.
    assert summary["sources"] == 3
    assert summary["ok"] == 2
    assert summary["errors"] == 1


async def test_sweep_counts_self_reported_error_status(mongo_db):  # noqa: ARG001
    await _seed("w1", ["ok_one", "err_one"])

    async def fake_ingest(workspace_id, source_id):
        if source_id == "err_one":
            return {"status": "error", "errors": ["no mapping"]}
        return {"status": "ok"}

    summary = await ingest_service.run_ingest_sweep(ingest_fn=fake_ingest)
    assert summary["ok"] == 1
    assert summary["errors"] == 1


async def test_sweep_empty_when_no_configs(mongo_db):  # noqa: ARG001
    summary = await ingest_service.run_ingest_sweep()
    assert summary == {"sources": 0, "ok": 0, "errors": 0}


async def test_sweep_respects_workspace_filter(mongo_db):  # noqa: ARG001
    await _seed("w1", ["customers"])
    await _seed("w2", ["leads"])

    seen: list[tuple[str, str]] = []

    async def fake_ingest(workspace_id, source_id):
        seen.append((workspace_id, source_id))
        return {"status": "ok"}

    summary = await ingest_service.run_ingest_sweep(workspace_id="w1", ingest_fn=fake_ingest)
    assert summary["sources"] == 1
    assert seen == [("w1", "customers")]


async def test_disabled_config_is_skipped(mongo_db):  # noqa: ARG001
    # An enabled config and a disabled one; only the enabled one's sources sweep.
    await _seed("w1", ["customers"])
    disabled = FabricIngestConfig(
        workspace="w2",
        enabled=False,
        mappings=[FabricFieldMapping(collection="leads", object_type_id="ot-leads")],
    )
    await disabled.insert()

    sources = await ingest_service.list_ingest_sources()
    assert {(s["workspace_id"], s["source_id"]) for s in sources} == {("w1", "customers")}


# --------------------------------------------------------------------------
# Scheduler tick — delegates to the sweep, never raises.
# --------------------------------------------------------------------------


async def test_scheduler_tick_runs_sweep(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.fabric_ingest import scheduler as sched_mod

    called = {}

    async def fake_sweep():
        called["ran"] = True
        return {"sources": 2, "ok": 2, "errors": 0}

    monkeypatch.setattr(ingest_service, "run_ingest_sweep", fake_sweep)
    scheduler = sched_mod.FabricIngestScheduler(interval_seconds=999)
    summary = await scheduler.tick()
    assert called.get("ran") is True
    assert summary == {"sources": 2, "ok": 2, "errors": 0}


async def test_scheduler_tick_survives_sweep_failure(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.fabric_ingest import scheduler as sched_mod

    async def boom_sweep():
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(ingest_service, "run_ingest_sweep", boom_sweep)
    scheduler = sched_mod.FabricIngestScheduler(interval_seconds=999)
    # tick() must swallow the failure and report an empty summary.
    summary = await scheduler.tick()
    assert summary == {"sources": 0, "ok": 0, "errors": 0}
