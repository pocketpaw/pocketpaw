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
# Updated: 2026-06-12 (connector-store-unification CS-6) — ``disconnect()``
#   now operates on durable state, not just live adapters: it deletes the
#   persisted row even when no adapter is in memory (post-restart disconnect,
#   orphan-row cleanup). Returns True when an adapter was disconnected OR a
#   row was deleted; still False when there was nothing to forget.
# Updated: 2026-06-12 (PR #1447 review fixes) — (1) per-key asyncio.Lock
#   serializes connect()/ensure_connected() so the post-restart thundering
#   herd (two concurrent executes) can't double-connect and leak the losing
#   adapter; (2) connect() rolls the persisted row back when adapter.connect
#   *raises*, not just when it returns a failure result; (3) connect() routes
#   through get_definition() so a YAML dropped in after startup is connectable
#   without a restart (it was already visible to detail/status).
# Updated: 2026-06-12 (connector-store-unification CS-3) — The default state
#   store is now pluggable: when no explicit ``state_store`` is passed, the
#   registry asks the ``pocketpaw.connector_state_stores`` entry-point group
#   (see ``ConnectorStateStoreProvider`` in pocketpaw/extensions.py) before
#   falling back to FileConnectorStateStore — so an EE install transparently
#   backs every registry with the cloud DB. Store ``get``/``set``/``delete``
#   calls on async paths go through ``_maybe_await`` so an async (DB-backed)
#   store and the sync file store both work behind the same seam.

from __future__ import annotations

import asyncio
import inspect
import logging
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

logger = logging.getLogger(__name__)


async def _maybe_await(value: Any) -> Any:
    """Await *value* if it is awaitable, else return it as-is.

    The seam that lets one registry speak to both store shapes: the sync
    file store returns plain values; the EE cloud store's ``get``/``set``/
    ``delete`` are async (Beanie). Only ``list`` must stay sync — it is
    called from the sync ``status()``.
    """
    if inspect.isawaitable(value):
        return await value
    return value


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
    "google_drive",
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
            if connector_name == "google_drive":
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


def _provider_state_store() -> ConnectorStateStore | None:
    """State store from the ``pocketpaw.connector_state_stores`` entry-point.

    Same discovery pattern as every other extension point (see
    ``pocketpaw.extensions.ConnectorStateStoreProvider``): an OSS install
    finds no provider and returns ``None``; a cloud install returns the
    DB-backed store so connector config rehydrates from the tenant database
    after a restart. A provider that blows up is skipped — a broken plugin
    must not take connector support down with it.
    """
    from pocketpaw._registry import first

    provider = first("pocketpaw.connector_state_stores")
    if provider is None:
        return None
    try:
        return provider.get_state_store()
    except Exception as exc:  # noqa: BLE001 — isolate plugin failures
        logger.warning("connector state store provider failed, using file store: %s", exc)
        return None


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
        self._state_store: ConnectorStateStore = (
            state_store or _provider_state_store() or FileConnectorStateStore()
        )
        self._definitions: dict[str, ConnectorDef] = {}
        self._instances: dict[str, AnyAdapter] = {}  # key = "{pocket_id}:{connector_name}"
        # Per-key locks serializing the connect paths — see _lock_for().
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._scan()

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Per-(pocket, connector) lock for connect()/ensure_connected().

        Two concurrent executes for the same key right after a restart (the
        thundering-herd shape ensure_connected creates) would otherwise both
        build and connect an adapter; the last ``_instances`` write wins and
        the loser's adapter (DB engine, HTTP client) leaks without a
        disconnect. ``setdefault`` on a plain dict is safe here — it runs
        between awaits on the event loop, so no two coroutines can interleave
        inside it.
        """
        return self._connect_locks.setdefault(key, asyncio.Lock())

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
        ``ensure_connected``). A failed connect — failure result *or* raised
        exception — rolls the persisted row back: a config that never
        connected must not read as configured. If a live adapter exists with
        *different* config, it is disconnected first and reconnected with the
        new config. Serialized per key with ``ensure_connected``.

        WARNING: never pass a namespaced (``ws:``/``pocket:``) scope key as
        ``pocket_id`` — against the EE cloud store, the write-through ``set``
        would mirror this unproven config onto the service-owned
        WorkspaceConnector row, and the rollback ``delete`` no-ops there, so
        a failed connect could not undo it. Cloud rows reconnect through
        ``ensure_connected`` only; their lifecycle belongs to the connectors
        service.
        """
        defn = self.get_definition(connector_name)
        if not defn:
            return None

        key = f"{pocket_id}:{connector_name}"
        async with self._lock_for(key):
            if key in self._instances:
                previous = await _maybe_await(self._state_store.get(connector_name, pocket_id))
                if previous is not None and previous != config:
                    await self.disconnect(pocket_id, connector_name)

            await _maybe_await(self._state_store.set(connector_name, pocket_id, config))
            try:
                result = await self._connect_adapter(pocket_id, connector_name, defn, config)
            except BaseException:
                # adapter.connect blew up (or was cancelled) — the row must
                # not survive, same invariant as a failure result.
                await _maybe_await(self._state_store.delete(connector_name, pocket_id))
                raise
            if result is None or not result.success:
                await _maybe_await(self._state_store.delete(connector_name, pocket_id))
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

        The miss path is serialized per key (see ``_lock_for``): concurrent
        post-restart executes wait for the first reconnect instead of each
        building an adapter and leaking all but the last one.
        """
        key = f"{scope_key}:{connector_name}"
        adapter = self._instances.get(key)
        if adapter is not None:
            return adapter

        async with self._lock_for(key):
            # Re-check under the lock — a racing call may have connected
            # while this one waited.
            adapter = self._instances.get(key)
            if adapter is not None:
                return adapter

            config = await _maybe_await(self._state_store.get(connector_name, scope_key))
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
        """Disconnect a connector from a pocket and forget its persisted config.

        Works on durable state, not just live adapters: after a restart there
        is no adapter in memory, but the persisted row must still be deletable
        or the connector could never be disconnected again. Also the only way
        to clear an orphaned (``definition_missing``) row. Returns ``True``
        when either a live adapter was disconnected or a persisted row was
        deleted.
        """
        key = f"{pocket_id}:{connector_name}"
        adapter = self._instances.pop(key, None)
        if adapter is not None:
            await adapter.disconnect(pocket_id)
        had_state = await _maybe_await(self._state_store.get(connector_name, pocket_id)) is not None
        if had_state:
            await _maybe_await(self._state_store.delete(connector_name, pocket_id))
        return adapter is not None or had_state

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
