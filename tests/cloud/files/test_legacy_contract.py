"""Regression: /api/v1/files must keep the Cluster E #998 response shape.

Shape: {workspace_id: str, source: str, files: list[dict], warnings: list[dict]}
Each file row has: id, source, filename, mime, size, url, created, chat_id.
"""
import pytest

from ee.cloud.files.service import UnifiedFilesService


@pytest.mark.asyncio
async def test_legacy_shape_with_empty_store():
    class _Store:
        async def list_by_workspace(self, workspace_id: str, *, limit: int = 500):
            return []

    svc = UnifiedFilesService(_Store())
    body = await svc.list("ws_1", source="all")
    assert set(body.keys()) == {"workspace_id", "source", "files", "warnings"}
    assert body["workspace_id"] == "ws_1"
    assert body["source"] == "all"
    assert body["files"] == []
    assert {"source": "drive", "code": "drive.not_connected"} in body["warnings"]
    assert {"source": "local", "code": "local.client_only"} in body["warnings"]
