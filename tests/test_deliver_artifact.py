# Tests for DeliverArtifactTool - file delivery to user channels.
# Created: 2026-03-12

from unittest.mock import patch

import pytest

from pocketpaw.config import Settings
from pocketpaw.tools.builtin.deliver import DeliverArtifactTool


@pytest.fixture
def mock_settings(tmp_path):
    settings = Settings(file_jail_path=tmp_path)
    with patch("pocketpaw.tools.builtin.deliver.get_settings", return_value=settings):
        yield settings


@pytest.fixture
def tool():
    return DeliverArtifactTool()


async def test_deliver_basic(tool, mock_settings, tmp_path):
    """Deliver an existing file returns media tag."""
    f = tmp_path / "output.txt"
    f.write_text("hello")
    result = await tool.execute(path=str(f))
    assert f"<!-- media:{f} -->" in result
    assert "output.txt" in result


async def test_deliver_with_caption(tool, mock_settings, tmp_path):
    """Caption appears in the result."""
    f = tmp_path / "chart.png"
    f.write_bytes(b"\x89PNG" + b"\x00" * 100)
    result = await tool.execute(path=str(f), caption="Here's your chart")
    assert "Here's your chart" in result
    assert f"<!-- media:{f} -->" in result


async def test_deliver_image_mime(tool, mock_settings, tmp_path):
    """Image files report correct mime type."""
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)
    result = await tool.execute(path=str(f))
    assert "image/jpeg" in result


async def test_deliver_video_mime(tool, mock_settings, tmp_path):
    """Video files report correct mime type."""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 200)
    result = await tool.execute(path=str(f))
    assert "video/mp4" in result


async def test_deliver_file_not_found(tool, mock_settings, tmp_path):
    """Non-existent file returns error."""
    result = await tool.execute(path=str(tmp_path / "nope.txt"))
    assert "Error" in result
    assert "not found" in result.lower()


async def test_deliver_not_a_file(tool, mock_settings, tmp_path):
    """Directory path returns error."""
    d = tmp_path / "subdir"
    d.mkdir()
    result = await tool.execute(path=str(d))
    assert "Error" in result
    assert "Not a file" in result


async def test_deliver_file_jail(tool, mock_settings, tmp_path):
    """Path outside jail is blocked."""
    result = await tool.execute(path="/etc/passwd")
    assert "Error" in result
    assert "Access denied" in result


async def test_deliver_size_info(tool, mock_settings, tmp_path):
    """Result includes file size information."""
    f = tmp_path / "data.csv"
    f.write_text("a,b,c\n" * 500)
    result = await tool.execute(path=str(f))
    assert "KB" in result or "MB" in result
    assert "data.csv" in result


async def test_deliver_definition(tool):
    """Tool definition has correct metadata."""
    defn = tool.definition
    assert defn.name == "deliver_artifact"
    assert defn.trust_level == "standard"
    assert "path" in defn.parameters["properties"]
    assert "caption" in defn.parameters["properties"]
    assert "path" in defn.parameters["required"]
