# Tests for OCR tool (Sprint 28)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestOCRToolSchema:
    """Test OCRTool properties and schema."""

    def test_name(self):
        from pocketclaw.tools.builtin.ocr import OCRTool

        tool = OCRTool()
        assert tool.name == "ocr"

    def test_trust_level(self):
        from pocketclaw.tools.builtin.ocr import OCRTool

        tool = OCRTool()
        assert tool.trust_level == "standard"

    def test_parameters(self):
        from pocketclaw.tools.builtin.ocr import OCRTool

        tool = OCRTool()
        params = tool.parameters
        assert "image_path" in params["properties"]
        assert "prompt" in params["properties"]
        assert "image_path" in params["required"]

    def test_description(self):
        from pocketclaw.tools.builtin.ocr import OCRTool

        tool = OCRTool()
        assert "ocr" in tool.description.lower() or "extract text" in tool.description.lower()


@pytest.fixture
def _mock_settings():
    settings = MagicMock()
    settings.openai_api_key = "test-key"
    with patch("pocketclaw.tools.builtin.ocr.get_settings", return_value=settings):
        yield settings


async def test_ocr_file_not_found(_mock_settings):
    from pocketclaw.tools.builtin.ocr import OCRTool

    tool = OCRTool()
    result = await tool.execute(image_path="/nonexistent/image.png")
    assert result.startswith("Error:")
    assert "not found" in result


async def test_ocr_unsupported_format(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.ocr import OCRTool

    tool = OCRTool()
    bad_file = tmp_path / "test.xyz"
    bad_file.write_bytes(b"\x00")
    result = await tool.execute(image_path=str(bad_file))
    assert result.startswith("Error:")
    assert "Unsupported" in result


async def test_ocr_file_too_large(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.ocr import OCRTool

    tool = OCRTool()
    big_file = tmp_path / "big.png"
    big_file.write_bytes(b"\x00" * (21 * 1024 * 1024))
    result = await tool.execute(image_path=str(big_file))
    assert result.startswith("Error:")
    assert "too large" in result


async def test_ocr_openai_success(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.ocr import OCRTool

    tool = OCRTool()
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"choices": [{"message": {"content": "Hello World"}}]}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        result = await tool.execute(image_path=str(img))

    assert "Hello World" in result
    assert "OCR result" in result


async def test_ocr_no_text_detected(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.ocr import OCRTool

    tool = OCRTool()
    img = tmp_path / "blank.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"choices": [{"message": {"content": ""}}]}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        result = await tool.execute(image_path=str(img))

    assert "No text detected" in result


async def test_ocr_no_api_key_no_tesseract(tmp_path):
    from pocketclaw.tools.builtin.ocr import OCRTool

    tool = OCRTool()
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)

    settings = MagicMock()
    settings.openai_api_key = None
    with patch("pocketclaw.tools.builtin.ocr.get_settings", return_value=settings):
        # Mock pytesseract as not installed
        with patch.dict("sys.modules", {"pytesseract": None}):
            result = await tool.execute(image_path=str(img))

    assert result.startswith("Error:")
    assert "No OCR provider" in result or "pytesseract" in result.lower()


async def test_ocr_api_error(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.ocr import OCRTool

    tool = OCRTool()
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    import httpx as httpx_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.request = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "rate limited", request=mock_resp.request, response=mock_resp
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        result = await tool.execute(image_path=str(img))

    assert result.startswith("Error:")
    assert "429" in result
