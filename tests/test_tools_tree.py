"""Tests for DirectoryTreeTool."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.config import Settings
from pocketpaw.tools.builtin.tree import DirectoryTreeTool


@pytest.fixture
def temp_jail():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_settings(temp_jail):
    settings = Settings(file_jail_path=temp_jail)
    with patch("pocketpaw.tools.builtin.tree.get_settings", return_value=settings):
        yield settings


@pytest.fixture
def tree_tool():
    return DirectoryTreeTool()


def _make_structure(root: Path) -> None:
    """Create a sample directory structure for tests."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hello')")
    (root / "src" / "utils").mkdir()
    (root / "src" / "utils" / "helpers.py").write_text("# helpers")
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_text("# docs")
    (root / "README.md").write_text("# project")
    (root / ".hidden").write_text("secret")
    (root / ".config").mkdir()
    (root / ".config" / "settings.json").write_text("{}")


class TestDirectoryTreeBasic:
    @pytest.mark.asyncio
    async def test_basic_tree(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail))

        assert "src/" in result
        assert "docs/" in result
        assert "README.md" in result
        assert "├──" in result or "└──" in result

    @pytest.mark.asyncio
    async def test_tree_includes_summary(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail))

        assert "directories" in result
        assert "files" in result

    @pytest.mark.asyncio
    async def test_nested_structure(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail))

        assert "main.py" in result
        assert "helpers.py" in result


class TestMaxDepth:
    @pytest.mark.asyncio
    async def test_depth_1(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail), max_depth=1)

        # Should show top-level entries but not nested ones
        assert "src/" in result
        assert "helpers.py" not in result

    @pytest.mark.asyncio
    async def test_depth_0(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail), max_depth=0)

        # Should show only the root line and summary
        assert "0 directories, 0 files" in result


class TestShowHidden:
    @pytest.mark.asyncio
    async def test_hidden_excluded_by_default(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail))

        assert ".hidden" not in result
        assert ".config" not in result

    @pytest.mark.asyncio
    async def test_hidden_included(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail), show_hidden=True)

        assert ".hidden" in result
        assert ".config/" in result


class TestShowSize:
    @pytest.mark.asyncio
    async def test_size_not_shown_by_default(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail))

        # Should not contain size annotations like "(X B)"
        assert " B)" not in result

    @pytest.mark.asyncio
    async def test_size_shown(self, temp_jail, mock_settings, tree_tool):
        _make_structure(temp_jail)
        result = await tree_tool.execute(path=str(temp_jail), show_size=True)

        # File entries should have size annotations
        assert " B)" in result or " KB)" in result


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_directory(self, temp_jail, mock_settings, tree_tool):
        empty = temp_jail / "empty"
        empty.mkdir()
        result = await tree_tool.execute(path=str(empty))

        assert "0 directories, 0 files" in result

    @pytest.mark.asyncio
    async def test_nonexistent_path(self, temp_jail, mock_settings, tree_tool):
        result = await tree_tool.execute(path=str(temp_jail / "nope"))

        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_file_not_directory(self, temp_jail, mock_settings, tree_tool):
        f = temp_jail / "file.txt"
        f.write_text("hi")
        result = await tree_tool.execute(path=str(f))

        assert "Error" in result
        assert "Not a directory" in result


class TestSecurity:
    @pytest.mark.asyncio
    async def test_jail_break_blocked(self, temp_jail, mock_settings, tree_tool):
        outside = str(temp_jail / ".." / "secret_dir")
        result = await tree_tool.execute(path=outside)

        assert "Access denied" in result

    @pytest.mark.asyncio
    async def test_startswith_bypass_blocked(self, temp_jail, mock_settings, tree_tool):
        outside_prefix = temp_jail.parent / f"{temp_jail.name}_outside"
        outside_prefix.mkdir(exist_ok=True)
        (outside_prefix / "file.txt").write_text("outside")

        result = await tree_tool.execute(path=str(outside_prefix))

        assert "Access denied" in result

    @pytest.mark.asyncio
    async def test_symlink_is_skipped(self, temp_jail, mock_settings, tree_tool):
        target = temp_jail / "target"
        target.mkdir()
        (target / "data.txt").write_text("ok")

        link = temp_jail / "link_to_target"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as e:
            if getattr(e, "winerror", None) == 1314:
                pytest.skip("Symlink creation requires elevated privilege on this Windows setup")
            raise

        result = await tree_tool.execute(path=str(temp_jail), show_hidden=True)

        assert "link_to_target -> [symlink skipped]" in result


class TestTruncation:
    @pytest.mark.asyncio
    async def test_max_entries_truncation(self, temp_jail, mock_settings, tree_tool):
        # Create more than MAX_ENTRIES files
        big_dir = temp_jail / "big"
        big_dir.mkdir()
        for i in range(600):
            (big_dir / f"file_{i:04d}.txt").write_text(f"{i}")

        result = await tree_tool.execute(path=str(big_dir), max_depth=1)

        assert "truncated" in result


class TestToolDefinition:
    def test_name(self, tree_tool):
        assert tree_tool.name == "directory_tree"

    def test_definition_schema(self, tree_tool):
        defn = tree_tool.definition
        assert defn.name == "directory_tree"
        assert "path" in defn.parameters["properties"]
        assert defn.trust_level == "standard"
