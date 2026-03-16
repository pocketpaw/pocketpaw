# Tests for EditFileTool - find-and-replace file editing.
# Created: 2026-03-12

from unittest.mock import patch

import pytest

from pocketpaw.config import Settings
from pocketpaw.tools.builtin.filesystem import EditFileTool


@pytest.fixture
def jail(tmp_path):
    """Temporary directory used as the file jail."""
    return tmp_path


@pytest.fixture
def mock_settings(jail):
    """Patch filesystem.get_settings to use the temp jail."""
    settings = Settings(file_jail_path=jail)
    with patch("pocketpaw.tools.builtin.filesystem.get_settings", return_value=settings):
        yield settings


@pytest.mark.asyncio
async def test_edit_file_basic(jail, mock_settings):
    """Replace one occurrence, content should change."""
    f = jail / "hello.txt"
    f.write_text("Hello World")

    tool = EditFileTool()
    result = await tool.execute(path=str(f), old_string="World", new_string="PocketPaw")

    assert "replacement" in result
    assert f.read_text() == "Hello PocketPaw"


@pytest.mark.asyncio
async def test_edit_file_not_found(jail, mock_settings):
    """Editing a non-existent file returns a 'not found' error."""
    tool = EditFileTool()
    result = await tool.execute(
        path=str(jail / "missing.txt"),
        old_string="anything",
        new_string="replacement",
    )

    assert "Error:" in result
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_edit_file_old_string_missing(jail, mock_settings):
    """old_string not in file returns an error."""
    f = jail / "content.txt"
    f.write_text("The quick brown fox")

    tool = EditFileTool()
    result = await tool.execute(path=str(f), old_string="lazy dog", new_string="cat")

    assert "Error:" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_edit_file_ambiguous(jail, mock_settings):
    """old_string appearing 3 times with replace_all=False returns an error mentioning the count."""
    f = jail / "repeat.txt"
    f.write_text("foo bar foo baz foo")

    tool = EditFileTool()
    result = await tool.execute(path=str(f), old_string="foo", new_string="qux", replace_all=False)

    assert "Error:" in result
    assert "3" in result


@pytest.mark.asyncio
async def test_edit_file_replace_all(jail, mock_settings):
    """replace_all=True replaces every occurrence."""
    f = jail / "repeat.txt"
    f.write_text("foo bar foo baz foo")

    tool = EditFileTool()
    result = await tool.execute(path=str(f), old_string="foo", new_string="qux", replace_all=True)

    assert "Error:" not in result
    assert f.read_text() == "qux bar qux baz qux"
    assert "3" in result


@pytest.mark.asyncio
async def test_edit_file_multiline(jail, mock_settings):
    """Replace a multi-line block."""
    original = "line one\nline two\nline three\n"
    f = jail / "multi.txt"
    f.write_text(original)

    tool = EditFileTool()
    result = await tool.execute(
        path=str(f),
        old_string="line one\nline two\n",
        new_string="replaced block\n",
    )

    assert "Error:" not in result
    assert f.read_text() == "replaced block\nline three\n"


@pytest.mark.asyncio
async def test_edit_file_empty_new_string(jail, mock_settings):
    """Replace with empty string effectively deletes the matched text."""
    f = jail / "delete.txt"
    f.write_text("keep this DELETE that")

    tool = EditFileTool()
    result = await tool.execute(path=str(f), old_string=" DELETE", new_string="")

    assert "Error:" not in result
    assert f.read_text() == "keep this that"


@pytest.mark.asyncio
async def test_edit_file_file_jail(jail, mock_settings):
    """Paths outside the jail are denied."""
    outside = jail.parent / "outside_secret.txt"
    outside.write_text("sensitive data")

    tool = EditFileTool()
    result = await tool.execute(
        path=str(outside),
        old_string="sensitive",
        new_string="redacted",
    )

    assert "Access denied" in result
    # File should be unchanged
    assert outside.read_text() == "sensitive data"


@pytest.mark.asyncio
async def test_edit_file_preserves_rest(jail, mock_settings):
    """Editing one part of a file leaves the rest of the content intact."""
    f = jail / "partial.txt"
    f.write_text("alpha beta gamma delta")

    tool = EditFileTool()
    await tool.execute(path=str(f), old_string="beta", new_string="BETA")

    content = f.read_text()
    assert "alpha" in content
    assert "BETA" in content
    assert "gamma" in content
    assert "delta" in content
    assert "beta" not in content


@pytest.mark.asyncio
async def test_edit_file_definition(mock_settings):
    """Tool definition has correct name, trust level, and required parameters."""
    tool = EditFileTool()
    defn = tool.definition

    assert defn.name == "edit_file"
    assert defn.trust_level == "standard"

    required = defn.parameters.get("required", [])
    assert "path" in required
    assert "old_string" in required
    assert "new_string" in required

    props = defn.parameters.get("properties", {})
    assert "replace_all" in props
    assert props["replace_all"]["type"] == "boolean"
