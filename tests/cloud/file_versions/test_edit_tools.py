# test_edit_tools.py — FL-5 structural edit tool tests (port of dewani12's #1193).
# Created: 2026-07-03 (FL-5). Locks:
#   - the pure operation engines (apply_operations / apply_slides_operations /
#     apply_spreadsheet_ops) mutate blocks / deck / workbook correctly
#   - EditDocumentTool edits an Editor.js document + writes a NEW revertable
#     FL-2 version (the pre-edit content is recoverable via list/get_version)
#   - EditSlidesTool / EditSpreadsheetTool each edit their structure + write a
#     revertable version
#   - the editor_blocks transport round-trips (set/get_editor_blocks + the
#     get_active_blocks fallback)
#   - cross-workspace access is DENIED (tenant isolation)
# The tool resolves the workspace from ``agent_service.current_workspace_id``;
# these tests patch the module getters directly (mirroring FL-3's
# test_library_verbs harness) so no full chat stream is needed.
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.file_versions import service as fv_service
from pocketpaw_ee.cloud.uploads.models import FileUpload

from pocketpaw.tools.builtin import edit_document as ed
from pocketpaw.tools.builtin import edit_slides as es
from pocketpaw.tools.builtin import edit_spreadsheet as esp
from pocketpaw.tools.builtin.edit_document import EditDocumentTool, apply_operations
from pocketpaw.tools.builtin.edit_slides import EditSlidesTool, apply_slides_operations
from pocketpaw.tools.builtin.edit_spreadsheet import (
    EditSpreadsheetTool,
    apply_spreadsheet_ops,
    parse_cell_ref,
    parse_range_ref,
)

# ---------------------------------------------------------------------------
# Pure operation engine unit tests (no DB, no workspace)
# ---------------------------------------------------------------------------


def test_apply_operations_update_insert_delete_move():
    blocks = [
        {"id": "a", "type": "paragraph", "data": {"text": "one"}},
        {"id": "b", "type": "paragraph", "data": {"text": "two"}},
    ]
    out, _ = apply_operations(
        blocks,
        [
            {"op": "update", "id": "a", "data": {"text": "ONE"}},
            {"op": "insert", "type": "header", "data": {"text": "H", "level": 2}, "index": 0},
            {"op": "delete", "id": "b"},
        ],
    )
    assert out[0]["type"] == "header"
    assert out[1]["data"]["text"] == "ONE"
    assert all(b.get("id") != "b" for b in out)


def test_apply_operations_normalizes_notion_types():
    blocks: list[dict] = []
    out, _ = apply_operations(
        blocks,
        [{"op": "insert", "type": "heading", "data": {"text": "Title"}}],
    )
    # "heading" is not a valid Editor.js type — normalized to "header".
    assert out[0]["type"] == "header"


def test_apply_operations_per_block_mode_blocks_replaceall():
    blocks = [{"id": "a", "type": "paragraph", "data": {"text": "x"}}]
    _, summary = apply_operations(
        blocks, [{"op": "replaceAll", "blocks": []}], selected_block_id="a"
    )
    assert "not allowed" in summary.lower()
    assert blocks[0]["data"]["text"] == "x"  # untouched


def test_apply_slides_operations_create_and_update():
    deck: dict = {"slides": []}
    apply_slides_operations(
        deck,
        [
            {
                "op": "create_slide",
                "layout": "content",
                "elements": [{"type": "heading", "content": "T"}],
            }
        ],
    )
    assert len(deck["slides"]) == 1
    slide_id = deck["slides"][0]["id"]
    el_id = deck["slides"][0]["elements"][0]["id"]
    apply_slides_operations(
        deck,
        [{"op": "update_element", "slide_id": slide_id, "element_id": el_id, "content": "T2"}],
    )
    assert deck["slides"][0]["elements"][0]["content"] == "T2"


def test_apply_slides_per_slide_mode_blocks_replace_all():
    deck = {"slides": [{"id": "s1", "elements": []}]}
    _, summary = apply_slides_operations(
        deck, [{"op": "replace_all", "deck": {}}], selected_slide_id="s1"
    )
    assert "not allowed" in summary.lower()
    assert deck["slides"][0]["id"] == "s1"


def test_a1_notation_parsers():
    assert parse_cell_ref("B5") == ("", 4, 1)
    assert parse_cell_ref("Sheet1!C1", "X") == ("Sheet1", 0, 2)
    assert parse_range_ref("A1:C10") == ("", 0, 0, 9, 2)


def test_apply_spreadsheet_ops_set_and_formula():
    snap: dict = {}
    apply_spreadsheet_ops(
        snap,
        [
            {"op": "setCell", "cell": "A1", "value": "Revenue"},
            {"op": "setCell", "cell": "B1", "value": 100},
            {"op": "setFormula", "cell": "C1", "formula": "=SUM(A1:B1)"},
        ],
    )
    sheet = snap["sheets"]["Sheet1"]
    assert sheet["cellData"]["0"]["0"]["v"] == "Revenue"
    assert sheet["cellData"]["0"]["1"]["v"] == 100
    assert sheet["cellData"]["0"]["1"]["t"] == 2  # number
    assert sheet["cellData"]["0"]["2"]["f"] == "=SUM(A1:B1)"


# ---------------------------------------------------------------------------
# editor_blocks transport round-trip
# ---------------------------------------------------------------------------


def test_editor_blocks_transport_round_trip():
    ed.clear_editor_blocks("f1")
    blocks = [{"id": "a", "type": "paragraph", "data": {"text": "hi"}}]
    ed.set_editor_blocks("f1", blocks)
    assert ed.get_editor_blocks("f1") == blocks
    # get_active_blocks falls back to the store when no ContextVar session.
    assert ed.get_active_blocks() == blocks
    ed.clear_editor_blocks("f1")
    assert ed.get_editor_blocks("f1") is None


def test_slides_and_spreadsheet_transport_round_trip():
    es.clear_slides_data("s1")
    esp.clear_spreadsheet_snapshot("x1")
    es.set_slides_data("s1", {"slides": [{"id": "a"}]})
    esp.set_spreadsheet_snapshot("x1", {"sheets": {"Sheet1": {}}})
    assert es.get_slides_data("s1") == {"slides": [{"id": "a"}]}
    assert esp.get_spreadsheet_snapshot("x1") == {"sheets": {"Sheet1": {}}}
    es.clear_slides_data("s1")
    esp.clear_spreadsheet_snapshot("x1")
    assert es.get_slides_data("s1") is None
    assert esp.get_spreadsheet_snapshot("x1") is None


# ---------------------------------------------------------------------------
# FL-2-wired tool tests (revertable version) — mirrors test_library_verbs harness
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """In-memory StorageAdapter stand-in (put + open over a dict of blobs)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str):
        from pocketpaw.uploads.adapter import StoredObject

        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
        data = b"".join(chunks)
        self._blobs[key] = data
        return StoredObject(key=key, size=len(data), mime=mime)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        data = self._blobs.get(key)
        if data is None:
            raise FileNotFoundError(key)
        yield data


@pytest.fixture
def fake_storage():
    adapter = _FakeAdapter()
    prev = fv_service._adapter
    fv_service.set_adapter(adapter)
    yield adapter
    fv_service._adapter = prev


@contextmanager
def _workspace(workspace_id: str, module, user_id: str = "u1"):
    """Patch a tool module's ``_current_workspace`` / ``_current_user`` getters."""
    orig_ws = module._current_workspace
    orig_user = module._current_user
    module._current_workspace = lambda: workspace_id
    module._current_user = lambda: user_id
    try:
        yield
    finally:
        module._current_workspace = orig_ws
        module._current_user = orig_user


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _seed_upload(
    workspace: str,
    file_id: str,
    *,
    filename: str,
    mime: str,
    content: str,
    adapter: _FakeAdapter,
) -> FileUpload:
    """Seed a Library FileUpload row + its blob (bypassing the guarded route)."""
    storage_key = f"editor/{workspace}/{file_id}"
    await adapter.put(storage_key, _one_chunk(content.encode("utf-8")), mime)
    doc = FileUpload(
        file_id=file_id,
        storage_key=storage_key,
        filename=filename,
        mime=mime,
        size=len(content.encode("utf-8")),
        workspace=workspace,
        owner="u1",
        folder_path="/",
        content_version=1,
    )
    await doc.insert()
    return doc


def _ctx(workspace_id: str):
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id="u1",
        workspace_id=workspace_id,
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_edit_document_writes_revertable_version(mongo_db, fake_storage):
    original = json.dumps(
        {"blocks": [{"id": "a", "type": "paragraph", "data": {"text": "original"}}]}
    )
    await _seed_upload(
        "w1",
        "doc1",
        filename="notes.json",
        mime="application/json",
        content=original,
        adapter=fake_storage,
    )

    with _workspace("w1", ed):
        out = await EditDocumentTool().execute(
            "doc1", [{"op": "update", "id": "a", "data": {"text": "edited"}}]
        )
    assert "version" in out.lower() and "revertable" in out.lower()

    ctx = _ctx("w1")
    # The pre-edit content is archived + recoverable.
    versions = await fv_service.list_versions(ctx, "doc1")
    assert len(versions) == 1
    archived = await fv_service.get_version(ctx, "doc1", versions[0].id)
    assert json.loads(archived.content)["blocks"][0]["data"]["text"] == "original"

    # The live blob now carries the edit, editor_kind agent, bumped version.
    doc = await FileUpload.find_one({"file_id": "doc1", "workspace": "w1"})
    assert doc.content_version == 2
    live = json.loads(fake_storage._blobs[doc.storage_key].decode())
    assert live["blocks"][0]["data"]["text"] == "edited"
    assert versions[0].editor_kind == "agent"


@pytest.mark.asyncio
async def test_edit_document_cross_workspace_denied(mongo_db, fake_storage):
    original = json.dumps(
        {"blocks": [{"id": "a", "type": "paragraph", "data": {"text": "secret"}}]}
    )
    await _seed_upload(
        "wA",
        "docA",
        filename="a.json",
        mime="application/json",
        content=original,
        adapter=fake_storage,
    )

    with _workspace("wB", ed):
        out = await EditDocumentTool().execute(
            "docA", [{"op": "update", "id": "a", "data": {"text": "x"}}]
        )
    assert "not found" in out.lower()

    ctx = _ctx("wA")
    assert await fv_service.list_versions(ctx, "docA") == []
    doc = await FileUpload.find_one({"file_id": "docA", "workspace": "wA"})
    assert doc.content_version == 1  # untouched


@pytest.mark.asyncio
async def test_edit_slides_writes_revertable_version(mongo_db, fake_storage):
    original = json.dumps({"slides": [{"id": "s1", "layout": "content", "elements": []}]})
    await _seed_upload(
        "w1",
        "deck1",
        filename="deck.json",
        mime="application/json",
        content=original,
        adapter=fake_storage,
    )

    with _workspace("w1", es):
        out = await EditSlidesTool().execute(
            "deck1",
            [{"op": "insert_element", "slide_id": "s1", "type": "heading", "content": "Hello"}],
        )
    assert "revertable" in out.lower()

    ctx = _ctx("w1")
    versions = await fv_service.list_versions(ctx, "deck1")
    assert len(versions) == 1
    doc = await FileUpload.find_one({"file_id": "deck1", "workspace": "w1"})
    assert doc.content_version == 2
    live = json.loads(fake_storage._blobs[doc.storage_key].decode())
    assert live["slides"][0]["elements"][0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_edit_spreadsheet_writes_revertable_version(mongo_db, fake_storage):
    await _seed_upload(
        "w1",
        "wb1",
        filename="book.json",
        mime="application/json",
        content="{}",
        adapter=fake_storage,
    )

    with _workspace("w1", esp):
        out = await EditSpreadsheetTool().execute(
            "wb1", [{"op": "setCell", "cell": "A1", "value": "Total"}]
        )
    assert "revertable" in out.lower()

    ctx = _ctx("w1")
    versions = await fv_service.list_versions(ctx, "wb1")
    assert len(versions) == 1
    archived = await fv_service.get_version(ctx, "wb1", versions[0].id)
    assert archived.content == "{}"  # pre-edit content recoverable

    doc = await FileUpload.find_one({"file_id": "wb1", "workspace": "w1"})
    assert doc.content_version == 2
    live = json.loads(fake_storage._blobs[doc.storage_key].decode())
    assert live["sheets"]["Sheet1"]["cellData"]["0"]["0"]["v"] == "Total"


@pytest.mark.asyncio
async def test_edit_document_non_text_rejected(mongo_db, fake_storage):
    await _seed_upload(
        "w1",
        "img1",
        filename="pic.png",
        mime="image/png",
        content="binary",
        adapter=fake_storage,
    )
    with _workspace("w1", ed):
        out = await EditDocumentTool().execute(
            "img1", [{"op": "insert", "type": "paragraph", "data": {"text": "x"}}]
        )
    assert "cannot be edited" in out.lower() or "not_editable" in out.lower()


@pytest.mark.asyncio
async def test_edit_document_requires_workspace(fake_storage):
    # No workspace bound → refuses before any DB access.
    with _workspace("", ed):
        out = await EditDocumentTool().execute("f", [{"op": "delete", "id": "a"}])
    assert "workspace context" in out.lower()
