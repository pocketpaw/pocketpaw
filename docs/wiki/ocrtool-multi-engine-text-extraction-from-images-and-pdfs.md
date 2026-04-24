---
{
  "title": "OCRTool: Multi-Engine Text Extraction from Images and PDFs",
  "summary": "The `OCRTool` extracts text from images and PDFs using three interchangeable OCR backends — OpenAI Vision (GPT-4o), Sarvam Vision (specialized for 23 Indian languages), and Tesseract (offline fallback). All path operations go through `is_safe_path` to enforce the file jail, and outputs are saved to a dedicated OCR output directory so extracted text is recoverable after the tool call completes.",
  "concepts": [
    "OCRTool",
    "_ocr_openai",
    "_ocr_sarvam",
    "_ocr_tesseract",
    "OpenAI Vision",
    "Sarvam Vision",
    "Tesseract",
    "MIME types",
    "base64 encoding",
    "is_safe_path",
    "OCR output directory",
    "multi-engine",
    "BaseTool"
  ],
  "categories": [
    "builtin tools",
    "media processing",
    "OCR",
    "integrations"
  ],
  "source_docs": [
    "77beeeb5ea19fe31"
  ],
  "backlinks": null,
  "word_count": 544,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ocr.py` was created 2026-02-09 as part of Phase 4 Media Integrations. OCR unlocks document processing workflows: invoices, receipts, scanned contracts, and screenshots all become queryable text that the agent can reason about. The multi-engine design ensures the tool remains useful across different deployment contexts — cloud-only with OpenAI, Indian-language workflows with Sarvam, or fully offline with Tesseract.

## MIME type registry

```python
_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".pdf": "application/pdf",
}
```

The MIME type map is used when encoding images for the OpenAI Vision API and Sarvam API, which require the correct `Content-Type` or base64 encoding header. Without this map, the tool would need to infer MIME types from headers or rely on the OS MIME database, which is inconsistent across platforms.

## Three OCR backends

### _ocr_openai

Uses OpenAI's GPT-4o vision API. The image is base64-encoded and sent as a data URL in the vision message. This backend is the most capable for complex layouts (multi-column, tables, handwriting) but requires an OpenAI API key and sends image data to a third-party service.

### _ocr_sarvam

Uses Sarvam's vision API, which is specialized for 23 Indian languages (Devanagari, Tamil, Telugu, etc.) and accepts both images and PDFs. For Indian-language documents, Sarvam significantly outperforms generic vision models. This backend requires a Sarvam API key.

### _ocr_tesseract

Uses Tesseract, an open-source offline OCR engine. No API key required, no data leaves the machine. Quality is lower than the cloud backends for complex layouts, but it works in air-gapped environments and does not incur API costs. This is the fallback when no cloud API is configured.

## Path safety and output persistence

Input paths are validated with `is_safe_path` before reading, enforcing the `file_jail_path` boundary. This prevents the agent from OCR-ing sensitive files outside the permitted sandbox.

Extracted text is saved to `_get_ocr_output_dir()` — `get_config_dir() / "generated" / "ocr"` — in addition to being returned inline. This persistence matters for large documents where the extracted text might exceed practical inline return sizes, and for audit purposes.

```python
def _get_ocr_output_dir() -> Path:
    """Get/create the OCR output directory."""
    d = get_config_dir() / "generated" / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

## Trust level

`trust_level = "standard"`. OCR reads local files (path-safe) and optionally sends them to cloud APIs, but does not write to user-owned systems or take consequential actions. Standard trust is appropriate.

## Backend selection logic

The tool accepts an `engine` parameter (or equivalent) that lets the caller specify which backend to use. If not specified, the tool defaults to the first configured backend in priority order: OpenAI → Sarvam → Tesseract. This waterfall prevents silent failures when a preferred backend is unavailable.

## Known Gaps

- **No streaming for large PDFs**: Large PDFs are processed in a single API call. Multi-page PDFs can exceed API token or size limits.
- **No post-processing**: The raw extracted text is returned without layout normalization. Tables extracted from invoices may not preserve column alignment.
- **Tesseract language packs**: The Tesseract backend uses the default language pack installed on the system. Non-English documents will produce poor results unless the user has installed the appropriate Tesseract language pack manually — the tool does not verify or install language packs.