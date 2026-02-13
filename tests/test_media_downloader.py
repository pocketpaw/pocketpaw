# Tests for MediaDownloader utility
# Created: 2026-02-11

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketclaw.bus.media import (  # noqa: E402
    MediaDownloader,
    _sanitize_filename,
    _unique_filename,
    build_media_hint,
    get_media_dir,
    get_media_downloader,
)

# --- Helpers ---


def test_sanitize_filename_basic():
    assert _sanitize_filename("photo.jpg") == "photo.jpg"


def test_sanitize_filename_special_chars():
    result = _sanitize_filename("my file (1).jpg")
    assert "/" not in result
    assert "(" not in result
    assert ".jpg" in result


def test_sanitize_filename_empty():
    assert _sanitize_filename("") == "file"


def test_sanitize_filename_slashes():
    result = _sanitize_filename("../../etc/passwd")
    assert "/" not in result


def test_unique_filename_format():
    name = _unique_filename("photo.jpg", "image/jpeg")
    parts = name.split("_", 2)
    assert len(parts) == 3  # timestamp_hex, hash8, sanitized
    assert name.endswith(".jpg")


def test_unique_filename_adds_extension_from_mime():
    name = _unique_filename("noext", "image/png")
    assert name.endswith(".png")


def test_unique_filename_no_collision():
    """Two calls should produce different filenames."""
    name1 = _unique_filename("photo.jpg")
    name2 = _unique_filename("photo.jpg")
    assert name1 != name2


def test_build_media_hint_empty():
    assert build_media_hint([]) == ""


def test_build_media_hint_single():
    result = build_media_hint(["photo.jpg"])
    assert result == "\n[Attached: photo.jpg]"


def test_build_media_hint_multiple():
    result = build_media_hint(["photo.jpg", "doc.pdf"])
    assert result == "\n[Attached: photo.jpg, doc.pdf]"


# --- get_media_dir ---


@patch("pocketclaw.bus.media.get_settings")
@patch("pocketclaw.bus.media.get_config_dir")
def test_get_media_dir_default(mock_config_dir, mock_settings, tmp_path):
    mock_settings.return_value = MagicMock(media_download_dir="")
    mock_config_dir.return_value = tmp_path
    result = get_media_dir()
    assert result == tmp_path / "media"
    assert result.exists()


@patch("pocketclaw.bus.media.get_settings")
def test_get_media_dir_custom(mock_settings, tmp_path):
    custom = tmp_path / "custom_media"
    mock_settings.return_value = MagicMock(media_download_dir=str(custom))
    result = get_media_dir()
    assert result == custom
    assert result.exists()


# --- MediaDownloader.save_from_bytes ---


@patch("pocketclaw.bus.media.get_media_dir")
@patch("pocketclaw.bus.media.get_settings")
async def test_save_from_bytes(mock_settings, mock_dir, tmp_path):
    mock_settings.return_value = MagicMock(media_max_file_size_mb=50)
    mock_dir.return_value = tmp_path

    dl = MediaDownloader()
    path = await dl.save_from_bytes(b"hello world", "test.txt", "text/plain")
    assert os.path.exists(path)
    assert open(path, "rb").read() == b"hello world"


@patch("pocketclaw.bus.media.get_media_dir")
@patch("pocketclaw.bus.media.get_settings")
async def test_save_from_bytes_size_limit(mock_settings, mock_dir, tmp_path):
    mock_settings.return_value = MagicMock(media_max_file_size_mb=1)  # 1 MB limit
    mock_dir.return_value = tmp_path

    dl = MediaDownloader()
    data = b"x" * (2 * 1024 * 1024)  # 2 MB
    with pytest.raises(ValueError, match="exceeds limit"):
        await dl.save_from_bytes(data, "big.bin")


@patch("pocketclaw.bus.media.get_media_dir")
@patch("pocketclaw.bus.media.get_settings")
async def test_save_from_bytes_unlimited(mock_settings, mock_dir, tmp_path):
    mock_settings.return_value = MagicMock(media_max_file_size_mb=0)  # unlimited
    mock_dir.return_value = tmp_path

    dl = MediaDownloader()
    data = b"x" * (5 * 1024 * 1024)
    path = await dl.save_from_bytes(data, "big.bin")
    assert os.path.exists(path)


# --- MediaDownloader.download_url ---


@patch("pocketclaw.bus.media.get_media_dir")
@patch("pocketclaw.bus.media.get_settings")
async def test_download_url(mock_settings, mock_dir, tmp_path):
    mock_settings.return_value = MagicMock(media_max_file_size_mb=50)
    mock_dir.return_value = tmp_path

    dl = MediaDownloader()
    mock_resp = MagicMock()
    mock_resp.content = b"file data"
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "image/jpeg"}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.is_closed = False
    dl._client = mock_client

    path = await dl.download_url("https://example.com/photo.jpg", "photo.jpg", "image/jpeg")
    assert os.path.exists(path)
    assert open(path, "rb").read() == b"file data"
    mock_client.get.assert_called_once()


@patch("pocketclaw.bus.media.get_media_dir")
@patch("pocketclaw.bus.media.get_settings")
async def test_download_url_http_error(mock_settings, mock_dir, tmp_path):
    mock_settings.return_value = MagicMock(media_max_file_size_mb=50)
    mock_dir.return_value = tmp_path

    dl = MediaDownloader()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
    )

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.is_closed = False
    dl._client = mock_client

    with pytest.raises(httpx.HTTPStatusError):
        await dl.download_url("https://example.com/missing.jpg")


# --- MediaDownloader.download_url_with_auth ---


@patch("pocketclaw.bus.media.get_media_dir")
@patch("pocketclaw.bus.media.get_settings")
async def test_download_url_with_auth(mock_settings, mock_dir, tmp_path):
    mock_settings.return_value = MagicMock(media_max_file_size_mb=50)
    mock_dir.return_value = tmp_path

    dl = MediaDownloader()
    mock_resp = MagicMock()
    mock_resp.content = b"auth file"
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/pdf"}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.is_closed = False
    dl._client = mock_client

    path = await dl.download_url_with_auth(
        "https://slack.com/files/doc.pdf", "Bearer xoxb-token", name="doc.pdf"
    )
    assert os.path.exists(path)

    # Verify auth header was passed
    call_kwargs = mock_client.get.call_args
    assert call_kwargs[1]["headers"]["Authorization"] == "Bearer xoxb-token"


# --- get_media_downloader singleton ---


def test_get_media_downloader_singleton():
    import pocketclaw.bus.media as media_mod

    # Reset singleton
    media_mod._downloader = None
    d1 = get_media_downloader()
    d2 = get_media_downloader()
    assert d1 is d2
    media_mod._downloader = None  # cleanup


# --- download_url name inference ---


@patch("pocketclaw.bus.media.get_media_dir")
@patch("pocketclaw.bus.media.get_settings")
async def test_download_url_infers_name(mock_settings, mock_dir, tmp_path):
    """When no name is given, extract from URL."""
    mock_settings.return_value = MagicMock(media_max_file_size_mb=50)
    mock_dir.return_value = tmp_path

    dl = MediaDownloader()
    mock_resp = MagicMock()
    mock_resp.content = b"data"
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "image/png"}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.is_closed = False
    dl._client = mock_client

    path = await dl.download_url("https://cdn.example.com/images/cat.png")
    # Filename should contain "cat.png" somewhere
    assert "cat.png" in path
