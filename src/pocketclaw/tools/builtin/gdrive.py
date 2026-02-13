# Google Drive tools — list, download, upload, share.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

import logging
from typing import Any

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class DriveListTool(BaseTool):
    """List or search files in Google Drive."""

    @property
    def name(self) -> str:
        return "drive_list"

    @property
    def description(self) -> str:
        return (
            "List or search files in Google Drive. "
            "Uses Drive search query syntax (e.g. \"name contains 'report'\")."
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
                    "description": (
                        "Drive search query (e.g. \"name contains 'report'\", "
                        "\"mimeType='application/pdf'\"). Omit to list recent files."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default 20, max 100)",
                },
            },
            "required": [],
        }

    async def execute(self, query: str | None = None, max_results: int = 20) -> str:
        try:
            from pocketclaw.integrations.gdrive import DriveClient

            client = DriveClient()
            files = await client.list_files(query=query, max_results=max_results)

            if not files:
                return "No files found."

            lines = [f"Found {len(files)} file(s):\n"]
            for f in files:
                size = f.get("size", "")
                size_str = f" ({int(size):,} bytes)" if size else ""
                link = f.get("webViewLink", "")
                link_str = f"\n   {link}" if link else ""
                lines.append(
                    f"- **{f['name']}** [{f.get('mimeType', 'unknown')}]{size_str}\n"
                    f"  ID: {f['id']}{link_str}"
                )
            return "\n".join(lines)

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Drive list failed: {e}")


class DriveDownloadTool(BaseTool):
    """Download a file from Google Drive."""

    @property
    def name(self) -> str:
        return "drive_download"

    @property
    def description(self) -> str:
        return (
            "Download a file from Google Drive by its file ID. "
            "Google Docs/Sheets/Slides are exported as PDF/XLSX."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID",
                },
            },
            "required": ["file_id"],
        }

    async def execute(self, file_id: str) -> str:
        try:
            from pocketclaw.integrations.gdrive import DriveClient

            client = DriveClient()
            result = await client.download(file_id)

            return (
                f"Downloaded: {result['name']}\n"
                f"Saved to: {result['path']}\n"
                f"Size: {result['size']:,} bytes"
            )

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Drive download failed: {e}")


class DriveUploadTool(BaseTool):
    """Upload a file to Google Drive."""

    @property
    def name(self) -> str:
        return "drive_upload"

    @property
    def description(self) -> str:
        return "Upload a local file to Google Drive."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Local file path to upload",
                },
                "name": {
                    "type": "string",
                    "description": "File name in Drive (defaults to local filename)",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Parent folder ID in Drive (defaults to root)",
                },
            },
            "required": ["file_path"],
        }

    async def execute(
        self,
        file_path: str,
        name: str | None = None,
        folder_id: str | None = None,
    ) -> str:
        try:
            from pocketclaw.integrations.gdrive import DriveClient

            client = DriveClient()
            result = await client.upload(file_path, name=name, folder_id=folder_id)

            link = result.get("webViewLink", "")
            link_str = f"\nLink: {link}" if link else ""
            return (
                f"Uploaded: {result.get('name', file_path)}\n"
                f"ID: {result.get('id', 'unknown')}{link_str}"
            )

        except FileNotFoundError as e:
            return self._error(str(e))
        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Drive upload failed: {e}")


class DriveShareTool(BaseTool):
    """Share a Google Drive file with a user."""

    @property
    def name(self) -> str:
        return "drive_share"

    @property
    def description(self) -> str:
        return "Share a Google Drive file with a user by email."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID",
                },
                "email": {
                    "type": "string",
                    "description": "Email address to share with",
                },
                "role": {
                    "type": "string",
                    "description": (
                        "Permission role: 'reader', 'writer', or 'commenter' (default: reader)"
                    ),
                },
            },
            "required": ["file_id", "email"],
        }

    async def execute(
        self,
        file_id: str,
        email: str,
        role: str = "reader",
    ) -> str:
        if role not in ("reader", "writer", "commenter"):
            return self._error(f"Invalid role '{role}'. Use reader, writer, or commenter.")

        try:
            from pocketclaw.integrations.gdrive import DriveClient

            client = DriveClient()
            await client.share(file_id, email, role)

            return f"Shared file {file_id} with {email} as {role}."

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Drive share failed: {e}")
