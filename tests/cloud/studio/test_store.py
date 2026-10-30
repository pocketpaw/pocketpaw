# tests/cloud/studio/test_store.py — contract for the Mongo-backed Studio
# generation store (SM-0).
#
# Created 2026-09-08 (feat/studio-history-store). These tests pin the three
# properties the JSONL history could not hold, and which the sites media layer
# depends on:
#
#   1. TENANCY IS A QUERY, NOT A POST-FILTER. The JSONL kept one file for the
#      whole deployment and filtered a `_workspace` field on read, with an
#      `or _workspace is None` clause that showed untagged rows to EVERY tenant
#      while its docstring claimed the opposite. A record here is reachable only
#      from the workspace that wrote it — there is no untagged branch to fall
#      through.
#   2. THE STATUS LIFECYCLE IS REPRESENTABLE. All eight `_append_history` call
#      sites in service.py write `status="succeeded"`, so a four-value enum only
#      ever held one value: nothing was persisted while a generation was queued
#      or running, and a failure left no trace at all. Async video therefore had
#      nowhere to record that it was rendering, and a page reload lost the job.
#   3. PROVENANCE ROUND-TRIPS. `source` / `pocket_id` distinguish a site
#      agent's generations from a human's, which is what stops the linked
#      gallery reading as clutter.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import schemas

pytestmark = pytest.mark.asyncio


def _generation(gen_id: str, *, status: str = "queued", kind: str = "image") -> schemas.Generation:
    """A minimal but schema-valid Generation record."""
    return schemas.Generation(
        id=gen_id,
        prompt="a calm nordic reception desk, morning light",
        status=status,
        kind=kind,
        model="gpt-image-1",
        params=schemas.GenerationParams(
            kind=kind,
            model="gpt-image-1",
            aspectRatio="16:9",
            count=1,
        ),
        assets=[],
        createdAt=1_757_000_000_000,
    )


# ── 1. Tenancy ──────────────────────────────────────────────────────────────


async def test_a_generation_is_invisible_to_another_workspace(mongo_db):
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("g1"))

    assert [g.id for g in await store.list_generations("w1")] == ["g1"]
    assert await store.list_generations("w2") == []


async def test_get_generation_is_scoped_to_the_owning_workspace(mongo_db):
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("g1"))

    assert (await store.get_generation("w1", "g1")) is not None
    # The id is guessable; the workspace is the boundary.
    assert (await store.get_generation("w2", "g1")) is None


async def test_updating_from_a_foreign_workspace_does_not_touch_the_record(mongo_db):
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("g1", status="running"))

    assert await store.update_generation("w2", "g1", status="succeeded") is False

    still_mine = await store.get_generation("w1", "g1")
    assert still_mine is not None
    assert still_mine.status == "running"


# ── 2. The status lifecycle ─────────────────────────────────────────────────


async def test_a_queued_generation_is_persisted_before_it_completes(mongo_db):
    """The JSONL wrote nothing until success, so an in-flight video job did not
    exist and a reload lost it. A queued record must be listable immediately."""
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("vid1", status="queued", kind="video"))

    listed = await store.list_generations("w1")
    assert [(g.id, g.status) for g in listed] == [("vid1", "queued")]


async def test_a_generation_transitions_queued_to_running_to_succeeded(mongo_db):
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("vid1", status="queued", kind="video"))

    assert await store.update_generation("w1", "vid1", status="running") is True
    assert (await store.get_generation("w1", "vid1")).status == "running"

    asset = schemas.GeneratedAsset(
        id="a1",
        url="https://cdn.example.com/sites-assets/w1/p1/abc123-hero.mp4",
        mime="video/mp4",
    )
    assert await store.update_generation("w1", "vid1", status="succeeded", assets=[asset]) is True

    done = await store.get_generation("w1", "vid1")
    assert done.status == "succeeded"
    assert [a.url for a in done.assets] == [asset.url]


async def test_a_failed_generation_persists_with_its_error(mongo_db):
    """Failures left no trace at all, so failed spend could not be audited."""
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("g1", status="running"))

    assert (
        await store.update_generation("w1", "g1", status="failed", error="upstream quota exceeded")
        is True
    )

    failed = await store.get_generation("w1", "g1")
    assert failed.status == "failed"
    assert failed.error == "upstream quota exceeded"
    # A failure is still part of the workspace's history, not swallowed.
    assert [g.id for g in await store.list_generations("w1")] == ["g1"]


# ── 3. Provenance ───────────────────────────────────────────────────────────


async def test_source_and_pocket_round_trip_and_filter(mongo_db):
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("studio1"))
    await store.record_generation(
        "w1", _generation("site1"), source="sites", pocket_id="pocket-abc"
    )

    from_sites = await store.list_generations("w1", source="sites")
    assert [g.id for g in from_sites] == ["site1"]

    for_pocket = await store.list_generations("w1", pocket_id="pocket-abc")
    assert [g.id for g in for_pocket] == ["site1"]

    assert len(await store.list_generations("w1")) == 2


# ── 4. Listing shape ────────────────────────────────────────────────────────


async def test_list_is_newest_first_and_honours_limit(mongo_db):
    from pocketpaw_ee.cloud.studio import service as store

    for i in range(5):
        gen = _generation(f"g{i}")
        gen.createdAt = 1_757_000_000_000 + i
        await store.record_generation("w1", gen)

    assert [g.id for g in await store.list_generations("w1")] == [
        "g4",
        "g3",
        "g2",
        "g1",
        "g0",
    ]
    assert [g.id for g in await store.list_generations("w1", limit=2)] == ["g4", "g3"]


async def test_recording_the_same_id_twice_does_not_duplicate(mongo_db):
    """Retries and status rewrites must not fan out into duplicate tiles."""
    from pocketpaw_ee.cloud.studio import service as store

    await store.record_generation("w1", _generation("g1", status="queued"))
    await store.record_generation("w1", _generation("g1", status="succeeded"))

    listed = await store.list_generations("w1")
    assert [(g.id, g.status) for g in listed] == [("g1", "succeeded")]


# ── 5. The gallery must not silently lose the older half ────────────────────


async def test_the_gallery_is_not_silently_capped(mongo_db):
    """REGRESSION. The reader briefly defaulted to a 50-row cap. The /studio page
    fetches this once with NO cursor and renders whatever comes back, so a
    workspace with more history than the cap would simply stop seeing the older
    part of its own gallery — data present in Mongo, unreachable in the product,
    and invisible to every test that used fewer rows than the cap.

    MUTATION: give ``list_generations`` a non-None default ``limit``.
    """
    from pocketpaw_ee.cloud.studio import service as store

    for i in range(60):
        gen = _generation(f"g{i:02d}")
        gen.createdAt = 1_757_000_000_000 + i
        await store.record_generation("w1", gen)

    assert len(await store.list_generations("w1")) == 60


async def test_a_caller_that_can_page_may_still_ask_for_one(mongo_db):
    """The cap is opt-in, for callers that can actually page."""
    from pocketpaw_ee.cloud.studio import service as store

    for i in range(10):
        gen = _generation(f"g{i:02d}")
        gen.createdAt = 1_757_000_000_000 + i
        await store.record_generation("w1", gen)

    assert len(await store.list_generations("w1", limit=3)) == 3


# ── 6. The dedupe key is a constraint, not just a lookup ────────────────────


def test_the_dedupe_key_is_declared_unique():
    """``record_generation`` does find-then-insert with no lock, and `backend` and
    `worker` both run it — so the application check alone makes duplicate tiles
    merely unlikely. The UNIQUE index is what makes them impossible.

    Asserted on the DECLARATION, not the behaviour, because mongomock does not
    enforce unique indexes — a behavioural test would pass whether or not the
    constraint shipped, which is worse than no test.

    KNOWN GAP: the ``DuplicateKeyError`` recovery in ``record_generation`` is
    therefore NOT covered here. It cannot be exercised without a real mongod, and
    faking the error only proves the fake raises. Verify it against a real
    instance before trusting the recovery, and do not read this test as coverage
    of it.

    MUTATION: drop unique=True from the index.
    """
    from pocketpaw_ee.cloud.models.studio_generation import StudioGeneration
    from pymongo import IndexModel

    unique_keys = [
        tuple(k for k, _ in idx.document["key"].items())
        for idx in StudioGeneration.Settings.indexes
        if isinstance(idx, IndexModel) and idx.document.get("unique")
    ]
    assert ("workspace", "generation_id") in unique_keys


# ── 7. A history miss must not cost a paid generation ───────────────────────


async def test_a_history_failure_does_not_fail_the_caller(mongo_db, monkeypatch):
    """The JSONL appender swallowed OSError, so a history failure could never lose
    a generation the user had already paid for. A bare Mongo write removed that:
    an error would raise AFTER the image was generated, billed and stored.

    MUTATION: narrow the except in record_generation_best_effort.
    """
    from pocketpaw_ee.cloud.studio import service as store

    async def _boom(*_a, **_k):
        raise RuntimeError("mongo is having a day")

    monkeypatch.setattr(store, "record_generation", _boom)

    await store.record_generation_best_effort("w1", _generation("g1"))  # must not raise


async def test_tracked_filenames_asks_for_a_projection(mongo_db, monkeypatch):
    """It runs on every GET /api/v1/media and reads ONE field. Hydrating whole
    documents reproduces, against Mongo, the failure that made the JSONL
    untenable — every tenant's entire history decoded on every request.

    Asserted on the QUERY, not on behaviour, because behaviour is identical
    either way: this is a cost property, and a mutation that drops the projection
    escapes every behavioural test. That is exactly why this test looks odd.
    """
    from pocketpaw_ee.cloud.studio import service as store

    seen: dict = {}
    real = store.StudioGeneration.get_pymongo_collection()

    class _Spy:
        def find(self, *args):
            seen["args"] = args
            return real.find(*args)

    monkeypatch.setattr(
        store.StudioGeneration, "get_pymongo_collection", classmethod(lambda cls: _Spy())
    )

    await store.tracked_generation_filenames()

    assert len(seen["args"]) == 2, "no projection was passed — whole documents hydrate"
    assert seen["args"][1].get("assets.url") == 1
