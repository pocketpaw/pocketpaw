# GoogleDocsConnector — native adapter wrapping DocsClient.
# Created: 2026-05-03 — Phase 1 PR-5.
# Action surface mirrors src/pocketpaw/tools/builtin/gdocs.py.

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


class GoogleDocsConnector:
    """Native Google Docs connector implementing ConnectorProtocol."""

    @property
    def name(self) -> str:
        return "gdocs"

    @property
    def display_name(self) -> str:
        return "Google Docs"

    @property
    def type(self) -> str:
        return "knowledge"

    @property
    def icon(self) -> str:
        return "file-text"

    def __init__(self) -> None:
        self._connected = False

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            from pocketpaw.integrations.gdocs import DocsClient

            client = DocsClient()
            await client._get_token()  # noqa: SLF001
            self._connected = True
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message="Docs connected",
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
                name="docs_read",
                description="Read the full text content of a Google Doc by ID.",
                method="GET",
                parameters={
                    "document_id": {
                        "type": "string",
                        "description": "Google Doc document ID",
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="docs_create",
                description="Create a new Google Doc with optional initial content.",
                method="POST",
                parameters={
                    "title": {"type": "string", "description": "Document title"},
                    "content": {
                        "type": "string",
                        "description": "Initial document content (optional)",
                        "default": "",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="docs_search",
                description="Search your Google Docs by name / title.",
                method="GET",
                parameters={
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 10, capped at 50)",
                        "default": 10,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        from pocketpaw.integrations.gdocs import DocsClient

        try:
            client = DocsClient()
            if action == "docs_read":
                data = await client.get_document(params["document_id"])
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "docs_create":
                data = await client.create_document(
                    params["title"],
                    params.get("content", ""),
                )
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "docs_search":
                results = await client.search_docs(
                    params["query"],
                    max_results=min(int(params.get("max_results", 10)), 50),
                )
                return ActionResult(success=True, data=results, records_affected=len(results))
            return ActionResult(success=False, error=f"Unknown action: {action}")
        except RuntimeError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ActionResult(success=False, error=f"Docs {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "google_docs", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        return [
            WidgetRecipe(
                title="Recent Docs",
                display_type="feed",
                action="docs_search",
                params={"query": "modifiedTime > 'now-7d'", "max_results": 10},
                default_size="col-1 row-2",
                description="Docs you've touched in the last 7 days",
            ),
        ]

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        try:
            from pocketpaw.integrations.gdocs import DocsClient

            client = DocsClient()
            await client.search_docs("", max_results=1)
            return ConnectorHealth(
                ok=True,
                status=ConnectorStatus.CONNECTED,
                message="Docs reachable",
                checked_at_ms=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectorHealth(
                ok=False,
                status=ConnectorStatus.ERROR,
                message=str(exc),
                checked_at_ms=int(time.time() * 1000),
            )
