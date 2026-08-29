# tests/cloud/files/test_unified_carries_library_metadata.py — the flat
# ``GET /files`` shape must carry every library-metadata field.
#
# Created 2026-08-29. FL-1 (tags), FC-1 (summary/collections) and BA-1
# (agent_id) each added their field to ``files/dto.py::FileEntry`` — the v2
# /files/browse tree — and to the uploads provider, but NOT to
# ``files/service.py::UnifiedFile``, which is what the flat ``GET /files``
# listing the Files panel actually calls is built from. The result was a
# summary written correctly, stored correctly in Mongo, and dropped one layer
# before the client: the panel rendered an empty space and read as "the
# feature never ran". Three features, one silent hole.
#
# This test pins the CARRIER, not any single field: it asserts the flat row
# exposes every metadata attribute and that a value set on the source record
# survives the hop. A fourth feature that adds a field to FileEntry alone will
# fail here instead of shipping invisible.
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pocketpaw.uploads.file_store import FileRecord
from pocketpaw_ee.cloud.files.service import UnifiedFile, UnifiedFilesService

# Every field the library writes onto an upload row and the panel renders.
LIBRARY_METADATA = ("tags", "collections", "summary", "agent_id")


def _record(**over) -> FileRecord:
    base = dict(
        id="f1",
        storage_key="k1",
        filename="book.pdf",
        mime="application/pdf",
        size=10,
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC),
    )
    base.update(over)
    return FileRecord(**base)


class _Store:
    def __init__(self, records: list[FileRecord]) -> None:
        self._records = records

    async def list_by_workspace(self, workspace_id, *, limit, pocket_id=None):
        return self._records


def test_the_flat_row_declares_every_library_metadata_field():
    """A field FileEntry has and UnifiedFile lacks is a field the panel
    can never show, however correctly it was written."""
    for name in LIBRARY_METADATA:
        assert name in UnifiedFile.__dataclass_fields__, (
            f"UnifiedFile is missing {name!r}. The flat GET /files listing is "
            f"built from this dataclass, so the Files panel cannot render it."
        )


def test_a_record_that_carries_metadata_still_carries_it_after_the_hop():
    """The bug was not a missing write — it was a lossy hop. Set the values
    on the source record and require them on the far side."""
    rec = _record(
        tags=["anomaly", "hope"],
        collections=["media", "other"],
        summary="A short story about a strange object on a beach.",
        agent_id="agent-42",
    )
    svc = UnifiedFilesService(uploads=_Store([rec]))

    import asyncio

    rows = asyncio.run(svc.list_chat_uploads("w1", limit=10))

    assert len(rows) == 1
    row = rows[0]
    assert row.summary == rec.summary
    assert row.collections == ["media", "other"]
    assert row.tags == ["anomaly", "hope"]
    assert row.agent_id == "agent-42"


def test_a_row_with_no_metadata_is_still_valid():
    """Legacy rows predate all four fields — they must list, not explode."""
    svc = UnifiedFilesService(uploads=_Store([_record()]))

    import asyncio

    row = asyncio.run(svc.list_chat_uploads("w1", limit=10))[0]

    assert row.summary is None
    assert row.agent_id is None
    assert row.collections == []
    assert row.tags == []


@pytest.mark.parametrize("name", LIBRARY_METADATA)
def test_the_source_record_declares_it_too(name: str):
    """FileRecord is the other half of the same hop."""
    assert name in FileRecord.__dataclass_fields__
