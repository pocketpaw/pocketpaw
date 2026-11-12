# tests/ee/sites/test_source_write_race.py: two writers editing DIFFERENT files of
# one site's ``pocket.source`` at the same time must both land.
#
# Each writer reads the pocket, changes one key and persists. A whole-document
# ``doc.save()`` puts back the map the writer read, so the second save silently drops
# the first writer's file. The writers now persist through a compare-and-set on the
# source they read and re-apply their change on a fresh read when it raced.
#
# The interleave is deterministic: the first two pocket reads wait for each other, so
# both writers hold the same stale map before either writes.
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service

from tests.ee.sites.test_project_engine import SOURCE, _make_project_pocket

pytestmark = pytest.mark.asyncio

HTML = {"index.html": "<h1>Home</h1>\n", "about.html": "<h1>About</h1>\n"}


def _barrier_on_first_reads(monkeypatch, n: int = 2) -> None:
    """The first ``n`` pocket reads return only once all ``n`` happened."""
    real = pockets_service._fetch_pocket
    all_read = asyncio.Event()
    count = 0

    async def _fetch(pocket_id: str) -> Any:
        nonlocal count
        doc = await real(pocket_id)
        count += 1
        if count <= n:
            if count == n:
                all_read.set()
            await asyncio.wait_for(all_read.wait(), timeout=5)
        return doc

    monkeypatch.setattr(pockets_service, "_fetch_pocket", _fetch)


async def _source(pocket_id: str) -> dict:
    return dict((await pockets_service.get(pocket_id, "u1"))["source"])


async def test_two_project_writers_on_different_files_both_land(beanie_test_db, monkeypatch):
    pocket_id = await _make_project_pocket()
    _barrier_on_first_reads(monkeypatch)

    await asyncio.gather(
        pockets_service.set_project_source(pocket_id, "u1", writes={"src/a.ts": "a\n"}),
        pockets_service.set_project_source(pocket_id, "u1", writes={"src/b.ts": "b\n"}),
    )

    after = await _source(pocket_id)
    assert after["src/a.ts"] == "a\n"
    assert after["src/b.ts"] == "b\n"
    assert {k: after[k] for k in SOURCE} == SOURCE


async def test_two_html_writers_on_different_files_both_land(beanie_test_db, monkeypatch):
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Html",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="html",
        source=dict(HTML),
        trusted=True,
    )
    assert err is None, err
    _barrier_on_first_reads(monkeypatch)

    await asyncio.gather(
        pockets_service.set_html_source_file(
            pocket_id, "u1", file_path="index.html", new_source="<h1>Home v2</h1>\n"
        ),
        pockets_service.set_html_source_file(
            pocket_id, "u1", file_path="about.html", new_source="<h1>About v2</h1>\n"
        ),
    )

    after = await _source(pocket_id)
    assert after == {"index.html": "<h1>Home v2</h1>\n", "about.html": "<h1>About v2</h1>\n"}


async def test_a_retry_reruns_the_guard_and_never_overwrites_a_moved_file(
    beanie_test_db, monkeypatch
):
    """A write lands on the SAME file between the guarded writer's read and its
    write. The retry re-runs the base-hash guard on the fresh map, which refuses
    (409), so the racing edit survives instead of being overwritten."""
    from pocketpaw_ee.cloud._core.errors import ConflictError
    from pocketpaw_ee.cloud.models.pocket import Pocket

    pocket_id = await _make_project_pocket()
    racing = "<h1>Racing</h1>\n"
    real = pockets_service._fetch_pocket
    reads = 0

    async def _fetch_then_race(pid: str) -> Any:
        nonlocal reads
        doc = await real(pid)
        reads += 1
        if reads == 1:
            await Pocket.get_pymongo_collection().update_one(
                {"_id": doc.id},
                {"$set": {"source": {**doc.source, "src/pages/index.astro": racing}}},
            )
        return doc

    monkeypatch.setattr(pockets_service, "_fetch_pocket", _fetch_then_race)

    def _guard(current: dict) -> None:
        if current.get("src/pages/index.astro") != SOURCE["src/pages/index.astro"]:
            raise ConflictError("sites.project_file_conflict", "moved")

    with pytest.raises(ConflictError) as err:
        await pockets_service.set_project_source(
            pocket_id, "u1", writes={"src/pages/index.astro": "<h1>Mine</h1>\n"}, guard=_guard
        )
    assert err.value.code == "sites.project_file_conflict"
    assert reads == 2
    assert (await _source(pocket_id))["src/pages/index.astro"] == racing
