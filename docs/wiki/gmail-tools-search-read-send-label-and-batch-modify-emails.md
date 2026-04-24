---
{
  "title": "Gmail Tools: Search, Read, Send, Label, and Batch Modify Emails",
  "summary": "The `gmail.py` module provides eight `BaseTool` subclasses covering the full Gmail management surface: `GmailSearchTool`, `GmailReadTool`, `GmailSendTool`, `GmailListLabelsTool`, `GmailCreateLabelTool`, `GmailModifyTool`, `GmailTrashTool`, and `GmailBatchModifyTool`. All tools carry `trust_level = \"high\"` because email access is sensitive and requires explicit OAuth authorization; the batch modify tool exists specifically to reduce API round-trips when applying label changes to many messages.",
  "concepts": [
    "GmailSearchTool",
    "GmailReadTool",
    "GmailSendTool",
    "GmailListLabelsTool",
    "GmailCreateLabelTool",
    "GmailModifyTool",
    "GmailTrashTool",
    "GmailBatchModifyTool",
    "Gmail API",
    "label management",
    "batch operations",
    "trust level",
    "BaseTool"
  ],
  "categories": [
    "builtin tools",
    "email",
    "Google Workspace",
    "integrations"
  ],
  "source_docs": [
    "f28203e52fd95d99"
  ],
  "backlinks": null,
  "word_count": 562,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`gmail.py` was created 2026-02-07 as part of Phase 2 Integration Ecosystem. Email is one of the highest-value integration surfaces for a business agent — scheduling, invoicing, customer communication, and notification workflows all flow through it. The module provides both individual-message operations and batch operations to handle inbox management at scale.

All eight tools carry `trust_level = "high"` because email contains sensitive personal and business data and requires the user to explicitly authorize Google OAuth access.

## GmailSearchTool

Tool name: `gmail_search`. Searches the authenticated Gmail inbox using Gmail's native query syntax (e.g., `from:supplier@example.com`, `subject:invoice`, `has:attachment`). The `max_results` parameter (default 10) caps the response size. Like `DriveListTool`, the description includes query syntax guidance to prevent the LLM from passing natural-language queries to a structured search field.

## GmailReadTool

Tool name: `gmail_read`. Fetches a specific message by its Gmail message ID and returns it as structured text (headers + body). The message ID must come from a prior search — this two-step pattern (search then read) avoids returning full message bodies for every search result, keeping response sizes manageable.

## GmailSendTool

Tool name: `gmail_send`. Sends an email from the authenticated account. Parameters: `to` (recipient address), `subject`, and `body` (plain text). This is a high-trust, high-consequence action — sending email on behalf of the user is irreversible. The `trust_level = "high"` gate ensures the pocket has explicit authorization before this tool is registered.

## Label Management: GmailListLabelsTool and GmailCreateLabelTool

`gmail_list_labels` returns all labels in the account. `gmail_create_label` creates a new one. These exist as prerequisites for the modify tools — before the agent can apply a label, it needs to know what labels exist and potentially create new ones for custom workflows (e.g., "PocketPaw/Processed").

## GmailModifyTool

Tool name: `gmail_modify`. Adds and removes labels on a single message in one API call. The dual `add_labels`/`remove_labels` parameter design matters: the Gmail API supports both in a single `messages.modify` call. Doing them separately would require two round-trips and could leave the message in an intermediate inconsistent state if the second call failed.

## GmailTrashTool

Tool name: `gmail_trash`. Moves a message to the Trash folder. This is distinct from permanent deletion — Gmail's `messages.trash` operation is reversible, which is the appropriate default for an agent action. Permanent deletion would require an additional confirmation mechanism.

## GmailBatchModifyTool

Tool name: `gmail_batch_modify`. Applies the same label changes to multiple messages in a single API call. This exists because inbox management workflows often process dozens or hundreds of messages at once (e.g., "label all unread newsletters as 'Newsletter' and mark them read"). Calling `GmailModifyTool` in a loop would hit Gmail API rate limits quickly; the batch endpoint processes up to 1,000 message IDs in one request.

```python
class GmailBatchModifyTool(BaseTool):
    """Modify labels on multiple Gmail messages at once."""

    async def execute(
        self,
        message_ids: list[str],
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> str:
        ...
```

## Known Gaps

- **No attachment handling**: `GmailReadTool` returns body text only. Attachments cannot be downloaded through the Gmail tools — the agent would need to identify an attachment and separately use a file tool.
- **No thread support**: All tools operate on individual messages by ID. There is no `GmailThreadTool` for reading or acting on conversation threads as a unit.
- **No draft support**: There is no tool for creating, editing, or sending drafts. `GmailSendTool` sends immediately with no review step.