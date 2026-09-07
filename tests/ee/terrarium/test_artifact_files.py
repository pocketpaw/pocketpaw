# tests/ee/terrarium/test_artifact_files.py — a book a citizen writes is a real
# file a human can open, and a storage failure never costs the world a tick.

from __future__ import annotations

import pytest
from pocketpaw_ee.terrarium import service as svc

from .conftest import WS, create_universe

pytestmark = pytest.mark.asyncio


class _Rec:
    def __init__(self, i: str) -> None:
        self.id = i


async def test_a_written_book_lands_in_files_and_the_artifact_points_at_it(client, monkeypatch):
    calls: list[dict] = []

    async def fake_write_text_file(**kw):
        calls.append(kw)
        return _Rec("file-abc123")

    import pocketpaw_ee.cloud.uploads.service as uploads

    monkeypatch.setattr(uploads, "write_text_file", fake_write_text_file)

    uni = create_universe(client, founders=1)
    assert client.post(f"/terrarium/universes/{uni['id']}/tick?n=1").status_code == 200

    arts = client.get(f"/terrarium/universes/{uni['id']}/artifacts").json()["artifacts"]
    books = [a for a in arts if a["kind"] == "book"]
    assert books, "tick 1 writes each citizen its charter as a book"
    assert books[0]["file_id"] == "file-abc123"
    assert books[0]["mime"] == "text/markdown"

    assert calls, "the book was never written to /files"
    assert calls[0]["workspace_id"] == WS, "the file must land in the universe's own workspace"
    assert calls[0]["filename"].endswith(".md")
    assert calls[0]["folder_path"].startswith("/terrarium/")
    assert books[0]["author"] in calls[0]["content"]


async def test_a_files_failure_degrades_to_inline_and_the_tick_still_lands(client, monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("storage is down")

    import pocketpaw_ee.cloud.uploads.service as uploads

    monkeypatch.setattr(uploads, "write_text_file", boom)

    uni = create_universe(client, founders=1)
    res = client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    assert res.status_code == 200, res.text

    arts = client.get(f"/terrarium/universes/{uni['id']}/artifacts").json()["artifacts"]
    assert arts, "the artifact still exists"
    assert all(a["file_id"] is None for a in arts)


async def test_structures_never_go_to_files(client, monkeypatch):
    calls: list[dict] = []

    async def spy(**kw):
        calls.append(kw)
        return _Rec("f")

    import pocketpaw_ee.cloud.uploads.service as uploads

    monkeypatch.setattr(uploads, "write_text_file", spy)
    uni = create_universe(client, founders=1)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=3")
    arts = client.get(f"/terrarium/universes/{uni['id']}/artifacts").json()["artifacts"]
    structures = [a for a in arts if a["kind"] == "structure"]
    assert structures, "the mock builds by tick 3"
    assert all(a["file_id"] is None for a in structures)
    assert all(svc._FILE_BEARING_KINDS.isdisjoint({"structure"}) for _ in [0])
