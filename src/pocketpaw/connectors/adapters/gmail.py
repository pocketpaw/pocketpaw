# GmailConnector — native adapter wrapping the existing GmailClient.
# Created: 2026-05-03 — Phase 1 PR-3 (reference implementation).
# Adopts the ConnectorProtocol surface (actions / execute / sync /
# schema / widgets / health) so the home widget picker can read recipes
# from /api/v1/cloud/connectors/widget-recipes and the cloud router can
# proxy execute calls. Wraps the existing GmailClient at
# src/pocketpaw/integrations/gmail.py — that client owns the OAuth
# refresh, MIME construction, and base64 encoding.
#
# The 8 hand-written agent tool classes at
# src/pocketpaw/tools/builtin/gmail.py stay in place for Phase 1.
# A future PR replaces them with generated tools via
# tools.builtin.connector_tools_for(c) — the snapshot tests in
# tests/connectors/test_gmail_connector.py pin the action surface so
# the replacement is byte-identical.

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


class GmailConnector:
    """Native Gmail connector implementing ConnectorProtocol.

    Action surface mirrors the 8 hand-written tools in
    ``tools/builtin/gmail.py`` — same names, same descriptions, same
    parameter schemas. ``gmail_summary`` is a 9th action added in this
    PR to back the Email Stats widget recipe.
    """

    @property
    def name(self) -> str:
        return "gmail"

    @property
    def display_name(self) -> str:
        return "Gmail"

    @property
    def type(self) -> str:
        return "communication"

    @property
    def icon(self) -> str:
        return "mail"

    def __init__(self) -> None:
        self._connected = False

    # ------------------------------------------------------------------
    # connect / disconnect — credentials are managed by the GmailClient
    # via its TokenStore. The adapter just tracks connected state for
    # health() reporting.
    # ------------------------------------------------------------------

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            from pocketpaw.integrations.gmail import GmailClient

            client = GmailClient()
            # Touch the token store so a missing token surfaces immediately.
            await client._get_token()  # noqa: SLF001 — internal contract
            self._connected = True
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message="Gmail connected",
            )
        except Exception as exc:  # noqa: BLE001 — connect must never crash the registry
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=str(exc),
            )

    async def disconnect(self, pocket_id: str) -> bool:
        self._connected = False
        return True

    # ------------------------------------------------------------------
    # actions / execute — 9 actions, all cloud mode (Gmail API is REST
    # over HTTPS with bearer auth; no host-machine state needed).
    # ------------------------------------------------------------------

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="gmail_search",
                description=(
                    "Search Gmail for emails matching a query. Uses the same syntax as "
                    "the Gmail search bar (e.g., 'from:bob subject:meeting', 'is:unread', "
                    "'newer_than:1d')."
                ),
                method="GET",
                parameters={
                    "query": {"type": "string", "description": "Gmail search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 5, capped at 20)",
                        "default": 5,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_read",
                description="Read the full body of a single Gmail message by ID.",
                method="GET",
                parameters={
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID (from gmail_search results)",
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_send",
                description="Send an email from the authenticated account.",
                method="POST",
                parameters={
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body (plain text)"},
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_list_labels",
                description="List all labels in the mailbox.",
                method="GET",
                parameters={},
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_create_label",
                description="Create a new label.",
                method="POST",
                parameters={
                    "name": {"type": "string", "description": "Label name"},
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_modify",
                description="Add or remove labels on a single message.",
                method="POST",
                parameters={
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "add_label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to add",
                    },
                    "remove_label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to remove",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_trash",
                description="Move a message to Trash.",
                method="POST",
                parameters={
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_batch_modify",
                description="Apply label changes to many messages at once.",
                method="POST",
                parameters={
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Gmail message IDs",
                    },
                    "add_label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to add",
                    },
                    "remove_label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to remove",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="gmail_summary",
                description=(
                    "Aggregate inbox stats — unread / today / avg reply time. "
                    "Backs the Email Stats widget."
                ),
                method="GET",
                parameters={},
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        from pocketpaw.integrations.gmail import GmailClient

        try:
            client = GmailClient()

            if action == "gmail_search":
                results = await client.search(
                    params["query"],
                    max_results=min(int(params.get("max_results", 5)), 20),
                )
                return ActionResult(success=True, data=results, records_affected=len(results))
            if action == "gmail_read":
                data = await client.read(params["message_id"])
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "gmail_send":
                data = await client.send(params["to"], params["subject"], params["body"])
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "gmail_list_labels":
                labels = await client.list_labels()
                return ActionResult(success=True, data=labels, records_affected=len(labels))
            if action == "gmail_create_label":
                data = await client.create_label(params["name"])
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "gmail_modify":
                data = await client.modify_message(
                    params["message_id"],
                    add_label_ids=params.get("add_label_ids", []),
                    remove_label_ids=params.get("remove_label_ids", []),
                )
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "gmail_trash":
                data = await client.trash(params["message_id"])
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "gmail_batch_modify":
                data = await client.batch_modify(
                    params["message_ids"],
                    add_label_ids=params.get("add_label_ids", []),
                    remove_label_ids=params.get("remove_label_ids", []),
                )
                return ActionResult(
                    success=True, data=data, records_affected=len(params["message_ids"])
                )
            if action == "gmail_summary":
                # Lightweight aggregate built from gmail_search results.
                # Not an API call of its own — the Email Stats widget
                # gets enough from one search per metric.
                unread = await client.search("is:unread", max_results=1)
                today = await client.search("newer_than:1d", max_results=1)
                # records_affected for a search counts the page; the
                # widget just needs the buckets.
                return ActionResult(
                    success=True,
                    data={
                        "unread": len(unread),
                        "today": len(today),
                        "avg_reply_time": "—",  # backfilled by a heavier aggregator later
                    },
                    records_affected=1,
                )
            return ActionResult(success=False, error=f"Unknown action: {action}")
        except RuntimeError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ActionResult(success=False, error=f"Gmail {action} failed: {exc}")

    # ------------------------------------------------------------------
    # sync / schema — minimal stubs. Sync is for KB ingestion; Phase 1
    # doesn't ingest Gmail into KB (Files-as-Knowledge handles direct
    # uploads). schema() is for KB table mapping; not used yet.
    # ------------------------------------------------------------------

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(
            success=True,
            connector_name=self.name,
            records_synced=0,
        )

    async def schema(self) -> dict[str, Any]:
        return {"table": "gmail_messages", "mapping": {}, "schedule": "manual"}

    # ------------------------------------------------------------------
    # Phase 1 PR-2 protocol additions
    # ------------------------------------------------------------------

    async def widgets(self) -> list[WidgetRecipe]:
        """Three default home widgets users get when Gmail is enabled."""
        return [
            WidgetRecipe(
                title="Inbox",
                display_type="feed",
                action="gmail_search",
                params={"query": "is:unread", "max_results": 10},
                default_size="col-1 row-2",
                description="Unread messages in your inbox",
            ),
            WidgetRecipe(
                title="Important Emails",
                display_type="feed",
                action="gmail_search",
                params={"query": "is:important newer_than:1d", "max_results": 10},
                default_size="col-1 row-2",
                description="Messages flagged important in the last 24h",
            ),
            WidgetRecipe(
                title="Email Stats",
                display_type="stats",
                action="gmail_summary",
                params={},
                default_size="col-1 row-1",
                description="Inbox health at a glance",
            ),
        ]

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        """Cheap health check — pings list_labels (auth-only round-trip)."""
        try:
            from pocketpaw.integrations.gmail import GmailClient

            client = GmailClient()
            await client.list_labels()
            return ConnectorHealth(
                ok=True,
                status=ConnectorStatus.CONNECTED,
                message="Gmail reachable",
                checked_at_ms=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectorHealth(
                ok=False,
                status=ConnectorStatus.ERROR,
                message=str(exc),
                checked_at_ms=int(time.time() * 1000),
            )
