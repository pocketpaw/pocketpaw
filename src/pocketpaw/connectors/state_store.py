# Connector state store — durable connector config at ~/.pocketpaw/connectors/state/.
# Created: 2026-06-12 (connector-store-unification CS-1) — Connector lifecycle
#   must survive process restarts. The ConnectorRegistry previously held adapter
#   config only in memory, so every restart silently dropped all connections
#   until someone re-ran /connect. This module is the durable layer: a small
#   Protocol (so EE can swap in a DB-backed store) plus the file-backed default.
#   Naming/sanitization mirrors clients/token_store.py — sanitized human-readable
#   prefix + short sha256 of the raw value, so distinct keys that sanitize to the
#   same prefix never share a file and no key can path-traverse out of the dir.
#   Files are chmod 0600 (config may carry credentials, same posture as the
#   OAuth token store).
# Updated: 2026-06-12 (connector-store-unification CS-3) — Protocol docs note
#   that implementations may make get/set/delete async (the registry awaits
#   awaitable results via _maybe_await); list must stay sync. Lets the EE
#   WorkspaceConnector-backed store satisfy the same seam.

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pocketpaw.config import get_config_dir

logger = logging.getLogger(__name__)

# Characters safe to use verbatim in an on-disk filename segment. Anything else
# (e.g. ``@``, ``/``, ``..``) is replaced so a connector name or scope key can
# never escape the state dir or collide with the segment separator.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# Separator between the connector-name segment and the scope-key segment.
# Double underscore keeps a single underscore inside either value from being
# mistaken for the boundary (same convention as token_store).
_SEP = "__"


def _default_state_dir() -> Path:
    """Default on-disk location for connector state.

    Resolved lazily (not at construction) so tests can patch this function
    and already-built stores pick up the override.
    """
    return get_config_dir() / "connectors" / "state"


def _segment(value: str) -> str:
    """Build a filesystem-safe, collision-resistant segment for a key part.

    Sanitized, human-readable prefix for debuggability, plus a short hash of
    the *raw* value so two distinct values that sanitize to the same prefix
    (``a@x.com`` vs ``a/x.com``) never share a file.
    """
    safe = _SAFE_CHARS.sub("-", value).strip("-") or "x"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


@runtime_checkable
class ConnectorStateStore(Protocol):
    """Durable store for connector config, keyed by (name, scope_key).

    ``scope_key`` is the registry's scoping segment — the pocket_id on the
    OSS path, a namespaced ``ws:<workspace_id>`` / ``pocket:<pocket_id>``
    key on the cloud path. Implementations must treat both key parts as
    untrusted input.

    Implementations may make ``get``/``set``/``delete`` async (return an
    awaitable) — every registry call site on an async path awaits awaitable
    results (see ``ConnectorRegistry._maybe_await``). ``list`` must stay
    sync: it is called from the registry's sync ``status()``.
    """

    def get(self, name: str, scope_key: str) -> dict[str, Any] | None:
        """Return the persisted config for (name, scope_key), or None."""
        ...

    def set(self, name: str, scope_key: str, config: dict[str, Any]) -> None:
        """Persist config for (name, scope_key), overwriting any prior row."""
        ...

    def delete(self, name: str, scope_key: str) -> None:
        """Remove the row for (name, scope_key). No-op if absent."""
        ...

    def list(self) -> list[tuple[str, str]]:
        """Return all persisted (name, scope_key) pairs."""
        ...


class FileConnectorStateStore:
    """File-backed ConnectorStateStore at ``~/.pocketpaw/connectors/state/``.

    One JSON file per (name, scope_key): ``{name}__{scope_key}.json`` with
    both segments sanitized + hash-suffixed (see :func:`_segment`). The raw
    name and scope_key are stored inside the payload so :meth:`list` can
    return the originals — the hashed filename is not reversible.

    Files are chmod 0600; the directory 0700. ``base_dir`` overrides the
    default location (tests point it at tmp_path).
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir_override = base_dir

    # -- internals ----------------------------------------------------------

    def _dir(self, *, create: bool = False) -> Path:
        d = self._base_dir_override or _default_state_dir()
        if create:
            d.mkdir(parents=True, exist_ok=True)
            try:
                d.chmod(0o700)
            except OSError:
                pass  # Windows / exotic filesystems
        return d

    def _path(self, name: str, scope_key: str) -> Path:
        return self._dir() / f"{_segment(name)}{_SEP}{_segment(scope_key)}.json"

    # -- ConnectorStateStore ------------------------------------------------

    def get(self, name: str, scope_key: str) -> dict[str, Any] | None:
        path = self._path(name, scope_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            config = data.get("config")
            return config if isinstance(config, dict) else None
        except Exception as e:
            logger.warning("Failed to load connector state for %s: %s", name, e)
            return None

    def set(self, name: str, scope_key: str, config: dict[str, Any]) -> None:
        self._dir(create=True)
        path = self._path(name, scope_key)
        payload = {"name": name, "scope_key": scope_key, "config": config}
        path.write_text(json.dumps(payload, indent=2))
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        logger.info("Persisted connector state for %s (scope %s)", name, scope_key)

    def delete(self, name: str, scope_key: str) -> None:
        path = self._path(name, scope_key)
        if path.exists():
            path.unlink()
            logger.info("Deleted connector state for %s (scope %s)", name, scope_key)

    def list(self) -> list[tuple[str, str]]:
        d = self._dir()
        if not d.exists():
            return []
        rows: list[tuple[str, str]] = []
        for path in sorted(d.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                name = data.get("name")
                scope_key = data.get("scope_key")
                if isinstance(name, str) and isinstance(scope_key, str):
                    rows.append((name, scope_key))
            except Exception as e:
                logger.warning("Skipping unreadable connector state file %s: %s", path, e)
        return rows
