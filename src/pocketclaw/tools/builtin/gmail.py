# Gmail tools — search, read, and send email via Gmail API.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

import logging
from typing import Any

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class GmailSearchTool(BaseTool):
    """Search Gmail for messages matching a query."""

    @property
    def name(self) -> str:
        return "gmail_search"

    @property
    def description(self) -> str:
        return (
            "Search Gmail for emails matching a query. Uses the same syntax as the Gmail "
            "search bar (e.g., 'from:bob subject:meeting', 'is:unread', 'newer_than:1d')."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int = 5) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            results = await client.search(query, max_results=min(max_results, 20))

            if not results:
                return f"No emails found matching: {query}"

            lines = [f"Found {len(results)} email(s):\n"]
            for i, msg in enumerate(results, 1):
                lines.append(
                    f"{i}. **{msg['subject']}**\n"
                    f"   From: {msg['from']}\n"
                    f"   Date: {msg['date']}\n"
                    f"   {msg['snippet'][:150]}\n"
                    f"   [ID: {msg['id']}]\n"
                )
            return "\n".join(lines)

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Gmail search failed: {e}")


class GmailReadTool(BaseTool):
    """Read a specific Gmail message by ID."""

    @property
    def name(self) -> str:
        return "gmail_read"

    @property
    def description(self) -> str:
        return "Read a full Gmail message by its ID. Use gmail_search first to find message IDs."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID (from gmail_search results)",
                },
            },
            "required": ["message_id"],
        }

    async def execute(self, message_id: str) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            msg = await client.read(message_id)

            return (
                f"**{msg['subject']}**\n"
                f"From: {msg['from']}\n"
                f"To: {msg['to']}\n"
                f"Date: {msg['date']}\n\n"
                f"{msg['body']}"
            )

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Failed to read email: {e}")


class GmailSendTool(BaseTool):
    """Send an email via Gmail."""

    @property
    def name(self) -> str:
        return "gmail_send"

    @property
    def description(self) -> str:
        return "Send an email via Gmail. Requires OAuth authentication."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject",
                },
                "body": {
                    "type": "string",
                    "description": "Email body (plain text)",
                },
            },
            "required": ["to", "subject", "body"],
        }

    async def execute(self, to: str, subject: str, body: str) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            result = await client.send(to=to, subject=subject, body=body)
            return f"Email sent successfully. Message ID: {result['id']}"

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Failed to send email: {e}")


class GmailListLabelsTool(BaseTool):
    """List all Gmail labels."""

    @property
    def name(self) -> str:
        return "gmail_list_labels"

    @property
    def description(self) -> str:
        return "List all Gmail labels with their IDs. Useful before creating or modifying labels."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            labels = await client.list_labels()
            lines = [f"Gmail labels ({len(labels)}):\n"]
            for lb in labels:
                lines.append(f"  {lb['name']:40s} [ID: {lb['id']}]  ({lb['type']})")
            return "\n".join(lines)
        except Exception as e:
            return self._error(f"Failed to list labels: {e}")


class GmailCreateLabelTool(BaseTool):
    """Create a Gmail label."""

    @property
    def name(self) -> str:
        return "gmail_create_label"

    @property
    def description(self) -> str:
        return "Create a new Gmail label. Use '/' for nested labels (e.g. 'Work/Projects')."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Label name"},
            },
            "required": ["name"],
        }

    async def execute(self, name: str) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            result = await client.create_label(name)
            return f"Label created: {result['name']} [ID: {result['id']}]"
        except Exception as e:
            return self._error(f"Failed to create label: {e}")


class GmailModifyTool(BaseTool):
    """Modify labels on a Gmail message."""

    @property
    def name(self) -> str:
        return "gmail_modify"

    @property
    def description(self) -> str:
        return (
            "Add or remove labels on a Gmail message. "
            "Common IDs: INBOX, SPAM, TRASH, UNREAD, STARRED, IMPORTANT. "
            "Use gmail_list_labels to find custom label IDs."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID"},
                "add_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label IDs to add",
                },
                "remove_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label IDs to remove",
                },
            },
            "required": ["message_id"],
        }

    async def execute(
        self,
        message_id: str,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            result = await client.modify_message(
                message_id, add_labels=add_labels, remove_labels=remove_labels
            )
            return f"Modified message {result['id']}. Labels: {result['labelIds']}"
        except Exception as e:
            return self._error(f"Failed to modify message: {e}")


class GmailTrashTool(BaseTool):
    """Move a Gmail message to trash."""

    @property
    def name(self) -> str:
        return "gmail_trash"

    @property
    def description(self) -> str:
        return "Move a Gmail message to trash."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID"},
            },
            "required": ["message_id"],
        }

    async def execute(self, message_id: str) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            await client.trash(message_id)
            return f"Message {message_id} moved to trash."
        except Exception as e:
            return self._error(f"Failed to trash message: {e}")


class GmailBatchModifyTool(BaseTool):
    """Modify labels on multiple Gmail messages at once."""

    @property
    def name(self) -> str:
        return "gmail_batch_modify"

    @property
    def description(self) -> str:
        return "Add or remove labels on multiple Gmail messages at once (bulk operation)."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Gmail message IDs",
                },
                "add_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label IDs to add",
                },
                "remove_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label IDs to remove",
                },
            },
            "required": ["message_ids"],
        }

    async def execute(
        self,
        message_ids: list[str],
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> str:
        from pocketclaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()
            await client.batch_modify(
                message_ids, add_labels=add_labels, remove_labels=remove_labels
            )
            return f"Batch modified {len(message_ids)} messages."
        except Exception as e:
            return self._error(f"Failed to batch modify: {e}")
