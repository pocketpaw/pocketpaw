# test_local.py — LocalExtractor parity tests.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Behavior parity check: LocalExtractor.extract(...).text must match the
# previous suffix-routed _extract_file output for the same input.
"""Tests for `ee.cloud.extraction.local.LocalExtractor`.

The LocalExtractor is a behavior-preserving port of the old `_extract_file`
helper from `agents/knowledge.py`. These tests pin that contract:

  - Text-file fallback returns the file body verbatim with errors='replace'.
  - PDF route uses pypdf's PdfReader; an empty-page PDF returns "".
  - DOCX route uses python-docx Document.paragraphs.
  - Image route delegates to pytesseract (mocked here).
  - Missing optional deps raise RuntimeError with install hints.
  - The result wraps the same text into ExtractionResult with backend="local".
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pypdf import PdfWriter

from ee.cloud.extraction import ExtractionResult
from ee.cloud.extraction.local import LocalExtractor


@pytest.fixture
def tmp_text_file(tmp_path: Path) -> Path:
    p = tmp_path / "notes.txt"
    p.write_text("hello local extractor\nsecond line\n", encoding="utf-8")
    return p


@pytest.fixture
def tmp_blank_pdf(tmp_path: Path) -> Path:
    """Blank single-page PDF — pypdf reads it back as an empty string."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    p = tmp_path / "blank.pdf"
    p.write_bytes(buf.getvalue())
    return p


async def test_text_file_fallback(tmp_text_file: Path) -> None:
    extractor = LocalExtractor()
    result = await extractor.extract(tmp_text_file, "text/plain")

    assert isinstance(result, ExtractionResult)
    assert result.text == "hello local extractor\nsecond line\n"
    assert result.backend == "local"
    assert result.captions == []
    assert result.metadata["path"] == str(tmp_text_file)
    assert result.metadata["mime"] == "text/plain"


async def test_text_file_with_invalid_utf8(tmp_path: Path) -> None:
    """errors='replace' contract: unreadable bytes do not raise."""
    p = tmp_path / "bad.txt"
    p.write_bytes(b"good text \xff\xfe trailing")
    extractor = LocalExtractor()

    result = await extractor.extract(p, "text/plain")

    # The U+FFFD replacement char appears for the bad bytes.
    assert "good text" in result.text
    assert "trailing" in result.text


async def test_pdf_blank_page(tmp_blank_pdf: Path) -> None:
    extractor = LocalExtractor()
    result = await extractor.extract(tmp_blank_pdf, "application/pdf")

    # Blank page yields empty extracted text — same as the prior _extract_file.
    assert result.text == ""
    assert result.backend == "local"


async def test_pdf_missing_pypdf_raises(tmp_path: Path) -> None:
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    extractor = LocalExtractor()

    # Force ImportError on `from pypdf import PdfReader`.
    with patch.dict(sys.modules, {"pypdf": None}):
        with pytest.raises(RuntimeError, match="pypdf not installed"):
            await extractor.extract(p, "application/pdf")


async def test_docx_path_uses_docx_paragraphs(tmp_path: Path) -> None:
    p = tmp_path / "doc.docx"
    p.write_bytes(b"placeholder")  # contents irrelevant — Document() is mocked

    fake_docx = MagicMock()
    fake_paragraph_a = MagicMock(text="first para")
    fake_paragraph_b = MagicMock(text="second para")
    fake_doc = MagicMock(paragraphs=[fake_paragraph_a, fake_paragraph_b])
    fake_docx.Document.return_value = fake_doc

    with patch.dict(sys.modules, {"docx": fake_docx}):
        result = await LocalExtractor().extract(p, "application/vnd.openxmlformats")

    fake_docx.Document.assert_called_once_with(str(p))
    assert result.text == "first para\nsecond para"
    assert result.backend == "local"


async def test_docx_missing_lib_raises(tmp_path: Path) -> None:
    p = tmp_path / "x.docx"
    p.write_bytes(b"placeholder")

    with patch.dict(sys.modules, {"docx": None}):
        with pytest.raises(RuntimeError, match="python-docx not installed"):
            await LocalExtractor().extract(p, "application/vnd.openxmlformats")


async def test_image_path_uses_pytesseract(tmp_path: Path) -> None:
    p = tmp_path / "diagram.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # bytes irrelevant — PIL/tesseract are mocked

    fake_pytesseract = MagicMock()
    fake_pytesseract.image_to_string.return_value = "ocr text from image"
    fake_pil = MagicMock()
    fake_image = MagicMock()
    fake_pil.Image.open.return_value = fake_image

    with patch.dict(sys.modules, {"pytesseract": fake_pytesseract, "PIL": fake_pil}):
        result = await LocalExtractor().extract(p, "image/png")

    fake_pil.Image.open.assert_called_once_with(str(p))
    fake_pytesseract.image_to_string.assert_called_once_with(fake_image)
    assert result.text == "ocr text from image"
    assert result.backend == "local"


async def test_image_missing_lib_raises(tmp_path: Path) -> None:
    p = tmp_path / "x.jpg"
    p.write_bytes(b"\xff\xd8\xff")  # JPEG magic

    with patch.dict(sys.modules, {"pytesseract": None, "PIL": None}):
        with pytest.raises(RuntimeError, match="pytesseract not installed"):
            await LocalExtractor().extract(p, "image/jpeg")


async def test_supports_wildcard_mime() -> None:
    extractor = LocalExtractor()
    assert "*" in extractor.supports_mimes
    assert extractor.requires_network is False
    assert extractor.name == "local"
