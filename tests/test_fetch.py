"""Tests for fetch.py security and functionality."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from pocketpaw.tools.fetch import FetchRequest, get_directory_keyboard, handle_path, is_safe_path, list_directory


class TestFetchRequestValidation:
    """Test Pydantic validation for FetchRequest."""

    def test_valid_path(self) -> None:
        """Test that valid path strings are accepted."""
        req = FetchRequest(path="/tmp/test")
        assert req.path == "/tmp/test"

    def test_empty_string_rejected(self) -> None:
        """Test that empty string path is rejected."""
        with pytest.raises(ValueError, match="Path string cannot be empty or whitespace"):
            FetchRequest(path="")

    def test_whitespace_only_rejected(self) -> None:
        """Test that whitespace-only strings are rejected."""
        with pytest.raises(ValueError, match="Path string cannot be empty or whitespace"):
            FetchRequest(path="   ")

    def test_tab_only_rejected(self) -> None:
        """Test that tab-only strings are rejected."""
        with pytest.raises(ValueError, match="Path string cannot be empty or whitespace"):
            FetchRequest(path="\t\t")

    def test_newline_only_rejected(self) -> None:
        """Test that newline-only strings are rejected."""
        with pytest.raises(ValueError, match="Path string cannot be empty or whitespace"):
            FetchRequest(path="\n\n")

    def test_path_with_leading_trailing_spaces(self) -> None:
        """Test that paths with leading/trailing spaces are trimmed but accepted."""
        req = FetchRequest(path="  /tmp/test  ")
        # Validators strip but accept, the actual path is stored as-is
        assert req.path == "  /tmp/test  "


class TestIsSafePath:
    """Test path safety checks."""

    def test_path_within_jail(self) -> None:
        """Test that paths within jail directory are safe."""
        with TemporaryDirectory() as tmpdir:
            jail = Path(tmpdir)
            test_path = jail / "subdir"
            test_path.mkdir()

            assert is_safe_path(test_path, jail) is True

    def test_path_outside_jail(self) -> None:
        """Test that paths outside jail directory are unsafe."""
        with TemporaryDirectory() as tmpdir1:
            with TemporaryDirectory() as tmpdir2:
                jail = Path(tmpdir1)
                outside_path = Path(tmpdir2) / "file.txt"

                assert is_safe_path(outside_path, jail) is False

    def test_path_at_jail_root(self) -> None:
        """Test that path at jail root is safe."""
        with TemporaryDirectory() as tmpdir:
            jail = Path(tmpdir)
            assert is_safe_path(jail, jail) is True

    def test_sibling_directory_unsafe(self) -> None:
        """Test that sibling directories are unsafe."""
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir).parent
            jail = parent / "jail_dir"
            sibling = parent / "sibling_dir"
            jail.mkdir(exist_ok=True)
            sibling.mkdir(exist_ok=True)

            try:
                assert is_safe_path(sibling, jail) is False
            finally:
                jail.rmdir()
                sibling.rmdir()

    def test_relative_path_normalization(self) -> None:
        """Test that relative paths are properly normalized."""
        with TemporaryDirectory() as tmpdir:
            jail = Path(tmpdir)
            # Create a subdirectory
            subdir = jail / "subdir"
            subdir.mkdir()

            # Test with relative path that goes up and back down (should still be safe)
            rel_path = subdir / ".." / "subdir"
            assert is_safe_path(rel_path, jail) is True


@pytest.mark.asyncio
async def test_handle_path_empty_string_rejected() -> None:
    """Test that handle_path rejects empty string paths (security fix for issue #619)."""
    result = await handle_path("", Path.home())
    assert result["type"] == "error"
    assert "Path string cannot be empty or whitespace" in result["message"]


@pytest.mark.asyncio
async def test_handle_path_whitespace_rejected() -> None:
    """Test that handle_path rejects whitespace-only paths."""
    result = await handle_path("   ", Path.home())
    assert result["type"] == "error"
    assert "Path string cannot be empty or whitespace" in result["message"]


@pytest.mark.asyncio
async def test_handle_path_valid_directory() -> None:
    """Test that handle_path correctly handles valid directories."""
    with TemporaryDirectory() as tmpdir:
        result = await handle_path(tmpdir, Path(tmpdir))
        assert result["type"] == "directory"
        assert "keyboard" in result


@pytest.mark.asyncio
async def test_handle_path_valid_file() -> None:
    """Test that handle_path correctly handles valid files."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.txt"
        test_file.write_text("test content")

        result = await handle_path(str(test_file), Path(tmpdir))
        assert result["type"] == "file"
        assert result["filename"] == "test.txt"


@pytest.mark.asyncio
async def test_handle_path_outside_jail() -> None:
    """Test that handle_path rejects paths outside jail."""
    with TemporaryDirectory() as tmpdir1:
        with TemporaryDirectory() as tmpdir2:
            jail = Path(tmpdir1)
            outside = Path(tmpdir2)

            result = await handle_path(str(outside), jail)
            assert result["type"] == "error"
            assert "Access denied" in result["message"]


@pytest.mark.asyncio
async def test_handle_path_nonexistent() -> None:
    """Test that handle_path handles nonexistent paths."""
    with TemporaryDirectory() as tmpdir:
        jail = Path(tmpdir)
        nonexistent = jail / "does_not_exist.txt"

        result = await handle_path(str(nonexistent), jail)
        assert result["type"] == "error"
        assert "does not exist" in result["message"]


def test_list_directory_empty_string_rejected() -> None:
    """Test that list_directory rejects empty string paths (security fix for issue #619)."""
    result = list_directory("", str(Path.home()))
    assert "Validation Error" in result
    assert "Path string cannot be empty or whitespace" in result


def test_list_directory_whitespace_rejected() -> None:
    """Test that list_directory rejects whitespace-only paths."""
    result = list_directory("   ", str(Path.home()))
    assert "Validation Error" in result
    assert "Path string cannot be empty or whitespace" in result


def test_list_directory_valid() -> None:
    """Test that list_directory works with valid paths."""
    with TemporaryDirectory() as tmpdir:
        # Create a test file
        test_file = Path(tmpdir) / "test.txt"
        test_file.write_text("test")

        result = list_directory(tmpdir)
        assert "📂" in result
        assert "test.txt" in result


def test_list_directory_outside_jail() -> None:
    """Test that list_directory rejects paths outside jail."""
    with TemporaryDirectory() as tmpdir1:
        with TemporaryDirectory() as tmpdir2:
            jail = Path(tmpdir1)
            outside = Path(tmpdir2)

            result = list_directory(str(outside), str(jail))
            assert "Access denied" in result


def test_get_directory_keyboard_valid() -> None:
    """Test that get_directory_keyboard works with valid paths."""
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        # Create a subdirectory and file
        subdir = path / "subdir"
        subdir.mkdir()
        (path / "file.txt").write_text("test")

        keyboard = get_directory_keyboard(path, path)
        # Since we can't directly check telegram types, we just verify it doesn't crash
        assert keyboard is not None


def test_get_directory_keyboard_default_jail() -> None:
    """Test that get_directory_keyboard uses home directory as default jail."""
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        # This should work but constrain to home directory by default
        keyboard = get_directory_keyboard(path)
        # Verify it doesn't crash
        assert keyboard is not None


class TestSecurityRegressions:
    """Test security regressions against issue #619."""

    @pytest.mark.asyncio
    async def test_empty_path_cannot_bypass_jail(self) -> None:
        """Regression test: empty path cannot bypass jail restrictions."""
        with TemporaryDirectory() as tmpdir:
            jail = Path(tmpdir)
            # Empty string should be rejected before any path resolution
            result = await handle_path("", jail)
            assert result["type"] == "error"
            assert "Validation Error" in result["message"]

    @pytest.mark.asyncio
    async def test_whitespace_path_cannot_bypass_jail(self) -> None:
        """Regression test: whitespace-only path cannot bypass jail restrictions."""
        with TemporaryDirectory() as tmpdir:
            jail = Path(tmpdir)
            # Whitespace should be rejected before any path resolution
            result = await handle_path("   ", jail)
            assert result["type"] == "error"
            assert "Validation Error" in result["message"]

    def test_path_resolve_with_empty_string_not_called(self) -> None:
        """Verify that Path('').resolve() is not called (validation prevents it)."""
        # This test ensures the fix is in place by verifying
        # that FetchRequest validation catches empty strings
        with pytest.raises(ValueError, match="Path string cannot be empty or whitespace"):
            FetchRequest(path="")
