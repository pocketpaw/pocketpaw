# Google Docs tools — read, create, search.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

import logging
import re
from typing import Any

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# Regex to extract document ID from Google Docs URLs
_DOC_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")


def _parse_doc_id(doc_id_or_url: str) -> str:
    """Extract document ID from a URL or return as-is if already an ID."""
    match = _DOC_ID_RE.search(doc_id_or_url)
    if match:
        return match.group(1)
    return doc_id_or_url.strip()


class DocsReadTool(BaseTool):
    """Read a Google Docs document as plain text."""

    @property
    def name(self) -> str:
        return "docs_read"

    @property
    def description(self) -> str:
        return (
            "Read a Google Doc and return its content as plain text. "
            "Accepts a document ID or a Google Docs URL."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID or URL",
                },
            },
            "required": ["document_id"],
        }

    async def execute(self, document_id: str) -> str:
        doc_id = _parse_doc_id(document_id)
        try:
            from pocketclaw.integrations.gdocs import DocsClient

            client = DocsClient()
            result = await client.get_document(doc_id)

            body = result["body"]
            if not body:
                return f"Document '{result['title']}' is empty."

            # Truncate very long docs
            if len(body) > 10000:
                body = body[:10000] + "\n\n... (truncated, showing first 10000 chars)"

            return f"**{result['title']}**\n\n{body}"

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Failed to read document: {e}")


class DocsCreateTool(BaseTool):
    """Create a new Google Doc."""

    @property
    def name(self) -> str:
        return "docs_create"

    @property
    def description(self) -> str:
        return "Create a new Google Doc with optional initial content."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Document title",
                },
                "content": {
                    "type": "string",
                    "description": "Initial text content (optional)",
                },
            },
            "required": ["title"],
        }

    async def execute(self, title: str, content: str = "") -> str:
        try:
            from pocketclaw.integrations.gdocs import DocsClient

            client = DocsClient()
            result = await client.create_document(title, content)

            return (
                f"Created document: {result['title']}\n"
                f"ID: {result['documentId']}\n"
                f"Link: {result['link']}"
            )

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Failed to create document: {e}")


class DocsSearchTool(BaseTool):
    """Search Google Docs by name."""

    @property
    def name(self) -> str:
        return "docs_search"

    @property
    def description(self) -> str:
        return "Search your Google Docs by name/title."

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
                    "description": "Search query for document names",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default 10)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int = 10) -> str:
        try:
            from pocketclaw.integrations.gdocs import DocsClient

            client = DocsClient()
            docs = await client.search_docs(query, max_results)

            if not docs:
                return f"No documents found matching '{query}'."

            lines = [f"Found {len(docs)} document(s):\n"]
            for d in docs:
                link = d.get("webViewLink", "")
                modified = d.get("modifiedTime", "")
                lines.append(
                    f"- **{d['name']}**\n  ID: {d['id']}\n  Modified: {modified}\n  {link}"
                )
            return "\n".join(lines)

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Failed to search documents: {e}")
