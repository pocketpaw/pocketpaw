# local.py — Local extraction adapter (pypdf + python-docx + pytesseract).
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Wraps the existing _extract_file logic from agents/knowledge.py so
# self-hosted and offline deployments keep a no-network extraction path.
"""LocalExtractor — wraps the traditional extraction libraries.

Behavior must match the previous `_extract_file` in `agents/knowledge.py`
(line-for-line port). MIME routing here is suffix-based to mirror the
original helper, with the public `supports_mimes = {"*"}` so the chain
treats Local as a catch-all fallback.
"""

from __future__ import annotations

from pathlib import Path

from ee.cloud.extraction.adapter import ExtractionResult


class LocalExtractor:
    """Behavior-preserving wrapper around pypdf / python-docx / pytesseract."""

    name = "local"
    supports_mimes = {"*"}
    requires_network = False

    async def extract(self, path: Path, mime: str) -> ExtractionResult:
        text = await _extract_text(path)
        return ExtractionResult(
            text=text,
            metadata={"path": str(path), "mime": mime},
            backend=self.name,
        )


async def _extract_text(path: Path) -> str:
    """Extract text from PDF, DOCX, image, or fall back to raw read.

    Matches the previous `_extract_file` implementation byte-for-byte so the
    output of `LocalExtractor.extract(...).text` is identical to what callers
    used to get from `await _extract_file(file_path)`.
    """
    file_path = str(path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError as exc:
            raise RuntimeError("pypdf not installed — run: pip install pypdf") from exc

    if suffix in (".docx", ".doc"):
        try:
            from docx import Document

            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError as exc:
            raise RuntimeError(
                "python-docx not installed — run: pip install python-docx"
            ) from exc

    if suffix in (".png", ".jpg", ".jpeg"):
        try:
            import pytesseract
            from PIL import Image

            return pytesseract.image_to_string(Image.open(file_path))
        except ImportError as exc:
            raise RuntimeError(
                "pytesseract not installed — run: pip install pytesseract Pillow"
            ) from exc

    return path.read_text(encoding="utf-8", errors="replace")
