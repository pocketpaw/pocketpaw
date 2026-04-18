"""Knowledge ingestion — extract text from sources and store as RawDocs.

Supports: plain text, URLs, PDFs, images (OCR), docx, text-based files.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from pocketpaw.knowledge.models import RawDoc

logger = logging.getLogger(__name__)


def _content_hash(text: str, source: str) -> str:
    """Deterministic hash from content + source."""
    return hashlib.sha256(f"{source}:{text[:1000]}".encode()).hexdigest()[:16]


async def ingest_text(text: str, source: str = "manual") -> RawDoc:
    """Create a RawDoc from plain text."""
    return RawDoc(
        id=_content_hash(text, source),
        source_type="text",
        source=source,
        content_type="text",
        raw_text=text,
        metadata={"word_count": len(text.split())},
    )


async def ingest_url(url: str) -> RawDoc:
    """Fetch URL and extract clean content.

    Uses trafilatura (best-in-class HTML→text/markdown extraction, F1=0.958)
    with fallback to basic HTML stripping if not installed.
    """
    import httpx

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        raise ValueError(f"Failed to fetch {url}: {exc}") from exc

    # Try trafilatura first — strips boilerplate, preserves structure
    clean = ""
    try:
        import trafilatura

        clean = (
            trafilatura.extract(
                html, output_format="markdown", include_links=True, include_tables=True
            )
            or ""
        )
    except ImportError:
        pass

    # Fallback: basic HTML stripping
    if not clean:
        clean = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()

    if not clean:
        raise ValueError(f"No text content extracted from {url}")

    clean = clean[:100_000]

    return RawDoc(
        id=_content_hash(clean, url),
        source_type="url",
        source=url,
        content_type="html",
        raw_text=clean,
        metadata={"word_count": len(clean.split()), "url": url},
    )


async def ingest_file(file_path: str) -> RawDoc:
    """Auto-detect file type and extract text."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = path.suffix.lower()
    text = ""
    content_type = suffix.lstrip(".")

    # PDF
    if suffix == ".pdf":
        text = _extract_pdf(path)
        content_type = "pdf"

    # Images
    elif suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"):
        text = _extract_image(path)
        content_type = "image"

    # Word documents
    elif suffix == ".docx":
        text = _extract_docx(path)
        content_type = "docx"

    # Text-based files
    elif suffix in (
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".html",
        ".xml",
        ".log",
        ".py",
        ".js",
        ".ts",
    ):
        text = path.read_text(encoding="utf-8", errors="replace")
        content_type = suffix.lstrip(".")

    else:
        # Try reading as text
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raise ValueError(f"Unsupported file type: {suffix}")

    if not text.strip():
        raise ValueError(f"No text extracted from {path.name}")

    return RawDoc(
        id=_content_hash(text, file_path),
        source_type="file",
        source=file_path,
        filename=path.name,
        content_type=content_type,
        raw_text=text,
        metadata={"word_count": len(text.split()), "file_size": path.stat().st_size},
    )


# ── Extractors ──


def _extract_pdf(path: Path) -> str:
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        return "\n\n".join(p.extract_text() or "" for p in reader.pages)
    except ImportError:
        pass
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n\n".join(p.extract_text() or "" for p in pdf.pages)
    except ImportError:
        raise ImportError("Install pypdf or pdfplumber: pip install pypdf")


def _extract_image(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image

        return pytesseract.image_to_string(Image.open(str(path)))
    except ImportError:
        raise ImportError("Install Pillow + pytesseract: pip install Pillow pytesseract")


def _extract_docx(path: Path) -> str:
    try:
        import docx

        doc = docx.Document(str(path))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        raise ImportError("Install python-docx: pip install python-docx")
