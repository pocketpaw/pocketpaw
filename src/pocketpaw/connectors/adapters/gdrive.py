# GoogleDriveConnector — native adapter wrapping DriveClient.
# Created: 2026-05-03 — Phase 1 PR-6.
# Note: drive.yaml already exists at workspace root and is consumed by
# DirectRESTAdapter. This native adapter takes precedence via the
# registry's _create_native_adapter dispatch. The existing
# src/pocketpaw/connectors/drive/ subpackage (SourceAdapter for KB
# ingestion) keeps working — it's a different surface.
#
# Action surface mirrors src/pocketpaw/tools/builtin/gdrive.py.

from __future__ import annotations

import logging
import time
from typing import Any

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorHealth,
    ConnectorScope,
    ConnectorStatus,
    ExecutionMode,
    SyncResult,
    TrustLevel,
    WidgetRecipe,
)

logger = logging.getLogger(__name__)


class GoogleDriveConnector:
    """Native Google Drive connector implementing ConnectorProtocol."""

    @property
    def name(self) -> str:
        return "drive"

    @property
    def display_name(self) -> str:
        return "Google Drive"

    @property
    def type(self) -> str:
        return "knowledge"

    @property
    def icon(self) -> str:
        return "cloud"

    def __init__(self) -> None:
        self._connected = False

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            from pocketpaw.integrations.gdrive import DriveClient

            client = DriveClient()
            await client._get_token()  # noqa: SLF001
            self._connected = True
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message="Drive connected",
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=str(exc),
            )

    async def disconnect(self, pocket_id: str) -> bool:
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="drive_list",
                description=(
                    "List or search files in Google Drive. Pass a Drive search query "
                    "(e.g., \"name contains 'forecast'\") or omit to get newest first."
                ),
                method="GET",
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Drive search query (optional)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 20, capped at 100)",
                        "default": 20,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="drive_download",
                description="Download a file's content from Drive (Google Docs export to PDF).",
                method="GET",
                parameters={
                    "file_id": {"type": "string", "description": "Drive file ID"},
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="drive_upload",
                description="Upload a local file to Google Drive.",
                method="POST",
                parameters={
                    "filename": {"type": "string", "description": "File name in Drive"},
                    "content": {"type": "string", "description": "File content (text)"},
                    "mime_type": {
                        "type": "string",
                        "description": "MIME type (default text/plain)",
                        "default": "text/plain",
                    },
                    "parent_folder_id": {
                        "type": "string",
                        "description": "Parent folder ID (optional)",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="drive_share",
                description="Share a Google Drive file with a user by email.",
                method="POST",
                parameters={
                    "file_id": {"type": "string", "description": "Drive file ID"},
                    "email": {"type": "string", "description": "Recipient email"},
                    "role": {
                        "type": "string",
                        "description": "Permission role: reader | writer | commenter",
                        "default": "reader",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        from pocketpaw.integrations.gdrive import DriveClient

        try:
            client = DriveClient()
            if action == "drive_list":
                files = await client.list_files(
                    query=params.get("query"),
                    max_results=min(int(params.get("max_results", 20)), 100),
                )
                return ActionResult(success=True, data=files, records_affected=len(files))
            if action == "drive_download":
                data = await client.download(params["file_id"])
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "drive_upload":
                data = await client.upload(
                    params["filename"],
                    params["content"],
                    mime_type=params.get("mime_type", "text/plain"),
                    parent_folder_id=params.get("parent_folder_id"),
                )
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "drive_share":
                data = await client.share(
                    params["file_id"],
                    params["email"],
                    role=params.get("role", "reader"),
                )
                return ActionResult(success=True, data=data, records_affected=1)
            return ActionResult(success=False, error=f"Unknown action: {action}")
        except RuntimeError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ActionResult(success=False, error=f"Drive {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "drive_files", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        return [
            WidgetRecipe(
                title="Recent Drive Files",
                display_type="feed",
                action="drive_list",
                params={"max_results": 10},
                default_size="col-1 row-2",
                description="Files you've touched recently in Drive",
            ),
            WidgetRecipe(
                title="Shared with Me",
                display_type="feed",
                action="drive_list",
                params={"query": "sharedWithMe", "max_results": 10},
                default_size="col-1 row-2",
                description="Recently shared documents",
            ),
        ]

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        try:
            from pocketpaw.integrations.gdrive import DriveClient

            client = DriveClient()
            await client.list_files(max_results=1)
            return ConnectorHealth(
                ok=True,
                status=ConnectorStatus.CONNECTED,
                message="Drive reachable",
                checked_at_ms=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectorHealth(
                ok=False,
                status=ConnectorStatus.ERROR,
                message=str(exc),
                checked_at_ms=int(time.time() * 1000),
            )
