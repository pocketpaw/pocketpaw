---
{
  "title": "Gmail Integration Body Extraction and Tool Tests",
  "summary": "This test module validates `GmailClient._extract_body`, which parses Gmail's MIME structure to extract plain text from messages, and the three Gmail tools (`GmailSearchTool`, `GmailReadTool`, `GmailSendTool`) — checking their schemas and verifying that each returns a user-readable error when OAuth authentication is absent.",
  "concepts": [
    "GmailSearchTool",
    "GmailReadTool",
    "GmailSendTool",
    "GmailClient",
    "_extract_body",
    "MIME parsing",
    "multipart",
    "base64",
    "OAuth",
    "trust_level",
    "Gmail API"
  ],
  "categories": [
    "testing",
    "Google integrations",
    "tools",
    "test"
  ],
  "source_docs": [
    "99cf5303da67e9ee"
  ],
  "backlinks": null,
  "word_count": 454,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's Gmail integration provides three agent tools:

- **`GmailSearchTool`** — searches the user's inbox using Gmail's query syntax.
- **`GmailReadTool`** — fetches the full content of a specific message by ID.
- **`GmailSendTool`** — composes and sends an email.

## MIME Body Extraction (TestExtractBody)

Gmail's API returns email bodies in MIME format, which can be nested arbitrarily: a `multipart/mixed` envelope may contain a `multipart/alternative` part with both `text/plain` and `text/html` variants, themselves encoded in URL-safe base64. `GmailClient._extract_body` navigates this structure and returns the first `text/plain` content it finds.

Four tests cover the parsing:

**`test_plain_text_direct`** — the simplest case: `mimeType: "text/plain"` with base64-encoded data. The test constructs the payload manually using `base64.urlsafe_b64encode`:

```python
payload = {
    "mimeType": "text/plain",
    "body": {"data": base64.urlsafe_b64encode(b"Hello world").decode()},
}
assert GmailClient._extract_body(payload) == "Hello world"
```

**`test_multipart`** — a `multipart/alternative` message with both plain and HTML parts. The method must prefer `text/plain` and ignore `text/html`. This matters because HTML bodies often contain tracking pixels, inline CSS, and escaped entities that would be noise for an AI reading the message.

**`test_no_text_content`** — a `multipart/mixed` with empty `parts` returns `"(no text content)"` rather than an empty string. This sentinel value lets the agent report to the user that the email exists but has no readable text body (e.g., it is image-only).

**`test_nested_multipart`** — a `multipart/mixed` wrapping a `multipart/alternative` wrapping a `text/plain`. The method must recurse into nested parts. Without recursion, real-world emails with attachments (which are `multipart/mixed`) would always return `"(no text content)"`.

## Tool Schema Tests (TestToolDefinitions)

```python
def test_gmail_search_tool(self):
    tool = GmailSearchTool()
    assert tool.name == "gmail_search"
    assert tool.trust_level == "high"
    assert "query" in tool.parameters["properties"]
```

All three tools have `trust_level == "high"` because email access is highly sensitive. `GmailSendTool` requires `to`, `subject`, and `body` parameters — the test verifies all three are present in `properties`.

## Auth Error Tests

All three tools are tested with `GmailClient._get_token` mocked to raise `RuntimeError("Not authenticated")`:

- `test_gmail_search_no_auth` — result contains `"Error"` and `"authenticated"` (case-insensitive).
- `test_gmail_read_no_auth` — result contains `"Error"`.
- `test_gmail_send_no_auth` — result contains `"Error"`.

The `gmail_search` test additionally checks for `"authenticated"` in the message to confirm the error is actionable, not just a generic failure.

## Why MIME Parsing Is Tested Separately

`_extract_body` is a pure function with no network dependency, making it easy to unit test with synthetic payloads. Testing it separately from the full tool flow means MIME parsing bugs are caught and diagnosed faster — a failure here immediately localizes the problem to the parsing logic rather than the auth or HTTP layer.

## Known Gaps

No tests cover the happy path for any tool. There are no tests for Gmail's `labelIds` filtering, pagination through search results, or the `MIME-Version` and `Content-Transfer-Encoding` headers that real drafts require for `GmailSendTool`.