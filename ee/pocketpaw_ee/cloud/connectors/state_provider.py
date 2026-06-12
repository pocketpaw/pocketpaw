# Cloud connector state store — durable connector config from WorkspaceConnector docs.
# Created: 2026-06-12 (connector-store-unification CS-3) — The OSS registry's
#   restart-survival seam (ConnectorStateStore) backed by the cloud DB instead
#   of ~/.pocketpaw files. ``registry.ensure_connected`` on a fresh process
#   reads the connector's config straight from the ``WorkspaceConnector``
#   Beanie doc, so a cloud execute succeeds with no prior /connect call.
#   Discovered by core via the ``pocketpaw.connector_state_stores``
#   entry-point (see ``CloudConnectorStateStoreProvider`` in
#   ``pocketpaw_ee/extensions.py``); core never imports this module.
#
# Scope-key namespacing (the contract with ``connectors/service.py``):
#   ``ws:<workspace_id>``     — the workspace's row for a connector name
#                               (workspace tenancy filter; scope-agnostic to
#                               match the legacy execute() doc lookup).
#   ``pocket:<pocket_id>``    — the row bound to one pocket (scope=="pocket").
#                               Callers MUST validate the pocket belongs to
#                               the caller's workspace before composing this
#                               key (service.execute gates with
#                               ``is_connector_bound_to_pocket``).
#   anything else             — delegated to the file store, so the
#                               single-tenant OSS surfaces (/api/v1/connectors,
#                               agent connector tools) keep their exact
#                               behavior in a cloud install.
#
# Ownership: the WorkspaceConnector row LIFECYCLE belongs to
# ``connectors/service.py`` (enable/disable/update_config). This module is the
# read-mostly sister seam of the same entity: ``get`` reads config, ``set``
# only mirrors config onto an EXISTING row, ``delete`` is a deliberate no-op
# for namespaced keys (a registry-level disconnect must drop the live adapter
# without destroying service-owned durable state). ``list`` is sync (the
# registry's sync ``status()`` calls it) and returns the file-delegate rows
# only — cloud rows are tenant-scoped and surface through the cloud DTOs.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.connectors.state_store import FileConnectorStateStore

logger = logging.getLogger(__name__)

_WS_PREFIX = "ws:"
_POCKET_PREFIX = "pocket:"


def _parse_scope_key(scope_key: str) -> tuple[str, str] | None:
    """Split a namespaced scope key into ``(kind, ident)``, or ``None``.

    Non-namespaced keys (the OSS single-tenant path) return ``None`` and are
    delegated to the file store.
    """
    if scope_key.startswith(_WS_PREFIX):
        return ("ws", scope_key[len(_WS_PREFIX) :])
    if scope_key.startswith(_POCKET_PREFIX):
        return ("pocket", scope_key[len(_POCKET_PREFIX) :])
    return None


class CloudConnectorStateStore:
    """``ConnectorStateStore`` backed by the ``WorkspaceConnector`` Beanie doc.

    ``get``/``set``/``delete`` are async for namespaced keys (the registry
    awaits awaitable results); non-namespaced keys delegate to the sync file
    store. Every Beanie failure is soft — a registry built where the cloud DB
    isn't initialized (OSS tests, partial installs) degrades to "no persisted
    cloud config" instead of crashing connector support.
    """

    def __init__(self, file_fallback: FileConnectorStateStore | None = None) -> None:
        self._file = file_fallback or FileConnectorStateStore()

    # -- internals ----------------------------------------------------------

    async def _find_doc(self, name: str, kind: str, ident: str) -> Any | None:
        """Resolve the enabled WorkspaceConnector row for a namespaced key."""
        from pocketpaw_ee.cloud.models.connector import WorkspaceConnector as _WCDoc

        if kind == "ws":
            # Workspace key: tenancy filter only, scope-agnostic — mirrors the
            # legacy execute() lookup (workspace + name), so a pocket- or
            # user-scoped row still rehydrates a workspace-keyed execute.
            return await _WCDoc.find_one(
                _WCDoc.workspace == ident,
                _WCDoc.name == name,
                _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
            )
        return await _WCDoc.find_one(
            _WCDoc.pocket_id == ident,
            _WCDoc.scope == "pocket",
            _WCDoc.name == name,
            _WCDoc.enabled == True,  # noqa: E712 — Beanie expects ==
        )

    # -- ConnectorStateStore --------------------------------------------------

    def get(self, name: str, scope_key: str) -> Any:
        parsed = _parse_scope_key(scope_key)
        if parsed is None:
            return self._file.get(name, scope_key)
        return self._get_cloud(name, *parsed)

    async def _get_cloud(self, name: str, kind: str, ident: str) -> dict[str, Any] | None:
        try:
            doc = await self._find_doc(name, kind, ident)
        except Exception as exc:  # noqa: BLE001 — degrade, don't break connectors
            logger.warning("cloud connector state read failed for %s: %s", name, exc)
            return None
        if doc is None:
            return None
        # An enabled row with empty config is still a valid binding (CLI/no-cred
        # connectors) — return the dict, never coerce {} to None.
        return dict(doc.config)

    def set(self, name: str, scope_key: str, config: dict[str, Any]) -> Any:
        parsed = _parse_scope_key(scope_key)
        if parsed is None:
            return self._file.set(name, scope_key, config)
        return self._set_cloud(name, *parsed, config=config)

    async def _set_cloud(self, name: str, kind: str, ident: str, *, config: dict[str, Any]) -> None:
        """Mirror config onto an existing row — never create one.

        Row creation (with its scope/tenancy validation) belongs to
        ``service.enable_connector``; a registry-seam write for a key with no
        row is logged and skipped so the seam can't conjure tenant state.
        """
        try:
            doc = await self._find_doc(name, kind, ident)
            if doc is None:
                logger.warning(
                    "no WorkspaceConnector row for %s (%s:%s) — rows are created via "
                    "enable_connector; skipping registry config write",
                    name,
                    kind,
                    ident,
                )
                return
            doc.config = dict(config)
            # no-event: registry-seam config mirror of a row the connectors
            # service owns; service-level writes emit their own events.
            await doc.save()
        except Exception as exc:  # noqa: BLE001 — degrade, don't break connectors
            logger.warning("cloud connector state write failed for %s: %s", name, exc)

    def delete(self, name: str, scope_key: str) -> Any:
        parsed = _parse_scope_key(scope_key)
        if parsed is None:
            return self._file.delete(name, scope_key)
        # Deliberate no-op for namespaced keys: the WorkspaceConnector row
        # lifecycle is owned by enable/disable_connector. A registry-level
        # disconnect() against a cloud key only needs the LIVE adapter dropped;
        # destroying the durable row here would let a registry rollback wipe
        # service-owned state.
        logger.debug("skipping registry delete for cloud connector row %s (%s)", name, scope_key)
        return None

    def list(self) -> list[tuple[str, str]]:
        # Sync by contract (the registry's sync status() calls it). Cloud rows
        # are tenant-scoped and never match a single-tenant pocket filter, so
        # they are intentionally not enumerated here — the cloud status/list
        # DTOs derive from WorkspaceConnector docs in connectors/service.py.
        return self._file.list()
