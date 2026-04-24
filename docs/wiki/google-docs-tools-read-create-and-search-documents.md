---
{
  "title": "Google Docs Tools: Read, Create, and Search Documents",
  "summary": "The `gdocs.py` module provides three `BaseTool` subclasses — `DocsReadTool`, `DocsCreateTool`, and `DocsSearchTool` — that allow the PocketPaw agent to read, create, and search Google Docs documents via the Google Docs API. A helper function `_parse_doc_id` normalizes both raw document IDs and full Google Docs URLs so the agent does not need to extract the ID manually.",
  "concepts": [
    "DocsReadTool",
    "DocsCreateTool",
    "DocsSearchTool",
    "_parse_doc_id",
    "Google Docs API",
    "document ID normalization",
    "trust level",
    "BaseTool",
    "Google OAuth",
    "Phase 4 Media Integrations"
  ],
  "categories": [
    "builtin tools",
    "Google Workspace",
    "document management",
    "integrations"
  ],
  "source_docs": [
    "cff19306c06bd4e3"
  ],
  "backlinks": null,
  "word_count": 545,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`gdocs.py` was created 2026-02-09 as part of the Phase 4 Media Integrations sprint. It gives the agent read and write access to Google Docs, enabling workflows like drafting a document from agent output, reading an existing doc to answer questions about it, or finding docs by name.

All three tools carry `trust_level = "high"` because they access third-party cloud storage containing potentially sensitive user data.

## URL normalization with _parse_doc_id

Google Docs URLs follow the pattern `https://docs.google.com/document/d/<ID>/edit`. Users naturally paste full URLs rather than just the document ID. Without `_parse_doc_id`, every tool would need its own URL-parsing logic or would silently fail when given a URL.

```python
_DOC_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")

def _parse_doc_id(doc_id_or_url: str) -> str:
    """Extract document ID from a URL or return as-is if already an ID."""
    match = _DOC_ID_RE.search(doc_id_or_url)
    if match:
        return match.group(1)
    return doc_id_or_url.strip()
```

The regex uses `search` (not `match`) so it works on any substring of the URL. The `.strip()` on the fallback path handles clipboard artifacts where users accidentally include a trailing space.

## DocsReadTool

Tool name: `docs_read`. Accepts a document ID or URL, resolves the ID, fetches the document via the Google Docs API, and returns its content as plain text. Returning plain text (rather than the raw JSON API response) keeps the output within the agent's context window and avoids exposing the full Docs structural format, which the agent does not need for most read tasks.

## DocsCreateTool

Tool name: `docs_create`. Creates a new Google Doc with a given title and optional initial content. Returns the new document's ID and URL so the agent can share it or follow up with edits. The create-then-write pattern (create an empty doc, then insert content) mirrors the Docs API's own model.

## DocsSearchTool

Tool name: `docs_search`. Searches the user's Google Drive for documents by name query, returning up to `max_results` (default 10) matches. The search is name-based rather than full-text because the Drive API's full-text search has quota implications; name search is faster and sufficient for the common case of "find the doc called 'Q1 Report'".

## Trust level rationale

All three tools are marked `trust_level = "high"`. This places them in a tier that requires the user to have explicitly granted Google OAuth permissions before the tool will run. The `BaseTool` protocol uses trust level to gate tool availability — a pocket without Google credentials configured will refuse to register these tools, preventing confusing "unauthorized" errors at execution time.

## Integration pattern

```python
class DocsReadTool(BaseTool):
    @property
    def trust_level(self) -> str:
        return "high"

    async def execute(self, document_id: str) -> str:
        doc_id = _parse_doc_id(document_id)
        # fetch via Google Docs API connector
```

The actual API call is delegated to a Google connector registered in the PocketPaw connector registry, keeping HTTP logic out of the tool layer.

## Known Gaps

- **No edit/update tool**: There is no `DocsEditTool`. Once a document is created, the agent can only read it — not append to or modify it. This limits document-creation workflows to one-shot drafts.
- **Plain-text only**: `DocsReadTool` strips formatting. Tables, inline images, and comments are lost. For documents where structure matters (e.g., a contract with a specific table layout), the plain-text output may be misleading.
- **No pagination**: `DocsSearchTool` returns at most `max_results` documents with no cursor for subsequent pages.