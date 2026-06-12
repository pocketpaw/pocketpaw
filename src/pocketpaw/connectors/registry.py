# Connector registry — discovers and manages available connectors.
# Created: 2026-03-27 — Scans connectors/ dir for YAML definitions.
# Updated: 2026-03-30 — Native adapter support for database connectors.
# Updated: 2026-04-01 — Added Firebase CLI adapter registration.
# Updated: 2026-04-01 — Added GCP adapter for gcloud CLI integration.
# Updated: 2026-06-08 — Added the public ``definitions`` property (full
#   ConnectorDef list incl. ``.senses``) so the EE SenseResolver can index
#   connectors by sense without reaching into the private ``_definitions``
#   dict. OSS-only change; must not import pocketpaw_ee.
# Updated: 2026-06-12 (connector-store-unification CS-1) — Connector config now
#   survives restarts. The registry takes an optional ``state_store``
#   (default: FileConnectorStateStore at ~/.pocketpaw/connectors/state/).
#   ``connect()`` is write-through: persist config, then connect the adapter
#   (rolled back on a failed connect so a bad config never reads as
#   configured); a live adapter with different config is disconnected and
#   reconnected. New ``ensure_connected(name, scope_key)`` lazily reconnects
#   from persisted config so the execute path no longer assumes a prior
#   /connect in the same process. ``disconnect()`` deletes the persisted row.
#   With an empty store, behavior is identical to before.
# Updated: 2026-06-12 (connector-store-unification CS-2) — Status tells the
#   truth + home-dir definitions. ``status()`` derives "connected" from
#   durable state (definition present + config persisted in the state store),
#   never from the in-process adapter dict, so a fresh process reports the
#   same status as the one that connected. Definition scan now reads
#   ~/.pocketpaw/connectors/*.yaml first, then CWD connectors/*.yaml — CWD
#   wins on name collision (deploys override). ``get_definition`` rescans on
#   miss. Orphan state rows whose definition is gone surface as
#   ``definition_missing`` instead of vanishing or crashing.

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorStatus,
)
from pocketpaw.connectors.state_store import ConnectorStateStore, FileConnectorStateStore
from pocketpaw.connectors.yaml_engine import ConnectorDef, DirectRESTAdapter, parse_connector_yaml


@runtime_checkable
class AnyAdapter(Protocol):
    """Union type for all adapter kinds."""

    @property
    def name(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult: ...
    async def disconnect(self, pocket_id: str) -> bool: ...
    async def actions(self) -> list[ActionSchema]: ...
    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult: ...


# Connectors handled by native Python adapters instead of YAML/REST.
# SQL databases use DatabaseAdapter, MongoDB uses MongoDBAdapter.
# CLI connectors (firebase, gcp) are subprocess-based and execute
# locally — see ee/cloud/connectors/CHARTER.md §6.2 for the local-agent
# bus dispatch the cloud router uses.
# Native communication connectors (gmail, gcalendar, gdocs, gdrive)
# wrap a stateful Python client (OAuth, MIME, etc.) and live in
# pocketpaw/connectors/adapters/.
_SQL_CONNECTORS: set[str] = {"postgresql", "mysql", "mssql", "sqlite"}
_NOSQL_CONNECTORS: set[str] = {"mongodb"}
_CLI_CONNECTORS: set[str] = {"firebase", "gcp"}
_NATIVE_COMM_CONNECTORS: set[str] = {
    "gmail",
    "gcalendar",
    "gdocs",
    "drive",
    "reddit",
    "spotify",
}  # PR-3..7


def _create_native_adapter(connector_name: str) -> AnyAdapter | None:
    """Create a native adapter for database / CLI / communication connectors."""
    if connector_name in _SQL_CONNECTORS:
        try:
            from pocketpaw.connectors.db_adapter import DatabaseAdapter

            return DatabaseAdapter(connector_name)
        except Exception:
            return None
    if connector_name in _NOSQL_CONNECTORS:
        try:
            from pocketpaw.connectors.mongo_adapter import MongoDBAdapter

            return MongoDBAdapter()
        except Exception:
            return None
    if connector_name in _CLI_CONNECTORS:
        try:
            if connector_name == "gcp":
                from pocketpaw.connectors.gcp_adapter import GCPAdapter

                return GCPAdapter()
            from pocketpaw.connectors.firebase_adapter import FirebaseAdapter

            return FirebaseAdapter()
        except Exception:
            return None
    if connector_name in _NATIVE_COMM_CONNECTORS:
        try:
            if connector_name == "gmail":
                from pocketpaw.connectors.adapters.gmail import GmailConnector

                return GmailConnector()
            if connector_name == "gcalendar":
                from pocketpaw.connectors.adapters.gcalendar import GoogleCalendarConnector

                return GoogleCalendarConnector()
            if connector_name == "gdocs":
                from pocketpaw.connectors.adapters.gdocs import GoogleDocsConnector

                return GoogleDocsConnector()
            if connector_name == "drive":
                from pocketpaw.connectors.adapters.gdrive import GoogleDriveConnector

                return GoogleDriveConnector()
            if connector_name == "reddit":
                from pocketpaw.connectors.adapters.reddit import RedditConnector

                return RedditConnector()
            if connector_name == "spotify":
                from pocketpaw.connectors.adapters.spotify import SpotifyConnector

                return SpotifyConnector()
        except Exception:
            return None
    return None


def _default_home_connectors_dir() -> Path:
    """Default home-dir location for user-installed connector definitions.

    Resolved lazily (not at construction) so tests can patch this function
    and already-built registries pick up the override.
    """
    return Path.home() / ".pocketpaw" / "connectors"


class ConnectorRegistry:
    """Discovers available connectors and manages instances per pocket."""

    def __init__(
        self,
        connectors_dir: Path | None = None,
        *,
        state_store: ConnectorStateStore | None = None,
        home_connectors_dir: Path | None = None,
    ) -> None:
        self._connectors_dir = connectors_dir or Path("connectors")
        self._home_connectors_dir = home_connectors_dir
        self._state_store: ConnectorStateStore = state_store or FileConnectorStateStore()
        self._definitions: dict[str, ConnectorDef] = {}
        self._instances: dict[str, AnyAdapter] = {}  # key = "{pocket_id}:{connector_name}"
        self._scan()

    def _scan(self) -> None:
        """Scan both connector directories for YAML definitions.

        Home dir (``~/.pocketpaw/connectors``) first, then the CWD dir —
        scanned second so it overwrites on name collision: deploys override
        user-installed definitions.
        """
        home_dir = self._home_connectors_dir or _default_home_connectors_dir()
        for directory in (home_dir, self._connectors_dir):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.yaml")):
                try:
                    defn = parse_connector_yaml(path)
                    self._definitions[defn.name] = defn
                except Exception:
                    pass  # Skip malformed YAMLs

    @property
    def available(self) -> list[dict[str, str]]:
        """List all available connector definitions."""
        return [
            {
                "name": d.name,
                "display_name": d.display_name,
                "type": d.type,
                "icon": d.icon,
            }
            for d in self._definitions.values()
        ]

    @property
    def definitions(self) -> list[ConnectorDef]:
        """All parsed connector definitions, including their ``.senses``.

        Public accessor over the internal ``_definitions`` map so consumers
        (notably the EE SenseResolver) can index connectors by sense via
        ``pocketpaw.senses.connectors_for_sense`` without reaching into the
        private dict. Returns a fresh list; mutating it does not affect the
        registry.
        """
        return list(self._definitions.values())

    def get_definition(self, name: str) -> ConnectorDef | None:
        """Get a connector definition by name, rescanning once on a miss.

        The rescan is cheap (a few dozen small YAMLs) and lets a definition
        dropped into either scan dir after registry construction resolve
        without a process restart.
        """
        defn = self._definitions.get(name)
        if defn is None:
            self.reload()
            defn = self._definitions.get(name)
        return defn

    def get_adapter(self, pocket_id: str, connector_name: str) -> AnyAdapter | None:
        """Get an active adapter instance for a pocket+connector."""
        key = f"{pocket_id}:{connector_name}"
        return self._instances.get(key)

    async def connect(self, pocket_id: str, connector_name: str, config: dict[str, Any]) -> Any:
        """Create and connect a connector adapter for a pocket.

        Write-through: the config is persisted to the state store before the
        adapter connects, so the binding survives a process restart (see
        ``ensure_connected``). A failed connect rolls the persisted row back —
        a config that never connected must not read as configured. If a live
        adapter exists with *different* config, it is disconnected first and
        reconnected with the new config.
        """
        defn = self._definitions.get(connector_name)
        if not defn:
            return None

        key = f"{pocket_id}:{connector_name}"
        if key in self._instances:
            previous = self._state_store.get(connector_name, pocket_id)
            if previous is not None and previous != config:
                await self.disconnect(pocket_id, connector_name)

        self._state_store.set(connector_name, pocket_id, config)
        result = await self._connect_adapter(pocket_id, connector_name, defn, config)
        if result is None or not result.success:
            self._state_store.delete(connector_name, pocket_id)
        return result

    async def _connect_adapter(
        self,
        pocket_id: str,
        connector_name: str,
        defn: ConnectorDef,
        config: dict[str, Any],
    ) -> ConnectionResult:
        """Build and connect an adapter; register it on success.

        Internal — never touches the state store, so the restart-recovery
        path (``ensure_connected``) can retry a transient failure without
        wiping the persisted config.
        """
        # Use native adapter if available, otherwise fall back to YAML/REST.
        adapter: AnyAdapter
        native = _create_native_adapter(connector_name)
        if native is not None:
            adapter = native
        else:
            adapter = DirectRESTAdapter(defn)

        result = await adapter.connect(pocket_id, config)

        if result.success:
            key = f"{pocket_id}:{connector_name}"
            self._instances[key] = adapter

        return result

    async def ensure_connected(self, connector_name: str, scope_key: str) -> AnyAdapter | None:
        """Return a live adapter for (connector, scope_key), reconnecting if needed.

        The execute path calls this instead of assuming a prior ``connect()``
        in the same process: if no adapter is live, the persisted config is
        read from the state store and the adapter reconnects. No-op when
        already connected (a live adapter always carries the current config —
        ``connect()`` keeps the store and the instance map in sync). Returns
        ``None`` when there is no persisted config, the definition is gone,
        or the reconnect fails; the persisted config is kept either way so a
        transient failure can be retried.
        """
        key = f"{scope_key}:{connector_name}"
        adapter = self._instances.get(key)
        if adapter is not None:
            return adapter

        config = self._state_store.get(connector_name, scope_key)
        if config is None:
            return None
        defn = self.get_definition(connector_name)
        if defn is None:
            return None

        result = await self._connect_adapter(scope_key, connector_name, defn, config)
        if not result.success:
            return None
        return self._instances.get(key)

    async def disconnect(self, pocket_id: str, connector_name: str) -> bool:
        """Disconnect a connector from a pocket and forget its persisted config."""
        key = f"{pocket_id}:{connector_name}"
        adapter = self._instances.get(key)
        if not adapter:
            return False
        await adapter.disconnect(pocket_id)
        del self._instances[key]
        self._state_store.delete(connector_name, pocket_id)
        return True

    def status(self, pocket_id: str) -> list[dict[str, Any]]:
        """Get connection status for all connectors in a pocket.

        "Connected" is derived from durable state — definition present +
        config persisted in the state store — never from the in-process
        adapter dict, so a fresh process reports the same status as the one
        that ran /connect. Persisted rows whose definition no longer resolves
        surface as ``definition_missing`` instead of disappearing.
        """
        persisted = {name for name, scope in self._state_store.list() if scope == pocket_id}
        orphans = persisted - set(self._definitions)
        if orphans:
            # A definition may have landed since the last scan — rescan once
            # before declaring rows orphaned.
            self.reload()
            orphans = persisted - set(self._definitions)

        results = []
        for name, defn in self._definitions.items():
            results.append(
                {
                    "name": name,
                    "display_name": defn.display_name,
                    "icon": defn.icon,
                    "status": ConnectorStatus.CONNECTED
                    if name in persisted
                    else ConnectorStatus.DISCONNECTED,
                }
            )
        for name in sorted(orphans):
            results.append(
                {
                    "name": name,
                    "display_name": name,
                    "icon": "plug",
                    "status": ConnectorStatus.DEFINITION_MISSING,
                }
            )
        return results

    def reload(self) -> None:
        """Re-scan the connectors directory."""
        self._definitions.clear()
        self._scan()
