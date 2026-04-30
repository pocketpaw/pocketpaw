# test_gemini_flash.py — GeminiFlashExtractor tests.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Asserts request structure (model, MIME on Part, image bytes) plus PDF
# strategy (pypdf text per page, sparse-page marker for image-heavy pages).
# No real network calls — google.genai.Client is mocked.
"""Tests for `ee.cloud.extraction.gemini_flash.GeminiFlashExtractor`.

Mocking approach: monkeypatch `google.genai.Client` to return a MagicMock
whose `models.generate_content(...)` returns a stub response. We assert on
the args the SDK was called with — the model name, the inline `Part`
contents, and the MIME type — to pin the request shape.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pypdf import PdfWriter

from ee.cloud.extraction import ExtractionResult


@pytest.fixture
def mock_genai_client(monkeypatch: pytest.MonkeyPatch):
    """Patch `google.genai.Client` to return a MagicMock factory.

    The fixture returns the (client, response) tuple so tests can
    configure response.text and inspect call args.
    """
    from google import genai

    fake_response = MagicMock()
    fake_response.text = "stub caption"
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    def _factory(api_key: str):
        return fake_client

    monkeypatch.setattr(genai, "Client", _factory)
    return fake_client, fake_response


@pytest.fixture
def tmp_image(tmp_path: Path) -> Path:
    p = tmp_path / "diagram.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    return p


@pytest.fixture
def tmp_blank_pdf(tmp_path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    p = tmp_path / "blank.pdf"
    p.write_bytes(buf.getvalue())
    return p


async def test_image_extract_calls_gemini_with_inline_bytes(
    mock_genai_client, tmp_image: Path
) -> None:
    from ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    fake_client, fake_response = mock_genai_client
    fake_response.text = "Caption: a small PNG image."

    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(tmp_image, "image/png")

    assert isinstance(result, ExtractionResult)
    assert result.text == "Caption: a small PNG image."
    assert result.captions == ["Caption: a small PNG image."]
    assert result.backend == "gemini-flash"
    assert result.metadata["model"] == "gemini-2.5-flash"

    fake_client.models.generate_content.assert_called_once()
    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == "gemini-2.5-flash"

    # Request body: [prompt_text, Part(image bytes, image/png)]
    contents = call_kwargs["contents"]
    assert len(contents) == 2
    assert "knowledge base" in contents[0]
    part = contents[1]
    # types.Part is a Pydantic model; the bytes live on inline_data.
    assert getattr(part.inline_data, "mime_type", None) == "image/png"
    assert getattr(part.inline_data, "data", None) == tmp_image.read_bytes()


async def test_image_extract_uses_configured_model(
    mock_genai_client, tmp_image: Path
) -> None:
    from ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    fake_client, _ = mock_genai_client
    extractor = GeminiFlashExtractor(api_key="fake-key", model="custom-model")
    await extractor.extract(tmp_image, "image/png")

    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == "custom-model"


async def test_image_extract_handles_empty_response(
    mock_genai_client, tmp_image: Path
) -> None:
    from ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    _, fake_response = mock_genai_client
    fake_response.text = None

    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(tmp_image, "image/png")

    assert result.text == ""
    assert result.captions == [""]


async def test_pdf_extract_blank_page_marked_sparse(
    mock_genai_client, tmp_blank_pdf: Path
) -> None:
    from ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    fake_client, _ = mock_genai_client
    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(tmp_blank_pdf, "application/pdf")

    # Stage 1.A doesn't ship a PDF→image renderer; sparse pages get a marker.
    assert "[page 1: image-heavy, no caption]" in result.text
    assert result.metadata["sparse_pages"] == [1]
    assert result.metadata["page_count"] == 1
    assert result.backend == "gemini-flash"

    # No Gemini call is made when every page is sparse — the renderer is
    # the missing piece, not the API. This pins the "no heavy dep" promise.
    fake_client.models.generate_content.assert_not_called()


async def test_unsupported_mime_raises(mock_genai_client, tmp_path: Path) -> None:
    from ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    p = tmp_path / "audio.wav"
    p.write_bytes(b"RIFF...")

    extractor = GeminiFlashExtractor(api_key="fake-key")
    with pytest.raises(ValueError, match="unsupported mime"):
        await extractor.extract(p, "audio/wav")


async def test_supports_metadata(mock_genai_client) -> None:
    from ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    extractor = GeminiFlashExtractor(api_key="fake-key")
    assert extractor.name == "gemini-flash"
    assert "image/png" in extractor.supports_mimes
    assert "image/jpeg" in extractor.supports_mimes
    assert "application/pdf" in extractor.supports_mimes
    assert extractor.requires_network is True
