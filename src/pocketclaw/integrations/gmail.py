# Gmail Client — HTTP client for Gmail API using OAuth tokens.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from typing import Any

import httpx

from pocketclaw.config import get_settings
from pocketclaw.integrations.oauth import OAuthManager
from pocketclaw.integrations.token_store import TokenStore

logger = logging.getLogger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailClient:
    """HTTP client for Gmail API.

    Uses OAuth bearer tokens from the token store. No new dependencies
    beyond httpx (already a core dep).
    """

    def __init__(self):
        self._oauth = OAuthManager(TokenStore())

    async def _get_token(self) -> str:
        """Get a valid OAuth access token for Gmail."""
        settings = get_settings()
        token = await self._oauth.get_valid_token(
            service="google_gmail",
            client_id=settings.google_oauth_client_id or "",
            client_secret=settings.google_oauth_client_secret or "",
        )
        if not token:
            raise RuntimeError(
                "Gmail not authenticated. Complete OAuth flow first "
                "(Settings > Google OAuth > Authorize Gmail)."
            )
        return token

    async def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search Gmail messages.

        Args:
            query: Gmail search query (same syntax as Gmail search bar).
            max_results: Maximum number of results.

        Returns:
            List of message summaries with id, snippet, subject, from, date.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            # List message IDs
            resp = await client.get(
                f"{_GMAIL_BASE}/messages",
                params={"q": query, "maxResults": max_results},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        messages = data.get("messages", [])
        if not messages:
            return []

        # Fetch metadata for each message
        results = []
        async with httpx.AsyncClient(timeout=15) as client:
            for msg in messages[:max_results]:
                try:
                    resp = await client.get(
                        f"{_GMAIL_BASE}/messages/{msg['id']}",
                        params={
                            "format": "metadata",
                            "metadataHeaders": ["From", "Subject", "Date"],
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    resp.raise_for_status()
                    msg_data = resp.json()

                    headers = {
                        h["name"]: h["value"]
                        for h in msg_data.get("payload", {}).get("headers", [])
                    }
                    results.append(
                        {
                            "id": msg["id"],
                            "subject": headers.get("Subject", "(no subject)"),
                            "from": headers.get("From", ""),
                            "date": headers.get("Date", ""),
                            "snippet": msg_data.get("snippet", ""),
                        }
                    )
                except Exception as e:
                    logger.warning("Failed to fetch message %s: %s", msg["id"], e)

        return results

    async def read(self, message_id: str) -> dict[str, Any]:
        """Read a full Gmail message.

        Args:
            message_id: Gmail message ID.

        Returns:
            Dict with subject, from, date, body (plain text).
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GMAIL_BASE}/messages/{message_id}",
                params={"format": "full"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}

        # Extract plain text body
        body = self._extract_body(data.get("payload", {}))

        return {
            "id": message_id,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "body": body,
            "snippet": data.get("snippet", ""),
        }

    async def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        """Send an email.

        Args:
            to: Recipient email address.
            subject: Email subject.
            body: Plain text body.

        Returns:
            Dict with message id and thread id.
        """
        token = await self._get_token()

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/messages/send",
                json={"raw": raw},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return {"id": data.get("id", ""), "threadId": data.get("threadId", "")}

    async def list_labels(self) -> list[dict[str, Any]]:
        """List all Gmail labels.

        Returns:
            List of label dicts with id, name, type.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GMAIL_BASE}/labels",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            {"id": lb["id"], "name": lb["name"], "type": lb.get("type", "")}
            for lb in data.get("labels", [])
        ]

    async def create_label(self, name: str) -> dict[str, Any]:
        """Create a Gmail label.

        Args:
            name: Label name (supports nesting with '/': 'Parent/Child').

        Returns:
            Dict with id and name of the created label.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/labels",
                json={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return {"id": data["id"], "name": data["name"]}

    async def modify_message(
        self,
        message_id: str,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Modify a message's labels (move, archive, mark read, etc.).

        Args:
            message_id: Gmail message ID.
            add_labels: Label IDs to add.
            remove_labels: Label IDs to remove.

        Returns:
            Dict with id and updated labelIds.

        Common label IDs:
            INBOX, SPAM, TRASH, UNREAD, STARRED, IMPORTANT
        """
        token = await self._get_token()

        body: dict[str, Any] = {}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/messages/{message_id}/modify",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return {"id": data["id"], "labelIds": data.get("labelIds", [])}

    async def trash(self, message_id: str) -> dict[str, str]:
        """Move a message to trash.

        Args:
            message_id: Gmail message ID.

        Returns:
            Dict with id.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/messages/{message_id}/trash",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()

        return {"id": message_id}

    async def batch_modify(
        self,
        message_ids: list[str],
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> None:
        """Modify labels on multiple messages at once.

        Args:
            message_ids: List of Gmail message IDs.
            add_labels: Label IDs to add to all messages.
            remove_labels: Label IDs to remove from all messages.
        """
        token = await self._get_token()

        body: dict[str, Any] = {"ids": message_ids}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/messages/batchModify",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Extract plain text body from a Gmail message payload."""
        # Direct body
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Multipart
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            # Nested multipart
            for sub in part.get("parts", []):
                if sub.get("mimeType") == "text/plain":
                    data = sub.get("body", {}).get("data", "")
                    if data:
                        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        return "(no text content)"
