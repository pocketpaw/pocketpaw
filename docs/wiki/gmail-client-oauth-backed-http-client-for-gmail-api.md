---
{
  "title": "Gmail Client — OAuth-Backed HTTP Client for Gmail API",
  "summary": "`GmailClient` provides async methods for searching messages, reading full message content, sending emails, and listing labels via the Gmail API, using OAuth bearer tokens from `OAuthManager`. It handles base64url-encoded message bodies and MIME multipart structures for plain text extraction.",
  "concepts": [
    "Gmail",
    "GmailClient",
    "Gmail API",
    "OAuth",
    "message search",
    "MIME",
    "base64url",
    "MIMEText",
    "send email",
    "list labels",
    "body extraction",
    "multipart messages"
  ],
  "categories": [
    "integrations",
    "Google Workspace"
  ],
  "source_docs": [
    "9b8eebcb3c71fcb4"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`gmail.py` implements `GmailClient`, PocketPaw's interface to the Gmail API. It uses the shared `OAuthManager` with service key `"google_gmail"`. The client focuses on the operations most useful to an AI agent: searching for messages, reading their content, sending replies, and understanding label/folder structure.

## search — Two-Phase Message Retrieval

Gmail's API splits message retrieval into two steps:

1. `GET /messages?q={query}` — returns a list of `{id, threadId}` objects matching the search query
2. `GET /messages/{id}?format=metadata` — fetches header metadata (From, Subject, Date) for each message

```python
# List message IDs
resp = await client.get(f"{_GMAIL_BASE}/messages", params={"q": query, "maxResults": max_results})
messages = resp.json().get("messages", [])

# Fetch metadata for each message
for msg in messages[:max_results]:
    resp = await client.get(f"{_GMAIL_BASE}/messages/{msg['id']}",
        params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
```

The `metadataHeaders` parameter limits the returned header fields, reducing response size. The snippet (short preview of message content) is included in the initial list response, so `search()` returns snippet + subject + from + date for each result — enough for "show me emails about X" use cases without fetching full bodies.

## get_message — Full Body Extraction

`get_message()` fetches a full message in `"full"` format, then calls `_extract_body()` to extract plain text from the MIME structure. Gmail messages can be simple (a single `text/plain` part) or deeply nested multipart messages. The extractor handles both:

```python
def _extract_body(self, payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    # Nested multipart
    for sub in payload.get("parts", []):
        if sub.get("mimeType") == "text/plain":
            data = sub.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return "(no text content)"
```

Gmail encodes message bodies in base64url format. `errors="replace"` handles malformed UTF-8 (common in old emails with encoding issues) by substituting the replacement character rather than raising an exception.

## send — MIME Encoding via Python Standard Library

```python
msg = MIMEText(body)
msg["to"] = to
msg["subject"] = subject
if reply_to_id:
    msg["In-Reply-To"] = reply_to_id
raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
```

Python's `email.mime.text.MIMEText` constructs a properly formatted MIME message. The entire message is base64url-encoded before submission to the Gmail API's `messages/send` endpoint. The `In-Reply-To` header threads the reply into the correct conversation.

## list_labels

Returns the user's Gmail labels (both system labels like INBOX, SENT and user-created labels), enabling an agent to understand the user's email organization structure before performing label-based searches.

## Known Gaps

- The `search()` method fetches metadata for each message in a sequential loop rather than in parallel. For `max_results=20`, this makes 20 sequential API calls, which is slow. Batch requests (Gmail supports `batchGet`) would be significantly faster.
- `_extract_body()` prefers `text/plain` over `text/html`. If a message has only an HTML body (common in marketing emails), the extractor returns `"(no text content)"`.
- There is no `get_thread()` method — conversations must be fetched message by message.
