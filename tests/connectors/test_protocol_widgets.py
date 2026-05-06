# Connector protocol additions — Phase 1 PR-2 contract tests.
# Created: 2026-05-03 — pins the new types added to
# src/pocketpaw/connectors/protocol.py: ExecutionMode, ConnectorScope
# (PocketScope | WorkspaceScope | UserScope), WidgetRecipe,
# ConnectorHealth, plus the new ActionSchema fields.
#
# Also pins the DirectRESTAdapter defaults so YAML connectors keep
# satisfying the protocol unchanged: widgets() returns [], health()
# returns a CONNECTED/DISCONNECTED snapshot based on connect() state,
# actions() reads execution_mode + requires_binary from YAML rows.

from __future__ import annotations

import pytest

from pocketpaw.connectors.protocol import (
    ActionSchema,
    ConnectorHealth,
    ConnectorScope,
    ConnectorStatus,
    ExecutionMode,
    PocketScope,
    UserScope,
    WidgetRecipe,
    WorkspaceScope,
)
from pocketpaw.connectors.yaml_engine import ConnectorDef, DirectRESTAdapter

# ---------------------------------------------------------------------------
# ExecutionMode enum
# ---------------------------------------------------------------------------


def test_execution_mode_values():
    """Three modes; values match the wire format the cloud router uses."""
    assert ExecutionMode.CLOUD.value == "cloud"
    assert ExecutionMode.LOCAL.value == "local"
    assert ExecutionMode.SANDBOX.value == "sandbox"


def test_execution_mode_round_trip_from_string():
    """YAML connectors specify mode as a string; the enum round-trips."""
    assert ExecutionMode("cloud") is ExecutionMode.CLOUD
    assert ExecutionMode("local") is ExecutionMode.LOCAL
    assert ExecutionMode("sandbox") is ExecutionMode.SANDBOX


# ---------------------------------------------------------------------------
# ActionSchema additions — execution_mode + requires_binary
# ---------------------------------------------------------------------------


def test_action_schema_defaults_to_cloud_mode():
    """YAML connectors get cloud mode by default — no behaviour change."""
    s = ActionSchema(name="search")
    assert s.execution_mode is ExecutionMode.CLOUD
    assert s.requires_binary is None


def test_action_schema_can_declare_local_mode_with_binary():
    """CLI adapters declare local mode + the binary they shell out to."""
    s = ActionSchema(
        name="apps_list",
        execution_mode=ExecutionMode.LOCAL,
        requires_binary="firebase",
    )
    assert s.execution_mode is ExecutionMode.LOCAL
    assert s.requires_binary == "firebase"


# ---------------------------------------------------------------------------
# ConnectorScope tagged union
# ---------------------------------------------------------------------------


def test_pocket_scope_carries_pocket_id():
    s = PocketScope(pocket_id="p-1", workspace_id="ws-1")
    assert s.kind == "pocket"
    assert s.pocket_id == "p-1"
    assert s.workspace_id == "ws-1"


def test_workspace_scope_requires_workspace_id():
    s = WorkspaceScope(workspace_id="ws-1")
    assert s.kind == "workspace"
    assert s.workspace_id == "ws-1"


def test_user_scope_carries_user_and_workspace():
    s = UserScope(user_id="u-1", workspace_id="ws-1")
    assert s.kind == "user"
    assert s.user_id == "u-1"
    assert s.workspace_id == "ws-1"


def test_connector_scope_is_a_union():
    """All three scope variants are valid ConnectorScope instances."""
    scopes: list[ConnectorScope] = [
        PocketScope(pocket_id="p-1"),
        WorkspaceScope(workspace_id="ws-1"),
        UserScope(user_id="u-1"),
    ]
    kinds = [s.kind for s in scopes]
    assert kinds == ["pocket", "workspace", "user"]


def test_scope_is_frozen():
    """Scope value objects are immutable — pattern from ee/cloud rule §3."""
    s = WorkspaceScope(workspace_id="ws-1")
    with pytest.raises((AttributeError, Exception)):
        s.workspace_id = "ws-2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WidgetRecipe + ConnectorHealth shapes
# ---------------------------------------------------------------------------


def test_widget_recipe_minimal_construction():
    """Minimum recipe — title + display_type + action."""
    r = WidgetRecipe(title="Inbox", display_type="feed", action="search")
    assert r.title == "Inbox"
    assert r.display_type == "feed"
    assert r.action == "search"
    assert r.params == {}
    assert r.default_size == "col-1 row-1"


def test_widget_recipe_full_construction():
    r = WidgetRecipe(
        title="Important Emails",
        display_type="feed",
        action="search",
        params={"q": "is:important", "max": 10},
        default_size="col-1 row-2",
        description="Last 10 important threads",
    )
    assert r.params == {"q": "is:important", "max": 10}
    assert r.default_size == "col-1 row-2"
    assert r.description == "Last 10 important threads"


def test_connector_health_defaults():
    h = ConnectorHealth(ok=True, status=ConnectorStatus.CONNECTED)
    assert h.ok is True
    assert h.status is ConnectorStatus.CONNECTED
    assert h.message == ""
    assert h.checked_at_ms == 0


# ---------------------------------------------------------------------------
# DirectRESTAdapter — protocol additions land as no-op defaults
# ---------------------------------------------------------------------------


def _stripe_def() -> ConnectorDef:
    return ConnectorDef(
        name="stripe",
        display_name="Stripe",
        type="data",
        icon="dollar-sign",
        auth={"method": "bearer", "credentials": [{"name": "API_KEY"}]},
        actions=[
            {"name": "list_invoices", "description": "List invoices", "method": "GET"},
            # CLI-style action declared in YAML — protocol PR-2 reads this
            {
                "name": "deploy",
                "description": "Run firebase deploy",
                "method": "LOCAL",
                "execution_mode": "local",
                "requires_binary": "firebase",
            },
        ],
    )


@pytest.mark.asyncio
async def test_yaml_widgets_default_empty():
    """YAML connectors don't ship widget recipes in Phase 1."""
    a = DirectRESTAdapter(_stripe_def())
    assert await a.widgets() == []


@pytest.mark.asyncio
async def test_yaml_health_reflects_connection_state():
    """health() returns CONNECTED when connect() succeeded, DISCONNECTED otherwise."""
    a = DirectRESTAdapter(_stripe_def())

    h_before = await a.health()
    assert h_before.ok is False
    assert h_before.status is ConnectorStatus.DISCONNECTED

    await a.connect("workspace:ws-1", {"API_KEY": "sk_test_x"})
    h_after = await a.health()
    assert h_after.ok is True
    assert h_after.status is ConnectorStatus.CONNECTED


@pytest.mark.asyncio
async def test_yaml_actions_carry_execution_mode_and_requires_binary():
    """YAML actions can opt into local mode + declare a required binary."""
    a = DirectRESTAdapter(_stripe_def())
    schemas = await a.actions()
    by_name = {s.name: s for s in schemas}

    cloud_action = by_name["list_invoices"]
    assert cloud_action.execution_mode is ExecutionMode.CLOUD
    assert cloud_action.requires_binary is None

    local_action = by_name["deploy"]
    assert local_action.execution_mode is ExecutionMode.LOCAL
    assert local_action.requires_binary == "firebase"


@pytest.mark.asyncio
async def test_unknown_execution_mode_falls_back_to_cloud():
    """Garbage in YAML doesn't break the runtime — defaults to cloud."""
    defn = ConnectorDef(
        name="weird",
        display_name="Weird",
        type="data",
        icon="?",
        auth={},
        actions=[{"name": "x", "execution_mode": "wormhole"}],
    )
    a = DirectRESTAdapter(defn)
    schemas = await a.actions()
    assert schemas[0].execution_mode is ExecutionMode.CLOUD
