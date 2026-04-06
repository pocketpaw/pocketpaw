from pathlib import Path

import pytest

from pocketpaw.tools.fetch import (
    FetchRequest,
    handle_path,
    is_safe_path,
    list_directory,
)


class TestIsSafePath:
    """Test path safety checks."""

    def test_path_within_jail(self, tmp_path: Path) -> None:
        """Test that paths within jail directory are safe."""
        jail = tmp_path
        test_path = jail / "subdir"
        test_path.mkdir()
        assert is_safe_path(test_path, jail) is True

    def test_path_outside_jail(self, tmp_path: Path) -> None:
        """Test that paths outside jail directory are unsafe."""
        jail = tmp_path / "jail"
        jail.mkdir()
        outside_path = tmp_path / "outside.txt"
        assert is_safe_path(outside_path, jail) is False

    def test_path_at_jail_root(self, tmp_path: Path) -> None:
        """Test that path at jail root is safe."""
        jail = tmp_path
        assert is_safe_path(jail, jail) is True

    def test_sibling_directory_unsafe(self, tmp_path: Path) -> None:
        """Test that sibling directories are unsafe."""
        jail = tmp_path / "jail_dir"
        sibling = tmp_path / "sibling_dir"
        jail.mkdir()
        sibling.mkdir()
        assert is_safe_path(sibling, jail) is False


@pytest.mark.asyncio
async def test_handle_path_empty_string_rejected() -> None:
    """Test that handle_path rejects empty string paths (security fix for issue #619)."""
    result = await handle_path("", Path.home())
    assert result["type"] == "error"
    assert "Validation Error" in result["message"]


@pytest.mark.asyncio
async def test_handle_path_whitespace_rejected() -> None:
    """Test that handle_path rejects whitespace-only paths."""
    result = await handle_path("   ", Path.home())
    assert result["type"] == "error"
    assert "Validation Error" in result["message"]


@pytest.mark.asyncio
async def test_handle_path_outside_jail(tmp_path: Path) -> None:
    """Test that handle_path rejects paths outside jail."""
    jail = tmp_path / "jail"
    jail.mkdir()
    outside = tmp_path / "outside.txt"
    result = await handle_path(str(outside), jail)
    assert result["type"] == "error"
    assert "Access denied" in result["message"]


def test_list_directory_empty_string_rejected() -> None:
    """Test that list_directory rejects empty string paths."""
    result = list_directory("", str(Path.home()))
    assert "Validation Error" in result


def test_list_directory_outside_jail(tmp_path: Path) -> None:
    """Test that list_directory rejects paths outside jail."""
    jail = tmp_path / "jail"
    jail.mkdir()
    outside = tmp_path / "outside_dir"
    result = list_directory(str(outside), str(jail))
    assert "Access denied" in result


class TestSecurityRegressions:
    """Test security regressions against issue #619."""

    @pytest.mark.asyncio
    async def test_empty_path_cannot_bypass_jail(self, tmp_path: Path) -> None:
        """Regression test: empty path cannot bypass jail restrictions."""
        jail = tmp_path / "jail"
        jail.mkdir()
        result = await handle_path("", jail)
        assert result["type"] == "error"
        assert "Validation Error" in result["message"]

    def test_path_resolve_with_empty_string_not_called(self, tmp_path: Path) -> None:
        """Verify that validation catches empty strings preventing bypasses."""
        with pytest.raises(ValueError, match="Path string cannot be empty or whitespace"):
            FetchRequest(path_str="", jail_str=str(tmp_path))
