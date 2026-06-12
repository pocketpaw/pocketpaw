# test_registry_state.py — connector-store-unification CS-1 — registry + state store.
# Created: 2026-06-12 — Locks the restart-survival contract: connect() is
#   write-through to the state store; a second registry sharing the same store
#   (a simulated process restart) can ensure_connected + execute with no new
#   /connect; a config change disconnects and reconnects the live adapter; a
#   failed connect rolls the persisted row back; an empty store keeps behavior
#   identical to the pre-store registry.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import pocketpaw.connectors.registry as registry_module
from pocketpaw.connectors.protocol import ActionResult, ConnectionResult, ConnectorStatus
from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.state_store import FileConnectorStateStore

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


class FakeAdapter:
    """Minimal AnyAdapter that records connect/disconnect/execute calls."""

    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, dict[str, Any]]] = []
        self.disconnect_calls: list[str] = []
        self.fail_connect = False

    @property
    def name(self) -> str:
        return "testsvc"

    @property
    def display_name(self) -> str:
        return "Test Service"

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        self.connect_calls.append((pocket_id, config))
        if self.fail_connect:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message="bad credentials",
            )
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message="connected",
        )

    async def disconnect(self, pocket_id: str) -> bool:
        self.disconnect_calls.append(pocket_id)
        return True

    async def actions(self) -> list:
        return []

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        return ActionResult(success=True, data={"action": action})


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
def fake_adapters(monkeypatch) -> list[FakeAdapter]:
    """Route testsvc through FakeAdapter; returns the created instances."""
    created: list[FakeAdapter] = []

    def _fake_create(connector_name: str):
        if connector_name != "testsvc":
            return None
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    monkeypatch.setattr(registry_module, "_create_native_adapter", _fake_create)
    return created


def _registry(defs_dir: Path, store: FileConnectorStateStore) -> ConnectorRegistry:
    return ConnectorRegistry(defs_dir, state_store=store)


class TestRestartSurvival:
    @pytest.mark.asyncio
    async def test_ensure_connected_and_execute_on_fresh_registry(
        self, defs_dir, store, fake_adapters
    ) -> None:
        """Registry A connects; registry B (same store, fresh process) executes
        with no /connect of its own."""
        reg_a = _registry(defs_dir, store)
        result = await reg_a.connect("default", "testsvc", {"api_key": "k1"})
        assert result.success is True

        reg_b = _registry(defs_dir, store)  # simulated restart
        assert reg_b.get_adapter("default", "testsvc") is None

        adapter = await reg_b.ensure_connected("testsvc", "default")
        assert adapter is not None
        # Reconnected from the persisted config, not from anything in-process.
        assert adapter.connect_calls == [("default", {"api_key": "k1"})]

        executed = await adapter.execute("ping", {})
        assert executed.success is True

    @pytest.mark.asyncio
    async def test_ensure_connected_is_noop_when_live(self, defs_dir, store, fake_adapters) -> None:
        reg = _registry(defs_dir, store)
        await reg.connect("default", "testsvc", {"api_key": "k1"})

        first = await reg.ensure_connected("testsvc", "default")
        second = await reg.ensure_connected("testsvc", "default")
        assert first is second
        # One connect from connect(); ensure_connected added none.
        assert len(first.connect_calls) == 1

    @pytest.mark.asyncio
    async def test_ensure_connected_without_persisted_config(
        self, defs_dir, store, fake_adapters
    ) -> None:
        reg = _registry(defs_dir, store)
        assert await reg.ensure_connected("testsvc", "default") is None

    @pytest.mark.asyncio
    async def test_ensure_connected_keeps_config_on_transient_failure(
        self, defs_dir, store, fake_adapters, monkeypatch
    ) -> None:
        reg_a = _registry(defs_dir, store)
        await reg_a.connect("default", "testsvc", {"api_key": "k1"})

        reg_b = _registry(defs_dir, store)
        # Next adapter built will refuse to connect (service down).
        original = registry_module._create_native_adapter

        def _failing_create(connector_name: str):
            adapter = original(connector_name)
            if adapter is not None:
                adapter.fail_connect = True
            return adapter

        monkeypatch.setattr(registry_module, "_create_native_adapter", _failing_create)
        assert await reg_b.ensure_connected("testsvc", "default") is None
        # The persisted config must survive so a later retry can succeed.
        assert store.get("testsvc", "default") == {"api_key": "k1"}


class TestWriteThrough:
    @pytest.mark.asyncio
    async def test_connect_persists_config(self, defs_dir, store, fake_adapters) -> None:
        reg = _registry(defs_dir, store)
        await reg.connect("default", "testsvc", {"api_key": "k1"})
        assert store.get("testsvc", "default") == {"api_key": "k1"}

    @pytest.mark.asyncio
    async def test_failed_connect_rolls_back_state(
        self, defs_dir, store, fake_adapters, monkeypatch
    ) -> None:
        original = registry_module._create_native_adapter

        def _failing_create(connector_name: str):
            adapter = original(connector_name)
            if adapter is not None:
                adapter.fail_connect = True
            return adapter

        monkeypatch.setattr(registry_module, "_create_native_adapter", _failing_create)
        reg = _registry(defs_dir, store)
        result = await reg.connect("default", "testsvc", {"api_key": "bad"})
        assert result.success is False
        assert store.get("testsvc", "default") is None

    @pytest.mark.asyncio
    async def test_config_change_disconnects_then_reconnects(
        self, defs_dir, store, fake_adapters
    ) -> None:
        reg = _registry(defs_dir, store)
        await reg.connect("default", "testsvc", {"api_key": "k1"})
        old_adapter = reg.get_adapter("default", "testsvc")

        await reg.connect("default", "testsvc", {"api_key": "k2"})
        new_adapter = reg.get_adapter("default", "testsvc")

        assert old_adapter.disconnect_calls == ["default"]
        assert new_adapter is not old_adapter
        assert new_adapter.connect_calls == [("default", {"api_key": "k2"})]
        assert store.get("testsvc", "default") == {"api_key": "k2"}

    @pytest.mark.asyncio
    async def test_reconnect_same_config_does_not_disconnect(
        self, defs_dir, store, fake_adapters
    ) -> None:
        """Identical config keeps today's overwrite path — no disconnect."""
        reg = _registry(defs_dir, store)
        await reg.connect("default", "testsvc", {"api_key": "k1"})
        old_adapter = reg.get_adapter("default", "testsvc")

        await reg.connect("default", "testsvc", {"api_key": "k1"})
        assert old_adapter.disconnect_calls == []

    @pytest.mark.asyncio
    async def test_disconnect_forgets_persisted_config(
        self, defs_dir, store, fake_adapters
    ) -> None:
        reg = _registry(defs_dir, store)
        await reg.connect("default", "testsvc", {"api_key": "k1"})
        assert await reg.disconnect("default", "testsvc") is True
        assert store.get("testsvc", "default") is None
        # And a fresh registry can no longer resurrect the connection.
        reg_b = _registry(defs_dir, store)
        assert await reg_b.ensure_connected("testsvc", "default") is None


class TestEmptyStoreRegression:
    """With an empty store the registry behaves exactly like the pre-store one."""

    @pytest.mark.asyncio
    async def test_unknown_connector_returns_none(self, defs_dir, store) -> None:
        reg = _registry(defs_dir, store)
        assert await reg.connect("default", "nope", {}) is None

    @pytest.mark.asyncio
    async def test_no_adapter_until_connect(self, defs_dir, store) -> None:
        reg = _registry(defs_dir, store)
        assert reg.get_adapter("default", "testsvc") is None
        assert await reg.disconnect("default", "testsvc") is False
