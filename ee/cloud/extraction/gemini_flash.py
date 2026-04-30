# gemini_flash.py — Gemini Flash extraction adapter.
# Created: 2026-04-30 — Phase 1 of "Files as Knowledge" plan, Stage 1.A.
# Cloud captioning sibling to LocalExtractor: rich descriptions for images,
# per-page captions for sparse PDF pages. No replacement of pypdf — it
# augments where pypdf returns near-empty text.
"""GeminiFlashExtractor — google-genai SDK adapter.

Captions images with `gemini-2.5-flash` (or whatever model is configured).
For PDFs the strategy is hybrid: pypdf extracts text per page; pages with
<200 chars of extracted text are marked image-heavy and *not* captioned in
this stage (heavy PDF→image rendering deferred to a later PR — Stage 1.A
ships without a new dep). When network is unavailable the chain falls
through to LocalExtractor before this adapter is asked to run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ee.cloud.extraction.adapter import ExtractionResult

CAPTION_PROMPT = (
    "Describe this image for a knowledge base. Cover: subject matter, "
    "any visible text verbatim, the structure if it's a diagram (boxes, "
    "arrows, labels), colors only if semantically meaningful. Output "
    "100-300 words. No preamble."
)

# Below this character count per pypdf-extracted page we treat the page
# as image-heavy and skip captioning. 200 is a heuristic; tune later.
_SPARSE_PAGE_THRESHOLD = 200


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

    async def _extract_image(self, path: Path, mime: str) -> ExtractionResult:
        from google.genai import types

        img = path.read_bytes()
        contents: list = [
            CAPTION_PROMPT,
            types.Part.from_bytes(data=img, mime_type=mime),
        ]
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model,
            contents=contents,
        )
        caption = (response.text or "").strip()
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

        for idx, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if len(page_text) < _SPARSE_PAGE_THRESHOLD:
                # Stage 1.A doesn't ship a PDF-to-image renderer dependency.
                # Mark the page so downstream search knows the gap exists.
                sections.append(f"[page {idx}: image-heavy, no caption]")
                sparse_pages.append(idx)
                continue
            sections.append(f"[page {idx}]\n{page_text}")

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
            },
            backend=self.name,
        )
