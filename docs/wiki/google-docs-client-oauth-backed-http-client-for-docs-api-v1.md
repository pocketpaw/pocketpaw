---
{
  "title": "Google Docs Client — OAuth-Backed HTTP Client for Docs API v1",
  "summary": "`DocsClient` provides async methods for reading Google Docs as plain text and creating new documents with initial content, using OAuth bearer tokens from `OAuthManager`. It includes a recursive text extraction helper that walks the Docs API's nested paragraph structure.",
  "concepts": [
    "Google Docs",
    "DocsClient",
    "OAuth",
    "Docs API v1",
    "text extraction",
    "batchUpdate",
    "insertText",
    "create document",
    "Drive integration",
    "async HTTP client",
    "paragraph traversal"
  ],
  "categories": [
    "integrations",
    "Google Workspace"
  ],
  "source_docs": [
    "de4fa3c8452df4e4"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`gdocs.py` implements `DocsClient`, PocketPaw's interface to the Google Docs API v1. It follows the same OAuth-backed pattern as `GmailClient` and `CalendarClient`, using the shared `OAuthManager` with service key `"google_docs"`.

## get_document — Reading Docs as Plain Text

```python
async def get_document(self, document_id: str) -> dict[str, Any]:
    token = await self._get_token()
    resp = await client.get(f"{_DOCS_BASE}/{document_id}", headers={"Authorization": f"Bearer {token}"})
    doc = resp.json()
    title = doc.get("title", "Untitled")
    body_text = self._extract_text(doc)
    return {"title": title, "body": body_text, "document_id": document_id}
```

The Google Docs API returns a deeply nested JSON structure. A document body contains `content` (list of structural elements), each of which contains `paragraph` objects, which contain `elements`, which contain `textRun` objects with the actual text. `_extract_text()` walks this tree:

```python
def _extract_text(self, doc: dict) -> str:
    parts: list[str] = []
    body = doc.get("body", {})
    for element in body.get("content", []):
        paragraph = element.get("paragraph", {})
        for pe in paragraph.get("elements", []):
            text_run = pe.get("textRun", {})
            content = text_run.get("content", "")
            if content:
                parts.append(content)
    return "".join(parts).strip()
```

This extracts only text runs, discarding images, tables, headers/footers, and inline objects. For the use case of "read this doc and summarize it," plain text extraction is the right trade-off — the LLM does not need formatting metadata.

## create_document — Two-Step Creation

Creating a Google Doc with content requires two API calls because the Docs API does not support creating a document with initial text in a single request:

1. `POST /documents` with `{"title": title}` — creates an empty document and returns the `documentId`.
2. `POST /documents/{id}/batchUpdate` with an `insertText` request — inserts the initial content at index 1 (the beginning of the document body).

```python
# Step 1: Create empty doc
resp = await client.post(_DOCS_BASE, json={"title": title}, headers=...)
doc_id = resp.json()["documentId"]

# Step 2: Insert content if provided
if content:
    resp = await client.post(f"{_DOCS_BASE}/{doc_id}:batchUpdate",
        json={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        headers=...)
```

The `batchUpdate` endpoint accepts a list of edit operations. Using `index: 1` inserts at the start of the document (index 0 is before the first paragraph marker, which is reserved).

## create_document_in_folder — Drive Integration

A third method creates a document and then moves it to a specific Drive folder using the Drive API's file metadata update endpoint. This requires two different base URLs: `_DOCS_BASE` for document creation and `_DRIVE_BASE` for the folder move.

## Known Gaps

- `_extract_text()` does not handle tables, headers, footers, or lists specially — all text runs are concatenated in document order, which can produce run-on text for table-heavy documents.
- There is no `update_document` method — existing documents can only be read, not modified via this client.
- The two-step `create_document` flow is not atomic: if the `batchUpdate` call fails after the empty document is created, an empty document with the given title is left in the user's Drive with no cleanup.
