# test_registry_definitions.py — connector-store-unification CS-2 — truthful
# status + two-dir definition scan.
# Created: 2026-06-12 — Locks: status() derives "connected" from durable state
#   (a fresh registry instance reports the same status as the one that
#   connected); home-dir YAML pickup (~/.pocketpaw/connectors); CWD precedence
#   on name collision; orphan state rows surface as definition_missing without
#   crashing; get_definition rescans on miss.
# Updated: 2026-06-12 (PR #1447 review fixes) — connect() routes through
#   get_definition, so a YAML dropped in after registry construction is
#   connectable, not just visible to detail/status.

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.connectors.protocol import ConnectorStatus
from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.state_store import FileConnectorStateStore

_YAML_TEMPLATE = """\
name: {name}
display_name: {display_name}
type: rest
icon: plug
auth:
  type: api_key
  credentials:
    - name: api_key
      required: true
actions:
  - name: ping
    method: GET
    url: https://example.invalid/ping
"""


def _write_yaml(directory: Path, name: str, display_name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(
        _YAML_TEMPLATE.format(name=name, display_name=display_name)
    )


@pytest.fixture
def defs_dir(tmp_path) -> Path:
    d = tmp_path / "defs"
    _write_yaml(d, "testsvc", "Test Service")
    return d


@pytest.fixture
def home_dir(tmp_path) -> Path:
    d = tmp_path / "home-defs"
    d.mkdir()
    return d


@pytest.fixture
def store(tmp_path) -> FileConnectorStateStore:
    return FileConnectorStateStore(base_dir=tmp_path / "state")


def _registry(defs_dir: Path, store: FileConnectorStateStore, home_dir: Path) -> ConnectorRegistry:
    return ConnectorRegistry(defs_dir, state_store=store, home_connectors_dir=home_dir)


def _status_of(reg: ConnectorRegistry, pocket_id: str, name: str):
    return next(s for s in reg.status(pocket_id) if s["name"] == name)


class TestDurableStatus:
    @pytest.mark.asyncio
    async def test_status_survives_fresh_instance(self, defs_dir, store, home_dir) -> None:
        reg_a = _registry(defs_dir, store, home_dir)
        result = await reg_a.connect("default", "testsvc", {"api_key": "k1"})
        assert result.success is True
        assert _status_of(reg_a, "default", "testsvc")["status"] == ConnectorStatus.CONNECTED

        # Fresh instance, same store — simulated restart. No adapter is live,
        # yet status still reports connected because the config is persisted.
        reg_b = _registry(defs_dir, store, home_dir)
        assert reg_b.get_adapter("default", "testsvc") is None
        assert _status_of(reg_b, "default", "testsvc")["status"] == ConnectorStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_status_is_scope_isolated(self, defs_dir, store, home_dir) -> None:
        reg = _registry(defs_dir, store, home_dir)
        await reg.connect("alpha", "testsvc", {"api_key": "k1"})
        assert _status_of(reg, "alpha", "testsvc")["status"] == ConnectorStatus.CONNECTED
        assert _status_of(reg, "beta", "testsvc")["status"] == ConnectorStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_reports_disconnected_everywhere(
        self, defs_dir, store, home_dir
    ) -> None:
        reg_a = _registry(defs_dir, store, home_dir)
        await reg_a.connect("default", "testsvc", {"api_key": "k1"})
        await reg_a.disconnect("default", "testsvc")
        assert _status_of(reg_a, "default", "testsvc")["status"] == ConnectorStatus.DISCONNECTED

        reg_b = _registry(defs_dir, store, home_dir)
        assert _status_of(reg_b, "default", "testsvc")["status"] == ConnectorStatus.DISCONNECTED

    def test_empty_store_reads_disconnected(self, defs_dir, store, home_dir) -> None:
        reg = _registry(defs_dir, store, home_dir)
        assert _status_of(reg, "default", "testsvc")["status"] == ConnectorStatus.DISCONNECTED


class TestOrphanRows:
    def test_orphan_row_surfaces_as_definition_missing(self, defs_dir, store, home_dir) -> None:
        store.set("ghost", "default", {"api_key": "k"})
        reg = _registry(defs_dir, store, home_dir)
        row = _status_of(reg, "default", "ghost")
        assert row["status"] == ConnectorStatus.DEFINITION_MISSING
        assert row["display_name"] == "ghost"
        # The defined connector is unaffected.
        assert _status_of(reg, "default", "testsvc")["status"] == ConnectorStatus.DISCONNECTED

    def test_orphan_recovers_when_definition_lands(self, defs_dir, store, home_dir) -> None:
        store.set("latecomer", "default", {"api_key": "k"})
        reg = _registry(defs_dir, store, home_dir)
        assert _status_of(reg, "default", "latecomer")["status"] == (
            ConnectorStatus.DEFINITION_MISSING
        )
        # Definition shows up after registry construction — the orphan check
        # rescans before declaring rows orphaned, so status heals itself.
        _write_yaml(defs_dir, "latecomer", "Latecomer")
        assert _status_of(reg, "default", "latecomer")["status"] == ConnectorStatus.CONNECTED


class TestDefinitionScan:
    def test_home_dir_yaml_pickup(self, defs_dir, store, home_dir) -> None:
        _write_yaml(home_dir, "homesvc", "Home Service")
        reg = _registry(defs_dir, store, home_dir)
        defn = reg.get_definition("homesvc")
        assert defn is not None
        assert defn.display_name == "Home Service"
        assert "homesvc" in [c["name"] for c in reg.available]

    def test_cwd_wins_on_name_collision(self, defs_dir, store, home_dir) -> None:
        _write_yaml(home_dir, "testsvc", "Home Override")
        reg = _registry(defs_dir, store, home_dir)
        defn = reg.get_definition("testsvc")
        assert defn is not None
        # The CWD definition (display_name "Test Service") beats the home one.
        assert defn.display_name == "Test Service"
        # And the name appears once, not twice.
        names = [c["name"] for c in reg.available]
        assert names.count("testsvc") == 1

    def test_get_definition_reloads_on_miss(self, defs_dir, store, home_dir) -> None:
        reg = _registry(defs_dir, store, home_dir)
        assert reg.get_definition("latesvc") is None
        _write_yaml(defs_dir, "latesvc", "Late Service")
        defn = reg.get_definition("latesvc")
        assert defn is not None
        assert defn.display_name == "Late Service"

    @pytest.mark.asyncio
    async def test_connect_picks_up_late_definition(self, defs_dir, store, home_dir) -> None:
        """connect() routes through the rescan-on-miss path too — drop a YAML,
        then /connect, without a restart in between."""
        reg = _registry(defs_dir, store, home_dir)
        _write_yaml(defs_dir, "latesvc", "Late Service")
        result = await reg.connect("default", "latesvc", {"api_key": "k1"})
        assert result is not None
        assert result.success is True
        assert _status_of(reg, "default", "latesvc")["status"] == ConnectorStatus.CONNECTED

    def test_missing_dirs_scan_to_empty(self, tmp_path, store) -> None:
        reg = ConnectorRegistry(
            tmp_path / "absent",
            state_store=store,
            home_connectors_dir=tmp_path / "also-absent",
        )
        assert reg.available == []
