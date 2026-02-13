# Test for file browser "Empty directory" bug.
# Created: 2026-02-12
#
# Reproduces the bug where handle_file_browse returns empty files when a
# directory has many hidden (dot-prefixed) items that exhaust the items[:50]
# limit before any visible files are included.

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket that captures sent JSON."""
    ws = AsyncMock()
    ws.sent_messages = []

    async def capture_send(data):
        ws.sent_messages.append(data)

    ws.send_json = capture_send
    return ws


@pytest.fixture
def mock_settings(tmp_path):
    """Create mock settings with file_jail_path set to tmp_path."""
    settings = MagicMock()
    settings.file_jail_path = tmp_path
    return settings


class TestFileBrowseHiddenFileBug:
    """Reproduce the bug: items[:50] slice applied BEFORE hidden file filter.

    When a directory has 50+ hidden items, the slice exhausts the limit and
    no visible files survive the subsequent `.startswith(".")` filter.
    """

    @pytest.mark.asyncio
    async def test_empty_result_with_many_hidden_dirs(
        self, mock_websocket, mock_settings, tmp_path
    ):
        """BUG REPRODUCER: 55 hidden dirs + 5 visible dirs → should return 5 visible, but returns 0."""
        from pocketclaw.dashboard import handle_file_browse

        # Create 55 hidden directories (sorted before any visible dir)
        for i in range(55):
            (tmp_path / f".hidden_dir_{i:02d}").mkdir()

        # Create 5 visible directories
        visible_dirs = ["Applications", "Desktop", "Documents", "Downloads", "Music"]
        for name in visible_dirs:
            (tmp_path / name).mkdir()

        # Browse the directory
        await handle_file_browse(mock_websocket, str(tmp_path), mock_settings)

        assert len(mock_websocket.sent_messages) == 1
        response = mock_websocket.sent_messages[0]
        assert "error" not in response, f"Unexpected error: {response.get('error')}"
        assert "files" in response

        files = response["files"]
        file_names = [f["name"] for f in files]

        # This assertion fails with the current bug — files is [] because
        # items[:50] only captured hidden dirs which were then filtered out
        assert len(files) > 0, (
            "Expected visible directories but got empty list. "
            "This is the bug: items[:50] applied before hidden file filtering."
        )
        # All 5 visible dirs should be present
        for name in visible_dirs:
            assert name in file_names, f"Expected '{name}' in file list, got: {file_names}"

    @pytest.mark.asyncio
    async def test_visible_files_returned_when_few_hidden(
        self, mock_websocket, mock_settings, tmp_path
    ):
        """Baseline: with few hidden items, visible files ARE returned correctly."""
        from pocketclaw.dashboard import handle_file_browse

        # Create 5 hidden directories
        for i in range(5):
            (tmp_path / f".hidden_{i}").mkdir()

        # Create 5 visible directories
        visible_dirs = ["Desktop", "Documents", "Downloads"]
        for name in visible_dirs:
            (tmp_path / name).mkdir()

        # Create 2 visible files
        (tmp_path / "readme.txt").write_text("hello")
        (tmp_path / "notes.md").write_text("notes")

        await handle_file_browse(mock_websocket, str(tmp_path), mock_settings)

        response = mock_websocket.sent_messages[0]
        files = response["files"]
        file_names = [f["name"] for f in files]

        # With few hidden items, visible items should appear
        assert len(files) == 5  # 3 dirs + 2 files
        for name in visible_dirs:
            assert name in file_names
        assert "readme.txt" in file_names
        assert "notes.md" in file_names

    @pytest.mark.asyncio
    async def test_hidden_files_never_included(self, mock_websocket, mock_settings, tmp_path):
        """Verify hidden files are always filtered out of results."""
        from pocketclaw.dashboard import handle_file_browse

        (tmp_path / ".gitconfig").write_text("config")
        (tmp_path / ".ssh").mkdir()
        (tmp_path / "Documents").mkdir()
        (tmp_path / "visible_file.txt").write_text("data")

        await handle_file_browse(mock_websocket, str(tmp_path), mock_settings)

        response = mock_websocket.sent_messages[0]
        files = response["files"]
        file_names = [f["name"] for f in files]

        assert ".gitconfig" not in file_names
        assert ".ssh" not in file_names
        assert "Documents" in file_names
        assert "visible_file.txt" in file_names

    @pytest.mark.asyncio
    async def test_limit_applies_to_visible_items(self, mock_websocket, mock_settings, tmp_path):
        """The 50-item limit should apply to VISIBLE items, not all items."""
        from pocketclaw.dashboard import handle_file_browse

        # Create 30 hidden dirs + 60 visible dirs
        for i in range(30):
            (tmp_path / f".hidden_{i:02d}").mkdir()
        for i in range(60):
            (tmp_path / f"visible_dir_{i:02d}").mkdir()

        await handle_file_browse(mock_websocket, str(tmp_path), mock_settings)

        response = mock_websocket.sent_messages[0]
        files = response["files"]

        # Should get exactly 50 visible items (not 20 = 50 - 30 hidden)
        assert len(files) == 50, (
            f"Expected 50 visible items but got {len(files)}. "
            f"The limit should apply after filtering hidden files."
        )
