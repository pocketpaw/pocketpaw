---
{
  "title": "Knowledge Ingestion: Multi-Source Text Extraction to RawDocs",
  "summary": "The ingestion layer extracts clean text from plain text, URLs, PDFs, images (OCR), and DOCX files, producing `RawDoc` objects identified by a content hash. URL ingestion uses `trafilatura` as the primary extractor for its best-in-class boilerplate removal (F1=0.958), with a regex-based HTML strip as a fallback.",
  "concepts": [
    "RawDoc",
    "content hash",
    "trafilatura",
    "URL ingestion",
    "PDF extraction",
    "OCR",
    "pytesseract",
    "pdfplumber",
    "ingest_text",
    "ingest_url",
    "ingest_file",
    "boilerplate removal"
  ],
  "categories": [
    "knowledge",
    "ingestion",
    "document processing"
  ],
  "source_docs": [
    "9f2a2a6a1d6d9964"
  ],
  "backlinks": null,
  "word_count": 349,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/knowledge/ingest.py` is the entry point for content entering the knowledge engine. It handles the messy reality of real-world document formats and produces clean, uniform `RawDoc` objects that the compiler can work with.

## Content Hash Deduplication

```python
def _content_hash(text: str, source: str) -> str:
    return hashlib.sha256(f"{source}:{text[:1000]}".encode()).hexdigest()[:16]
```

Each `RawDoc` is identified by a hash of the source identifier and the first 1000 characters of content. This is a fast fingerprint — for most documents the opening content is distinctive enough, and hashing the full text of large PDFs would be slow.

## URL Ingestion with trafilatura

```python
async def ingest_url(url: str) -> RawDoc:
    # 1. Fetch HTML with httpx (follows redirects, 30s timeout)
    # 2. Try trafilatura for clean extraction
    # 3. Fall back to basic HTML strip if trafilatura not installed
```

`trafilatura` is chosen for its F1 score of 0.958 on the boilerplate removal benchmark — it reliably strips navigation, ads, and footers while preserving article content. The fallback (regex HTML stripping) is far less accurate but ensures the function never fails due to a missing dependency.

## File Type Detection

```python
async def ingest_file(file_path: Path) -> RawDoc:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        text = _extract_pdf(file_path)
    elif suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        text = _extract_image(file_path)
    elif suffix == ".docx":
        text = _extract_docx(file_path)
    else:
        text = file_path.read_text(errors="replace")
```

Detection is by file extension. The `errors="replace"` on the text fallback prevents `UnicodeDecodeError` from crashing ingestion on binary files that slip through the type check.

## Image OCR and PDF Extraction

`_extract_image()` uses Tesseract (via `pytesseract`) for OCR, enabling ingestion of screenshots and scanned documents. `_extract_pdf()` uses `pdfplumber` to extract text page by page. Both are synchronous functions.

## Known Gaps

- **No URL change detection**: Ingesting the same URL twice creates a new RawDoc if page content changed. There is no mechanism to detect which articles need recompilation.
- **No async PDF/image extraction**: `_extract_pdf` and `_extract_image` are synchronous and run on the async event loop, potentially blocking it for large files.
- **OCR quality depends on Tesseract version**: No version check is performed.