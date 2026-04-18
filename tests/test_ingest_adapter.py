"""Tests for the IngestAdapter alias + IngestACL dataclass (Move 7 PR-A).

Created: 2026-04-13 — Wire-shape tests + a duck-typed adapter that
implements both ConnectorProtocol and the new permissions() method.
The fleet template runtime (PR-B) discovers ACL-aware connectors by
checking for the permissions() method, which this test pins as the
contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from pocketpaw.connectors import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorProtocol,
    IngestACL,
    IngestAdapter,
    SyncResult,
)
from pocketpaw.connectors.protocol import ConnectorStatus

# ---------------------------------------------------------------------------
# IngestACL dataclass
# ---------------------------------------------------------------------------


class TestIngestACL:
    def test_defaults_are_empty(self) -> None:
        acl = IngestACL()
        assert acl.scope == []
        assert acl.visibility == ""
        assert acl.source_principal == ""
        assert acl.metadata == {}

    def test_carries_scope_list_into_fabric(self) -> None:
        acl = IngestACL(
            scope=["org:sales:leads"],
            visibility="members",
            source_principal="hubspot:deal-team:42",
        )
        assert acl.scope == ["org:sales:leads"]
        assert acl.visibility == "members"
        assert acl.source_principal == "hubspot:deal-team:42"

    def test_metadata_is_open_dict(self) -> None:
        acl = IngestACL(metadata={"channel_type": "private", "guests_excluded": True})
        assert acl.metadata["channel_type"] == "private"
        assert acl.metadata["guests_excluded"] is True


# ---------------------------------------------------------------------------
# IngestAdapter contract — duck-typed implementation
# ---------------------------------------------------------------------------


class FakeIngestAdapter:
    """Reference implementation that satisfies IngestAdapter at runtime."""

    name = "fake_slack"
    display_name = "Fake Slack"

    def __init__(self) -> None:
        self.permissions_calls: list[tuple[str, str | None]] = []

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
        )

    async def disconnect(self, pocket_id: str) -> bool:
        return True

    async def actions(self) -> list[ActionSchema]:
        return [ActionSchema(name="list_messages", description="List channel messages")]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        return ActionResult(success=True, data={"messages": []})

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name)

    async def schema(self) -> dict[str, Any]:
        return {"messages": {"text": "string"}}

    async def permissions(self, pocket_id: str, record_id: str | None = None) -> IngestACL:
        self.permissions_calls.append((pocket_id, record_id))
        if record_id and record_id.startswith("private_"):
            return IngestACL(
                scope=["org:engineering:eyes-only"],
                visibility="private",
                source_principal=f"slack:channel:{record_id}",
            )
        return IngestACL(scope=["org:public:*"], visibility="public")


class TestIngestAdapterContract:
    def test_fake_adapter_satisfies_connector_protocol(self) -> None:
        adapter = FakeIngestAdapter()
        # Runtime isinstance check on Protocol with @runtime_checkable would
        # work, but ConnectorProtocol isn't decorated. Structural check via
        # attribute presence is the documented pattern in the codebase.
        assert hasattr(adapter, "connect")
        assert hasattr(adapter, "execute")
        assert hasattr(adapter, "sync")
        assert hasattr(adapter, "schema")

    def test_fake_adapter_exposes_permissions_method(self) -> None:
        adapter = FakeIngestAdapter()
        assert callable(getattr(adapter, "permissions", None))

    @pytest.mark.asyncio
    async def test_permissions_default_returns_public_scope(self) -> None:
        adapter = FakeIngestAdapter()
        acl = await adapter.permissions("pocket-1")
        assert "org:public:*" in acl.scope
        assert acl.visibility == "public"

    @pytest.mark.asyncio
    async def test_permissions_per_record_returns_private_scope(self) -> None:
        adapter = FakeIngestAdapter()
        acl = await adapter.permissions("pocket-1", record_id="private_founders")
        assert "org:engineering:eyes-only" in acl.scope
        assert acl.visibility == "private"
        assert "private_founders" in acl.source_principal

    @pytest.mark.asyncio
    async def test_permissions_call_records_pocket_and_record_args(self) -> None:
        adapter = FakeIngestAdapter()
        await adapter.permissions("pocket-1", record_id="msg_42")
        await adapter.permissions("pocket-2")
        assert adapter.permissions_calls == [("pocket-1", "msg_42"), ("pocket-2", None)]


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_ingest_adapter_re_exported(self) -> None:
        from pocketpaw.connectors import IngestAdapter as RexportedAdapter
        from pocketpaw.connectors.protocol import IngestAdapter as DirectAdapter

        assert RexportedAdapter is DirectAdapter

    def test_ingest_acl_re_exported(self) -> None:
        from pocketpaw.connectors import IngestACL as RexportedACL
        from pocketpaw.connectors.protocol import IngestACL as DirectACL

        assert RexportedACL is DirectACL


# ---------------------------------------------------------------------------
# Static check — IngestAdapter type alias keeps ConnectorProtocol surface
# ---------------------------------------------------------------------------


def test_ingest_adapter_protocol_extends_connector_protocol() -> None:
    """IngestAdapter should not lose any ConnectorProtocol methods."""
    connector_methods = {
        "connect",
        "disconnect",
        "actions",
        "execute",
        "sync",
        "schema",
    }
    ingest_methods = {
        "connect",
        "disconnect",
        "actions",
        "execute",
        "sync",
        "schema",
        "permissions",
    }
    # Inspect the Protocol classes to be sure.
    for method in connector_methods:
        assert hasattr(ConnectorProtocol, method)
    for method in ingest_methods:
        assert hasattr(IngestAdapter, method)
