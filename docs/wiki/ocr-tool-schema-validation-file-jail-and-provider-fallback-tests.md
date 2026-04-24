---
{
  "title": "OCR Tool: Schema Validation, File Jail, and Provider Fallback Tests",
  "summary": "Tests for OCRTool, a built-in agent tool that extracts text from images using OpenAI Vision or Tesseract as fallback. Validates the tool schema, enforces a file-system jail to prevent path traversal, and covers error paths for unsupported formats, oversized files, missing providers, and API failures.",
  "concepts": [
    "OCRTool",
    "file jail",
    "path traversal prevention",
    "is_safe_path",
    "OpenAI Vision",
    "Tesseract",
    "tool schema",
    "trust level",
    "image processing",
    "error handling",
    "file size limit",
    "provider fallback"
  ],
  "categories": [
    "agent tools",
    "security",
    "OCR",
    "testing",
    "test"
  ],
  "source_docs": [
    "2303b72bed7a0844"
  ],
  "backlinks": null,
  "word_count": 559,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`OCRTool` gives PocketPaw agents the ability to read text from images — receipts, screenshots, scanned documents. Because it accepts a file path from the agent (which ultimately comes from user input), it carries significant security risk. The test suite is security-forward, explicitly testing path traversal prevention alongside normal functionality.

## Tool Schema (`TestOCRToolSchema`)

- `test_name`: The tool name is `"ocr"` — the exact string the LLM uses to invoke it. A name mismatch means the tool is never called.
- `test_trust_level`: The tool has `trust_level = "standard"`, meaning it can be called without elevated permissions. If it were accidentally set to `"owner"`, regular users could not use it.
- `test_parameters`: The schema declares `image_path` (required) and `prompt` (optional). `image_path` must be in `required` — without it the LLM might call the tool without providing a path, causing a crash.
- `test_description`: The description mentions "ocr" or "extract text" so the LLM can correctly decide when to use it.

## File Jail Enforcement

- `test_ocr_file_jail_rejects_outside_path`: Files outside the configured `file_jail_path` must be rejected with an error. Without this, an agent could be tricked into OCR-ing `/etc/passwd` or any sensitive file on the host. The jail path is set per-deployment and defaults to a sandboxed directory. The test passes a path like `/etc/shadow` and asserts the tool returns an error, not file contents.

The `is_safe_path` function is the enforcement point — it is patched in most other tests to return `True` so those tests can focus on other behaviors.

## Error Paths

- `test_ocr_file_not_found`: A path that does not exist returns a "file not found" error message to the agent rather than raising `FileNotFoundError`. The agent must receive actionable feedback, not a Python traceback.
- `test_ocr_unsupported_format`: Non-image files (e.g., `.pdf`, `.exe`) are rejected before being sent to the OCR provider, saving API cost and preventing unexpected behavior.
- `test_ocr_file_too_large`: Images above the size limit are rejected. OpenAI's Vision API has an undocumented practical limit; sending a 50MB image would either fail expensively or time out. Early rejection gives the user a clear error.
- `test_ocr_no_api_key_no_tesseract`: When neither `openai_api_key` is set nor Tesseract is installed, the tool returns a configuration error rather than crashing. This is the "no OCR provider available" path, common in minimal deployments.
- `test_ocr_api_error`: When the OpenAI API call fails (network error, quota exceeded), the error is surfaced as a message to the agent, not raised as an exception that would crash the tool loop.

## Success Path

- `test_ocr_openai_success`: With a valid image, API key, and mocked OpenAI Vision response, the tool returns the extracted text. The mock uses `AsyncMock` to simulate the async API call.
- `test_ocr_no_text_detected`: When the Vision API returns an empty or "no text found" response, the tool returns a clear "no text detected" message. Returning empty string would look like a bug to the agent.

## Fixture Design

`_mock_settings` patches both `get_settings` and `is_safe_path`, providing a controlled environment where the tool sees a valid API key and an approved path. Individual security tests bypass this fixture and set up their own conditions to test rejection paths.

## Known Gaps

No TODOs in this file. The test suite does not cover the Tesseract fallback path — `test_ocr_openai_success` only tests the OpenAI provider. Tesseract is the offline alternative for users without an OpenAI key, and its integration path is not exercised here.
