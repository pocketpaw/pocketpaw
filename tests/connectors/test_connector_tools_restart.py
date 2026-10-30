# test_connector_tools_restart.py — connector-store-unification (PR-A review
#   follow-up) — agent-tool execute parity with the HTTP router.
# Created: 2026-06-12 — Locks: ConnectorExecuteTool reconnects from persisted
#   state on a fresh process (registry B shares registry A's state store, no
#   connector_connect in session B), exactly like /connectors/execute. Before
#   the fix the tool used get_adapter and failed with "not connected" after
#   every restart while the HTTP path succeeded.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import pocketpaw.connectors.registry as registry_module
import pocketpaw.tools.builtin.connector_tools as connector_tools
from pocketpaw.connectors.protocol import ActionResult, ConnectionResult, ConnectorStatus
from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.state_store import FileConnectorStateStore
from pocketpaw.tools.builtin.connector_tools import ConnectorExecuteTool

_TEST_YAML = """\
name: testsvc
display_name: Test Service
type: rest
icon: plug
auth:
  type: api_key
  credentials:
    - name: api_key
      required: true
actions:
  - name: ping
    description: Ping the service
    method: GET
    url: https://example.invalid/ping
"""


class _FakeAdapter:
    """Minimal adapter recording connect calls; execute returns a dict."""

    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "testsvc"

    @property
    def display_name(self) -> str:
        return "Test Service"

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        self.connect_calls.append((pocket_id, config))
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message="connected",
        )

    async def disconnect(self, pocket_id: str) -> bool:
        return True

    async def actions(self) -> list:
        return []

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        return ActionResult(success=True, data={"action": action}, records_affected=1)


@pytest.fixture
def defs_dir(tmp_path) -> Path:
    d = tmp_path / "defs"
    d.mkdir()
    (d / "testsvc.yaml").write_text(_TEST_YAML)
    return d


@pytest.fixture
def store(tmp_path) -> FileConnectorStateStore:
    return FileConnectorStateStore(base_dir=tmp_path / "state")


@pytest.fixture
def fake_adapters(monkeypatch) -> list[_FakeAdapter]:
    created: list[_FakeAdapter] = []

    def _fake_create(connector_name: str):
        if connector_name != "testsvc":
            return None
        adapter = _FakeAdapter()
        created.append(adapter)
        return adapter

    monkeypatch.setattr(registry_module, "_create_native_adapter", _fake_create)
    return created


@pytest.mark.asyncio
async def test_execute_tool_reconnects_after_restart(
    defs_dir, store, fake_adapters, monkeypatch
) -> None:
    """Session A connects; session B (fresh registry, same store) executes via
    the agent tool with NO connector_connect — parity with the HTTP router."""
    reg_a = ConnectorRegistry(defs_dir, state_store=store)
    result = await reg_a.connect("default", "testsvc", {"api_key": "k1"})
    assert result.success is True

    # Simulated restart: the tool module's singleton is a fresh registry that
    # only shares the durable store.
    reg_b = ConnectorRegistry(defs_dir, state_store=store)
    assert reg_b.get_adapter("default", "testsvc") is None
    monkeypatch.setattr(connector_tools, "_registry", reg_b)

    out = await ConnectorExecuteTool().execute("testsvc", "ping", {})

    assert "not connected" not in out
    assert '"action": "ping"' in out
    # The reconnect came from the persisted config, lazily.
    assert reg_b.get_adapter("default", "testsvc") is not None
    assert fake_adapters[-1].connect_calls == [("default", {"api_key": "k1"})]


@pytest.mark.asyncio
async def test_execute_tool_without_persisted_state_still_errors(
    defs_dir, store, fake_adapters, monkeypatch
) -> None:
    """No live adapter AND no persisted config keeps the clear error message."""
    reg = ConnectorRegistry(defs_dir, state_store=store)
    monkeypatch.setattr(connector_tools, "_registry", reg)

    out = await ConnectorExecuteTool().execute("testsvc", "ping", {})

    assert "not connected" in out
