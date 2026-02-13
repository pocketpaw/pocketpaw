# Google Drive Client — HTTP client for Drive API using OAuth tokens.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from pocketclaw.config import get_config_dir, get_settings
from pocketclaw.integrations.oauth import OAuthManager
from pocketclaw.integrations.token_store import TokenStore

logger = logging.getLogger(__name__)

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"


def _get_downloads_dir() -> Path:
    """Get/create the downloads directory."""
    d = get_config_dir() / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


class DriveClient:
    """HTTP client for Google Drive API v3.

    Uses OAuth bearer tokens from the token store.
    """

    def __init__(self):
        self._oauth = OAuthManager(TokenStore())

    async def _get_token(self) -> str:
        """Get a valid OAuth access token for Drive."""
        settings = get_settings()
        token = await self._oauth.get_valid_token(
            service="google_drive",
            client_id=settings.google_oauth_client_id or "",
            client_secret=settings.google_oauth_client_secret or "",
        )
        if not token:
            raise RuntimeError(
                "Google Drive not authenticated. Complete OAuth flow first "
                "(Settings > Google OAuth > Authorize Drive)."
            )
        return token

    async def list_files(
        self, query: str | None = None, max_results: int = 20
    ) -> list[dict[str, Any]]:
        """List or search files in Drive.

        Args:
            query: Drive search query (e.g. "name contains 'report'").
            max_results: Maximum number of results.

        Returns:
            List of file dicts with id, name, mimeType, modifiedTime, size.
        """
        token = await self._get_token()
        params: dict[str, Any] = {
            "pageSize": min(max_results, 100),
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
            "orderBy": "modifiedTime desc",
        }
        if query:
            params["q"] = query

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_DRIVE_BASE}/files",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return data.get("files", [])

    async def download(self, file_id: str) -> dict[str, Any]:
        """Download a file from Drive.

        Args:
            file_id: Drive file ID.

        Returns:
            Dict with name, path (local), size.
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Get file metadata first
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_DRIVE_BASE}/files/{file_id}",
                params={"fields": "id,name,mimeType,size"},
                headers=headers,
            )
            resp.raise_for_status()
            meta = resp.json()

        name = meta.get("name", f"{file_id}.bin")
        mime = meta.get("mimeType", "")

        # Google Docs/Sheets/Slides need export, not direct download
        export_mimes = {
            "application/vnd.google-apps.document": (
                "application/pdf",
                f"{name}.pdf",
            ),
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                f"{name}.xlsx",
            ),
            "application/vnd.google-apps.presentation": (
                "application/pdf",
                f"{name}.pdf",
            ),
        }

        async with httpx.AsyncClient(timeout=60) as client:
            if mime in export_mimes:
                export_mime, export_name = export_mimes[mime]
                resp = await client.get(
                    f"{_DRIVE_BASE}/files/{file_id}/export",
                    params={"mimeType": export_mime},
                    headers=headers,
                )
                name = export_name
            else:
                resp = await client.get(
                    f"{_DRIVE_BASE}/files/{file_id}",
                    params={"alt": "media"},
                    headers=headers,
                )
            resp.raise_for_status()

        output_path = _get_downloads_dir() / name
        output_path.write_bytes(resp.content)

        return {
            "name": name,
            "path": str(output_path),
            "size": len(resp.content),
        }

    async def upload(
        self,
        file_path: str,
        name: str | None = None,
        folder_id: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file to Drive.

        Args:
            file_path: Local file path.
            name: File name in Drive (defaults to local filename).
            folder_id: Parent folder ID (defaults to root).
            mime_type: MIME type (auto-detected if not specified).

        Returns:
            Dict with id, name, webViewLink.
        """
        token = await self._get_token()
        local = Path(file_path).expanduser()
        if not local.exists():
            raise FileNotFoundError(f"File not found: {local}")

        upload_name = name or local.name
        metadata: dict[str, Any] = {"name": upload_name}
        if folder_id:
            metadata["parents"] = [folder_id]

        content_type = mime_type or "application/octet-stream"
        file_bytes = local.read_bytes()

        # Multipart upload
        boundary = "pocketpaw_boundary"
        body = (
            (
                f"--{boundary}\r\n"
                f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + file_bytes
            + f"\r\n--{boundary}--".encode()
        )

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_UPLOAD_BASE}/files",
                params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                content=body,
            )
            resp.raise_for_status()

        return resp.json()

    async def share(
        self,
        file_id: str,
        email: str,
        role: str = "reader",
    ) -> dict[str, str]:
        """Share a file with a user.

        Args:
            file_id: Drive file ID.
            email: Email address to share with.
            role: Permission role ('reader', 'writer', 'commenter').

        Returns:
            Dict with status.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_DRIVE_BASE}/files/{file_id}/permissions",
                json={"type": "user", "role": role, "emailAddress": email},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()

        return {"status": "shared", "email": email, "role": role}

    async def delete(self, file_id: str) -> dict[str, str]:
        """Delete a file from Drive (move to trash).

        Args:
            file_id: Drive file ID.

        Returns:
            Dict with status.
        """
        token = await self._get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_DRIVE_BASE}/files/{file_id}",
                json={"trashed": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()

        return {"status": "trashed", "file_id": file_id}
