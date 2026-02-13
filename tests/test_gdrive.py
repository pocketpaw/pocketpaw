# Tests for Google Drive integration (Sprint 25)

from unittest.mock import AsyncMock, MagicMock, patch


class TestDriveToolSchemas:
    """Test Drive tool properties and schemas."""

    def test_drive_list_tool(self):
        from pocketclaw.tools.builtin.gdrive import DriveListTool

        tool = DriveListTool()
        assert tool.name == "drive_list"
        assert tool.trust_level == "high"

    def test_drive_download_tool(self):
        from pocketclaw.tools.builtin.gdrive import DriveDownloadTool

        tool = DriveDownloadTool()
        assert tool.name == "drive_download"
        assert "file_id" in tool.parameters["properties"]
        assert "file_id" in tool.parameters["required"]

    def test_drive_upload_tool(self):
        from pocketclaw.tools.builtin.gdrive import DriveUploadTool

        tool = DriveUploadTool()
        assert tool.name == "drive_upload"
        assert "file_path" in tool.parameters["properties"]
        assert "file_path" in tool.parameters["required"]

    def test_drive_share_tool(self):
        from pocketclaw.tools.builtin.gdrive import DriveShareTool

        tool = DriveShareTool()
        assert tool.name == "drive_share"
        assert "file_id" in tool.parameters["properties"]
        assert "email" in tool.parameters["properties"]
        assert "role" in tool.parameters["properties"]


async def test_drive_list_no_auth():
    from pocketclaw.tools.builtin.gdrive import DriveListTool

    tool = DriveListTool()
    with patch(
        "pocketclaw.integrations.gdrive.DriveClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute()
    assert result.startswith("Error:")
    assert "authenticated" in result.lower()


async def test_drive_list_success():
    from pocketclaw.tools.builtin.gdrive import DriveListTool

    tool = DriveListTool()
    mock_files = [
        {
            "id": "abc123",
            "name": "report.pdf",
            "mimeType": "application/pdf",
            "size": "1024",
        }
    ]
    with patch(
        "pocketclaw.integrations.gdrive.DriveClient._get_token",
        new_callable=AsyncMock,
        return_value="fake-token",
    ):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"files": mock_files}
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(query="name contains 'report'")

    assert "report.pdf" in result
    assert "abc123" in result


async def test_drive_list_empty():
    from pocketclaw.tools.builtin.gdrive import DriveListTool

    tool = DriveListTool()
    with patch(
        "pocketclaw.integrations.gdrive.DriveClient._get_token",
        new_callable=AsyncMock,
        return_value="fake-token",
    ):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"files": []}
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute()

    assert "No files found" in result


async def test_drive_download_no_auth():
    from pocketclaw.tools.builtin.gdrive import DriveDownloadTool

    tool = DriveDownloadTool()
    with patch(
        "pocketclaw.integrations.gdrive.DriveClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(file_id="abc123")
    assert result.startswith("Error:")


async def test_drive_upload_no_auth():
    from pocketclaw.tools.builtin.gdrive import DriveUploadTool

    tool = DriveUploadTool()
    with patch(
        "pocketclaw.integrations.gdrive.DriveClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(file_path="/tmp/test.txt")
    assert result.startswith("Error:")


async def test_drive_upload_file_not_found():
    from pocketclaw.tools.builtin.gdrive import DriveUploadTool

    tool = DriveUploadTool()
    with patch(
        "pocketclaw.integrations.gdrive.DriveClient._get_token",
        new_callable=AsyncMock,
        return_value="fake-token",
    ):
        result = await tool.execute(file_path="/nonexistent/file.txt")
    assert result.startswith("Error:")
    assert "not found" in result.lower()


async def test_drive_share_invalid_role():
    from pocketclaw.tools.builtin.gdrive import DriveShareTool

    tool = DriveShareTool()
    result = await tool.execute(file_id="abc", email="x@y.com", role="admin")
    assert result.startswith("Error:")
    assert "Invalid role" in result


async def test_drive_share_no_auth():
    from pocketclaw.tools.builtin.gdrive import DriveShareTool

    tool = DriveShareTool()
    with patch(
        "pocketclaw.integrations.gdrive.DriveClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute(file_id="abc", email="x@y.com")
    assert result.startswith("Error:")
