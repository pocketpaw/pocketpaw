from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from ee.cloud.files.providers.uploads import UploadsProvider
from ee.cloud.files.schemas import RequestContext
from tests.cloud.files.test_provider_contract import ProviderContract


class TestUploadsProviderContract(ProviderContract):
    def build_provider(self):
        store = MagicMock()

        async def _iter(workspace_id: str, *, include_deleted: bool = False, limit: int = 500):
            yield {
                "file_id": "abc",
                "filename": "report.pdf",
                "mime": "application/pdf",
                "size": 100,
                "owner_id": "u",
                "workspace_id": "ws",
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "tags": [],
            }

        store.iter_by_workspace = _iter
        return UploadsProvider(store=store)


@pytest.mark.asyncio
async def test_uploads_provider_list_entries_maps_fields():
    store = MagicMock()

    async def _iter(workspace_id, *, include_deleted=False, limit=500):
        yield {
            "file_id": "fid1",
            "filename": "a.txt",
            "mime": "text/plain",
            "size": 7,
            "owner_id": "u1",
            "workspace_id": "ws_1",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "tags": [],
        }

    store.iter_by_workspace = _iter
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="u1", workspace_id="ws_1", attributes={})
    page = await p.list_entries(ctx, "/My Files", None, 50, {})
    assert len(page.items) == 1
    e = page.items[0]
    assert e.id == "uploads:fid1"
    assert e.mount_path == "/My Files/a.txt"
    assert e.scope == "personal"


@pytest.mark.asyncio
async def test_uploads_provider_list_mounts_when_ctx_has_workspace():
    store = MagicMock()
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="u", workspace_id="ws", attributes={})
    mounts = await p.list_mounts(ctx)
    assert len(mounts) == 1
    assert mounts[0].path == "/My Files"
    assert mounts[0].writable is True


@pytest.mark.asyncio
async def test_uploads_provider_baseline_rbac_owner_is_manage():
    store = MagicMock()
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="u1", workspace_id="ws", attributes={})
    from tests.cloud.files.conftest import make_entry  # type: ignore  # noqa

    # Build an owned entry directly:
    from datetime import datetime as _dt

    from ee.cloud.files.schemas import FileEntry
    e = FileEntry(
        id="uploads:x",
        provider_id="uploads",
        mount_path="/My Files/x",
        name="x",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws",
        scope="personal",
        tags=[],
        created_at=_dt.now(UTC),
        updated_at=_dt.now(UTC),
        source_ref={},
        capabilities=["read", "download", "rename", "delete"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and perm.write and perm.manage


@pytest.mark.asyncio
async def test_uploads_provider_baseline_rbac_non_owner_is_read_only():
    store = MagicMock()
    p = UploadsProvider(store=store)
    ctx = RequestContext(user_id="other", workspace_id="ws", attributes={})
    from datetime import datetime as _dt

    from ee.cloud.files.schemas import FileEntry
    e = FileEntry(
        id="uploads:x",
        provider_id="uploads",
        mount_path="/My Files/x",
        name="x",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws",
        scope="personal",
        tags=[],
        created_at=_dt.now(UTC),
        updated_at=_dt.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and not perm.write and not perm.manage
