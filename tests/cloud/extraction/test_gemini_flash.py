# test_gemini_flash.py — GeminiFlashExtractor tests.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Updated: 2026-07-03 (FL-15) — cover the now-implemented image-heavy PDF path:
#   sparse pages render to PNG (renderer mocked) and get captioned via Gemini;
#   text-heavy pages still make no Gemini call; the per-PDF caption cap is
#   enforced. No real network / render — google.genai.Client and the page
#   renderer are both mocked.
# Asserts request structure (model, MIME on Part, image bytes) plus PDF
# strategy (pypdf text per page, render+caption for image-heavy pages, cap).
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
from pocketpaw_ee.cloud.extraction import ExtractionResult
from pypdf import PdfWriter


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


@pytest.fixture
def mock_renderer(monkeypatch: pytest.MonkeyPatch):
    """Patch the PDF-page renderer so sparse-page tests need no PyMuPDF.

    Returns the MagicMock standing in for _render_pdf_page_to_png; it yields
    deterministic PNG bytes so we never touch fitz or a real rasterizer.
    """
    from pocketpaw_ee.cloud.extraction import gemini_flash

    fake_render = MagicMock(return_value=b"\x89PNG\r\n\x1a\nrendered-page")
    monkeypatch.setattr(gemini_flash, "_render_pdf_page_to_png", fake_render)
    return fake_render


def _multipage_pdf(tmp_path: Path, n_pages: int) -> Path:
    """Write an n-page all-blank (sparse) PDF for cap/count assertions."""
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    p = tmp_path / f"blank-{n_pages}.pdf"
    p.write_bytes(buf.getvalue())
    return p


async def test_image_extract_calls_gemini_with_inline_bytes(
    mock_genai_client, tmp_image: Path
) -> None:
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

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


async def test_image_extract_uses_configured_model(mock_genai_client, tmp_image: Path) -> None:
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    fake_client, _ = mock_genai_client
    extractor = GeminiFlashExtractor(api_key="fake-key", model="custom-model")
    await extractor.extract(tmp_image, "image/png")

    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == "custom-model"


async def test_image_extract_handles_empty_response(mock_genai_client, tmp_image: Path) -> None:
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    _, fake_response = mock_genai_client
    fake_response.text = None

    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(tmp_image, "image/png")

    assert result.text == ""
    assert result.captions == [""]


async def test_pdf_sparse_page_is_rendered_and_captioned(
    mock_genai_client, mock_renderer, tmp_blank_pdf: Path
) -> None:
    """FL-15: an image-heavy (sparse) page is rendered to PNG and captioned."""
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    fake_client, fake_response = mock_genai_client
    fake_response.text = "A scanned invoice showing line items."

    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(tmp_blank_pdf, "application/pdf")

    # The sparse page now yields a non-empty caption, not a "no caption" marker.
    assert "A scanned invoice showing line items." in result.text
    assert result.captions == ["A scanned invoice showing line items."]
    assert result.metadata["sparse_pages"] == [1]
    assert result.metadata["captioned_pages"] == [1]
    assert result.metadata["page_count"] == 1
    assert result.backend == "gemini-flash"

    # Renderer was called for the (0-based) sparse page; Gemini was invoked with
    # image/png bytes from the render.
    mock_renderer.assert_called_once()
    assert mock_renderer.call_args.args[1] == 0  # page_index (0-based)
    fake_client.models.generate_content.assert_called_once()
    contents = fake_client.models.generate_content.call_args.kwargs["contents"]
    assert getattr(contents[1].inline_data, "mime_type", None) == "image/png"


async def test_pdf_text_heavy_page_makes_no_gemini_call(
    mock_genai_client, mock_renderer, tmp_blank_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page with plenty of extractable text keeps pypdf text, no captioning."""
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor
    from pypdf import PageObject

    # Force the single page to report dense text (> threshold).
    dense = "Lorem ipsum dolor sit amet. " * 40
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *a, **k: dense)

    fake_client, _ = mock_genai_client
    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(tmp_blank_pdf, "application/pdf")

    assert "[page 1]" in result.text
    assert "Lorem ipsum" in result.text
    assert result.metadata["sparse_pages"] == []
    assert result.metadata["captioned_pages"] == []
    # No render, no Gemini call for text-heavy pages.
    mock_renderer.assert_not_called()
    fake_client.models.generate_content.assert_not_called()


async def test_pdf_sparse_page_cap_is_enforced(
    mock_genai_client, mock_renderer, tmp_path: Path
) -> None:
    """FL-15 cost guard: only _MAX_SPARSE_PAGES_CAPTIONED pages get captioned."""
    from pocketpaw_ee.cloud.extraction import gemini_flash
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    cap = gemini_flash._MAX_SPARSE_PAGES_CAPTIONED
    over = cap + 3
    pdf = _multipage_pdf(tmp_path, over)

    fake_client, fake_response = mock_genai_client
    fake_response.text = "caption text"

    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(pdf, "application/pdf")

    # All pages are sparse, but only `cap` were captioned; the rest are marked.
    assert len(result.metadata["sparse_pages"]) == over
    assert len(result.metadata["captioned_pages"]) == cap
    assert result.metadata["max_captioned_pages"] == cap
    assert "caption cap reached" in result.text
    # Render + Gemini were each called exactly `cap` times, not `over` times.
    assert mock_renderer.call_count == cap
    assert fake_client.models.generate_content.call_count == cap


async def test_pdf_render_failure_degrades_gracefully(
    mock_genai_client, monkeypatch: pytest.MonkeyPatch, tmp_blank_pdf: Path
) -> None:
    """A render error on a sparse page must not sink the whole PDF."""
    from pocketpaw_ee.cloud.extraction import gemini_flash
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    def _boom(*a, **k):
        raise RuntimeError("PyMuPDF not installed")

    monkeypatch.setattr(gemini_flash, "_render_pdf_page_to_png", _boom)

    fake_client, _ = mock_genai_client
    extractor = GeminiFlashExtractor(api_key="fake-key")
    result = await extractor.extract(tmp_blank_pdf, "application/pdf")

    assert "[page 1: image-heavy, caption failed]" in result.text
    assert result.metadata["sparse_pages"] == [1]
    assert result.metadata["captioned_pages"] == []
    # Gemini is never reached when the render fails.
    fake_client.models.generate_content.assert_not_called()


async def test_unsupported_mime_raises(mock_genai_client, tmp_path: Path) -> None:
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    p = tmp_path / "audio.wav"
    p.write_bytes(b"RIFF...")

    extractor = GeminiFlashExtractor(api_key="fake-key")
    with pytest.raises(ValueError, match="unsupported mime"):
        await extractor.extract(p, "audio/wav")


async def test_supports_metadata(mock_genai_client) -> None:
    from pocketpaw_ee.cloud.extraction.gemini_flash import GeminiFlashExtractor

    extractor = GeminiFlashExtractor(api_key="fake-key")
    assert extractor.name == "gemini-flash"
    assert "image/png" in extractor.supports_mimes
    assert "image/jpeg" in extractor.supports_mimes
    assert "application/pdf" in extractor.supports_mimes
    assert extractor.requires_network is True
