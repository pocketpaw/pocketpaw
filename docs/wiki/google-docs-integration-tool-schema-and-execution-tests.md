---
{
  "title": "Google Docs Integration Tool Schema and Execution Tests",
  "summary": "This test module validates the three Google Docs tools (`DocsReadTool`, `DocsCreateTool`, `DocsSearchTool`), the `_parse_doc_id` URL-parsing utility, and the `DocsClient._extract_text` method that converts the Docs API's nested JSON structure into plain text. It also confirms that each tool returns actionable error messages when OAuth authentication is absent.",
  "concepts": [
    "DocsReadTool",
    "DocsCreateTool",
    "DocsSearchTool",
    "Google Docs",
    "_parse_doc_id",
    "_extract_text",
    "DocsClient",
    "OAuth",
    "trust_level",
    "textRun",
    "inlineObjectElement",
    "httpx"
  ],
  "categories": [
    "testing",
    "Google integrations",
    "tools",
    "test"
  ],
  "source_docs": [
    "7dcfd0ac5b3650c9"
  ],
  "backlinks": null,
  "word_count": 475,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's Google Docs integration (Sprint 26) exposes three agent tools:

- **`DocsReadTool`** — reads the full text of a document given its ID or URL.
- **`DocsCreateTool`** — creates a new document with a title and optional content.
- **`DocsSearchTool`** — searches Drive for documents matching a query and returns their IDs and links.

## Tool Schema Tests (TestDocsToolSchemas)

Each tool is instantiated and its `name`, `trust_level`, and `parameters` dict are inspected:

- `DocsReadTool` — name `"docs_read"`, trust `"high"`, requires `document_id`.
- `DocsCreateTool` — name `"docs_create"`, requires both `title` (in `required`) and `content`.
- `DocsSearchTool` — name `"docs_search"`, accepts `query` parameter.

Schema tests serve as a contract. If a developer renames a parameter, the downstream agent prompts that reference the old name will silently break — schema tests catch this before it reaches users.

## Document ID Parsing (TestDocIdParsing)

The `_parse_doc_id` utility handles two input forms:

```python
def test_plain_id(self):
    assert _parse_doc_id("abc123xyz") == "abc123xyz"

def test_full_url(self):
    url = "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/edit"
    assert _parse_doc_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

def test_url_with_fragment(self):
    url = "https://docs.google.com/document/d/abc_123-XYZ/edit#heading=h.1"
    assert _parse_doc_id(url) == "abc_123-XYZ"
```

Users typically copy document IDs from the browser URL bar, which includes `edit`, `view`, and `#heading` fragment suffixes. Without this parser, the raw URL would be passed to the Docs API and cause a 404. The fragment test is particularly important because Python's `urlparse` does not strip fragments by default.

## Text Extraction (TestDocsTextExtraction)

The Google Docs API returns documents as a deeply nested JSON structure where text lives inside `body.content[].paragraph.elements[].textRun.content`. `DocsClient._extract_text` flattens this into a plain string.

Four tests cover edge cases:

- **Simple text** — a single `textRun` produces the stripped string.
- **Multiple paragraphs** — text from both paragraphs appears in the result.
- **Empty doc** — `body.content: []` returns an empty string rather than raising `KeyError`.
- **No text run** — an `inlineObjectElement` (image placeholder) is silently skipped, returning an empty string.

The `inlineObjectElement` case is important because Docs with embedded images would crash an implementation that unconditionally accesses `element["textRun"]`.

## Auth Error Tests

All three tools are tested with `_get_token` mocked to raise `RuntimeError("Not authenticated")`:

```python
async def test_docs_read_no_auth():
    result = await tool.execute(document_id="abc123")
    assert result.startswith("Error:")
    assert "authenticated" in result.lower()
```

Returning a string that starts with `"Error:"` allows the agent to relay the message verbatim, giving the user a clear instruction to authenticate.

## Success Path Test (test_docs_search_success)

One happy-path test mocks `_get_token` to return a fake token, mocks `httpx.AsyncClient` to return a file list, and verifies the output contains the document name and ID. This test uses the async context manager protocol on the mock client (`__aenter__`, `__aexit__`) which is required for `async with httpx.AsyncClient() as client:` to work correctly in tests.

## Known Gaps

No tests cover `DocsCreateTool` or `DocsReadTool` in the success path. There are no tests for rate limiting, large document truncation, or documents shared via service account rather than user OAuth.