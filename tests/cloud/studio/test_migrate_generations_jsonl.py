# tests/cloud/studio/test_migrate_generations_jsonl.py — the legacy-history import.
#
# Created 2026-09-08 (feat/studio-history-store, SM-0). The JSONL lives on the
# ``backend-data`` named volume, so it survives redeploys and holds real
# galleries; shipping the Mongo store without this import would silently empty
# every workspace's /studio page.
#
# The behaviour worth pinning is what happens to UNTAGGED records. The old reader
# admitted rows whose ``_workspace`` was None and showed them to every tenant.
# They cannot be attributed to anyone, so the migration counts them and leaves
# them behind rather than picking an owner — their disappearance is the leak
# closing, not data loss to be quietly patched over.

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.cloud.studio import migrate_generations_jsonl as migration
from pocketpaw_ee.cloud.studio import service

pytestmark = pytest.mark.asyncio


def _record(gen_id: str, workspace: str | None) -> dict:
    """One legacy JSONL row: a Generation dump plus the ``_workspace`` tag."""
    record = {
        "id": gen_id,
        "prompt": "a quiet reception desk",
        "status": "succeeded",
        "kind": "image",
        "model": "gpt-image-1",
        "params": {
            "kind": "image",
            "model": "gpt-image-1",
            "aspectRatio": "1:1",
            "count": 1,
        },
        "assets": [],
        "createdAt": 1_757_000_000_000,
    }
    if workspace is not None:
        record["_workspace"] = workspace
    return record


def _write(path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


async def test_a_missing_file_is_not_an_error(tmp_path, mongo_db):
    """A fresh install has no file. If that failed, the first deploy deadlocks."""
    result = await migration.migrate_file(tmp_path / "nope.jsonl")

    assert result == migration.Result()


async def test_an_empty_file_imports_nothing(tmp_path, mongo_db):
    path = tmp_path / "generations.jsonl"
    _write(path, [])

    assert (await migration.migrate_file(path)).imported == 0


async def test_tagged_records_land_in_their_own_workspace(tmp_path, mongo_db):
    path = tmp_path / "generations.jsonl"
    _write(path, [_record("g1", "ws-1"), _record("g2", "ws-2")])

    result = await migration.migrate_file(path)
    assert result.imported == 2

    assert [g.id for g in await service.list_generations("ws-1")] == ["g1"]
    assert [g.id for g in await service.list_generations("ws-2")] == ["g2"]


async def test_untagged_records_are_counted_and_left_behind(tmp_path, mongo_db):
    """They were readable by every tenant; importing one means choosing an owner
    and any choice is wrong."""
    path = tmp_path / "generations.jsonl"
    _write(path, [_record("tagged", "ws-1"), _record("orphan", None)])

    result = await migration.migrate_file(path)

    assert result.imported == 1
    assert result.skipped_untagged == 1
    assert [g.id for g in await service.list_generations("ws-1")] == ["tagged"]


async def test_a_corrupt_line_is_stepped_over_not_fatal(tmp_path, mongo_db):
    """The old reader skipped corrupt lines; a migration that dies on one would
    strand every record after it."""
    path = tmp_path / "generations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_record("g1", "ws-1"))
        + "\n"
        + "{not json at all\n"
        + json.dumps(_record("g2", "ws-1"))
        + "\n",
        encoding="utf-8",
    )

    result = await migration.migrate_file(path)

    assert result.imported == 2
    assert result.skipped_unreadable == 1


async def test_rerunning_converges_and_does_not_duplicate(tmp_path, mongo_db):
    path = tmp_path / "generations.jsonl"
    _write(path, [_record("g1", "ws-1")])

    first = await migration.migrate_file(path)
    second = await migration.migrate_file(path)

    assert (first.imported, first.already) == (1, 0)
    assert (second.imported, second.already) == (0, 1)
    assert len(await service.list_generations("ws-1")) == 1


async def test_dry_run_reports_without_writing(tmp_path, mongo_db):
    path = tmp_path / "generations.jsonl"
    _write(path, [_record("g1", "ws-1")])

    result = await migration.migrate_file(path, dry_run=True)

    assert result.imported == 1
    assert await service.list_generations("ws-1") == []


async def test_the_uri_comes_from_the_same_var_the_app_reads(tmp_path):
    assert migration.resolve_mongo_uri({"CLOUD_MONGODB_URI": "mongodb://m:27017/x"}) == (
        "mongodb://m:27017/x"
    )
    # No env at all still resolves, so a local run needs no setup.
    assert migration.resolve_mongo_uri({}).startswith("mongodb://")
