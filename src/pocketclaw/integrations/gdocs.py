# Google Docs Client — HTTP client for Docs API using OAuth tokens.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

from __future__ import annotations

import logging
from typing import Any

import httpx

from pocketclaw.config import get_settings
from pocketclaw.integrations.oauth import OAuthManager
from pocketclaw.integrations.token_store import TokenStore

logger = logging.getLogger(__name__)

_DOCS_BASE = "https://docs.googleapis.com/v1/documents"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"


class DocsClient:
    """HTTP client for Google Docs API v1.

    Uses OAuth bearer tokens from the token store.
    """

    def __init__(self):
        self._oauth = OAuthManager(TokenStore())

    async def _get_token(self) -> str:
        """Get a valid OAuth access token for Docs."""
        settings = get_settings()
        token = await self._oauth.get_valid_token(
            service="google_docs",
            client_id=settings.google_oauth_client_id or "",
            client_secret=settings.google_oauth_client_secret or "",
        )
        if not token:
            raise RuntimeError(
                "Google Docs not authenticated. Complete OAuth flow first "
                "(Settings > Google OAuth > Authorize Docs)."
            )
        return token

    async def get_document(self, document_id: str) -> dict[str, Any]:
        """Read a Google Doc and return its content as plain text.

        Args:
            document_id: Google Docs document ID.

        Returns:
            Dict with title and body (plain text).
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_DOCS_BASE}/{document_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            doc = resp.json()

        title = doc.get("title", "Untitled")
        body_text = self._extract_text(doc)

        return {"title": title, "body": body_text, "document_id": document_id}

    async def create_document(self, title: str, content: str = "") -> dict[str, Any]:
        """Create a new Google Doc.

        Args:
            title: Document title.
            content: Initial text content.

        Returns:
            Dict with documentId, title, link.
        """
        token = await self._get_token()

        # Step 1: Create empty doc
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _DOCS_BASE,
                json={"title": title},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            doc = resp.json()

        doc_id = doc["documentId"]

        # Step 2: Insert content if provided
        if content:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{_DOCS_BASE}/{doc_id}:batchUpdate",
                    json={
                        "requests": [
                            {
                                "insertText": {
                                    "location": {"index": 1},
                                    "text": content,
                                }
                            }
                        ]
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()

        return {
            "documentId": doc_id,
            "title": title,
            "link": f"https://docs.google.com/document/d/{doc_id}/edit",
        }

    async def search_docs(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search Google Docs by name via Drive API.

        Args:
            query: Search query for document names.
            max_results: Maximum number of results.

        Returns:
            List of doc dicts with id, name, modifiedTime, link.
        """
        token = await self._get_token()

        drive_query = (
            f"mimeType='application/vnd.google-apps.document' "
            f"and name contains '{query}' and trashed=false"
        )

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_DRIVE_BASE}/files",
                params={
                    "q": drive_query,
                    "pageSize": min(max_results, 50),
                    "fields": "files(id,name,modifiedTime,webViewLink)",
                    "orderBy": "modifiedTime desc",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return data.get("files", [])

    @staticmethod
    def _extract_text(doc: dict) -> str:
        """Extract plain text from a Google Docs document JSON.

        Walks body.content → paragraph.elements → textRun.content.
        """
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
