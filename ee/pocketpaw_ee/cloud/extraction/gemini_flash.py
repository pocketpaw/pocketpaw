# gemini_flash.py — Gemini Flash extraction adapter.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Updated: 2026-07-03 (FL-15) — implement the deferred image-heavy PDF path:
#   sparse pages (pypdf text < _SPARSE_PAGE_THRESHOLD) are now RENDERED to a
#   PNG via PyMuPDF (fitz, lazy-imported like the genai client) and captioned
#   through the same Gemini call the image path uses. A per-PDF cost cap
#   (_MAX_SPARSE_PAGES_CAPTIONED) bounds render+caption calls. Text-heavy
#   pages stay pypdf-only (no Gemini call); the genai call was factored into a
#   shared _caption_image_bytes helper reused by both the image and PDF paths.
"""GeminiFlashExtractor — google-genai SDK adapter.

Captions images with `gemini-2.5-flash` (or whatever model is configured).
For PDFs the strategy is hybrid: pypdf extracts text per page. Pages with
>=200 chars of extracted text keep their pypdf text verbatim and are *not*
sent to Gemini. Pages with <200 chars are treated as image-heavy: the page
is rendered to a PNG with PyMuPDF and captioned via the same Gemini call the
image path uses, so scanned/diagram pages become BM25-searchable instead of
being silently skipped.

A cost guard caps the number of sparse pages captioned per PDF at
`_MAX_SPARSE_PAGES_CAPTIONED` — pages beyond the cap fall back to the old
"image-heavy, no caption" marker so a huge scanned PDF can't fan out into an
unbounded number of Gemini calls.

Heavy deps (google-genai, PyMuPDF) are lazy-imported so the module still
imports where they aren't installed (tests mock them). When network is
unavailable the chain falls through to LocalExtractor before this adapter is
asked to run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

CAPTION_PROMPT = (
    "Describe this image for a knowledge base. Cover: subject matter, "
    "any visible text verbatim, the structure if it's a diagram (boxes, "
    "arrows, labels), colors only if semantically meaningful. Output "
    "100-300 words. No preamble."
)

# Below this character count per pypdf-extracted page we treat the page
# as image-heavy and render+caption it. 200 is a heuristic; tune later.
_SPARSE_PAGE_THRESHOLD = 200

# Cost guard: cap how many sparse pages we render+caption per PDF. A scanned
# 500-page document would otherwise fan out into 500 Gemini calls. Pages past
# the cap keep the "image-heavy, no caption" marker.
_MAX_SPARSE_PAGES_CAPTIONED = 20

# DPI for rasterizing a sparse PDF page before captioning. 150 balances legible
# text/diagrams against payload size.
_RENDER_DPI = 150


def _render_pdf_page_to_png(path: Path, page_index: int, *, dpi: int = _RENDER_DPI) -> bytes:
    """Render a single PDF page (0-based) to PNG bytes via PyMuPDF (fitz).

    Lazy-imported so the module imports without the dep installed; tests patch
    this function or the `fitz` module to avoid a real render.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - exercised via mock in tests
        raise RuntimeError(
            "PyMuPDF not installed — run: pip install 'pocketpaw-ee[extraction]'"
        ) from exc

    doc = fitz.open(str(path))
    try:
        page = doc.load_page(page_index)
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()


class GeminiFlashExtractor:
    """Cloud-backed image and PDF-page captioning via google-genai."""

    name = "gemini-flash"
    supports_mimes = {"image/png", "image/jpeg", "image/jpg", "application/pdf"}
    requires_network = True

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        # Lazy-import the SDK so the adapter file can be imported in
        # environments where google-genai isn't installed (tests mock the
        # client with patch.dict on sys.modules).
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def extract(self, path: Path, mime: str) -> ExtractionResult:
        if mime.startswith("image/"):
            return await self._extract_image(path, mime)
        if mime == "application/pdf":
            return await self._extract_pdf_with_captions(path)
        raise ValueError(f"unsupported mime: {mime}")

    async def _caption_image_bytes(self, data: bytes, mime: str) -> str:
        """Send raw image bytes to Gemini and return the stripped caption.

        Shared by the image path and the rendered-PDF-page path so both use an
        identical request shape (model, prompt, inline Part).
        """
        from google.genai import types

        contents: list = [
            CAPTION_PROMPT,
            types.Part.from_bytes(data=data, mime_type=mime),
        ]
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model,
            contents=contents,
        )
        return (response.text or "").strip()

    async def _extract_image(self, path: Path, mime: str) -> ExtractionResult:
        caption = await self._caption_image_bytes(path.read_bytes(), mime)
        return ExtractionResult(
            text=caption,
            captions=[caption],
            metadata={"path": str(path), "mime": mime, "model": self._model},
            backend=self.name,
        )

    async def _extract_pdf_with_captions(self, path: Path) -> ExtractionResult:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("pypdf not installed — run: pip install pypdf") from exc

        reader = PdfReader(str(path))
        sections: list[str] = []
        captions: list[str] = []
        sparse_pages: list[int] = []
        captioned_pages: list[int] = []

        for idx, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if len(page_text) >= _SPARSE_PAGE_THRESHOLD:
                # Text-heavy page: keep pypdf text verbatim, no Gemini call.
                sections.append(f"[page {idx}]\n{page_text}")
                continue

            # Image-heavy page: render + caption, respecting the per-PDF cap.
            sparse_pages.append(idx)
            if len(captioned_pages) >= _MAX_SPARSE_PAGES_CAPTIONED:
                # Cost guard tripped — mark the gap instead of captioning.
                sections.append(f"[page {idx}: image-heavy, caption cap reached]")
                continue

            try:
                png = await asyncio.to_thread(_render_pdf_page_to_png, path, idx - 1)
                caption = await self._caption_image_bytes(png, "image/png")
            except Exception:  # noqa: BLE001 - render/caption failure must not sink the PDF
                sections.append(f"[page {idx}: image-heavy, caption failed]")
                continue

            if caption:
                sections.append(f"[page {idx} — image caption]\n{caption}")
                captions.append(caption)
                captioned_pages.append(idx)
            else:
                sections.append(f"[page {idx}: image-heavy, empty caption]")

        text = "\n\n".join(sections)
        return ExtractionResult(
            text=text,
            captions=captions,
            metadata={
                "path": str(path),
                "mime": "application/pdf",
                "model": self._model,
                "page_count": len(reader.pages),
                "sparse_pages": sparse_pages,
                "captioned_pages": captioned_pages,
                "max_captioned_pages": _MAX_SPARSE_PAGES_CAPTIONED,
            },
            backend=self.name,
        )
