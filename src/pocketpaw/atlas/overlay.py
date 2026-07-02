# atlas/overlay.py — live workspace overlay + fail-closed entitlement filter
# for the atlas OS self-model (AT-5). Created: 2026-07-02 (feat/atlas-overlay).
#
# The compiled artifact is GLOBAL — the same 72 entries in every workspace.
# This module makes atlas answers reflect the CALLING workspace's reality:
#
# * ``EntitlementProvider`` — a small structural protocol (like the repo's
#   other protocols) that answers two per-context questions: which connectors
#   are CONNECTED in this context, and whether a given entry is GRANTED.
# * ``AtlasOverlay`` — applies a provider to store results at the RESULT
#   layer (never mutating the shared ``AtlasStore`` singleton's entries):
#   connector entries gain an ``available: bool`` annotation; entries whose
#   grant check fails are REMOVED (absent from search, describe, and the
#   known-ids listing — no "upgrade to see this" leakage). Search re-ranks
#   available connectors above unavailable ones at EQUAL relevance via a
#   stable re-sort on top of the store's unchanged base scoring.
# * FAIL-CLOSED: a provider that raises (or answers anything other than a
#   literal ``True``) on ``is_granted`` filters that entry; a provider that
#   raises on ``connected_connector_names`` marks every connector
#   unavailable. Grant decisions key off the provider instance built for
#   THIS run/workspace — never a module-level "is cloud mode" flag (repo
#   lesson #1570/#1574).
# * ``DefaultEntitlementProvider`` — the OSS default, built on the real
#   connector seam: ``ConnectorRegistry.status(scope_key)`` (the same
#   durable-state view ``connector_list`` reports), with the same
#   ``"default"`` scope the connector tools use; a cloud run passes its
#   ``ws:<workspace_id>`` scope instead. Nothing is entitlement-gated in
#   OSS — ``is_granted`` is always True, so OS-level entries (primitives,
#   surfaces, senses) are never filtered by default.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pocketpaw.atlas.model import AtlasEntry

if TYPE_CHECKING:
    from pocketpaw.atlas.store import AtlasStore

logger = logging.getLogger(__name__)

# Default connector scope in a single-user OSS install — the same literal
# the builtin connector tools default their ``pocket_id`` argument to.
DEFAULT_SCOPE_KEY = "default"


@runtime_checkable
class EntitlementProvider(Protocol):
    """Per-run context answering what THIS workspace can see and use.

    Structural (duck-typed) like the repo's other protocols — any object
    with these two methods works, including EE providers that consult a
    tenant's plan. Implementations should be cheap: both methods are called
    on every atlas tool invocation.
    """

    def connected_connector_names(self) -> set[str]:
        """Names of connectors CONNECTED in this context (e.g. {'stripe'})."""
        ...

    def is_granted(self, entry: AtlasEntry) -> bool:
        """Whether this context is entitled to see *entry* at all.

        Return a literal ``True`` to grant. Anything else — ``False``,
        ``None``, a raise — is treated as NOT granted by the overlay
        (fail-closed) and the entry is filtered from every answer.
        """
        ...


@dataclass(frozen=True)
class OverlaidEntry:
    """A store entry viewed through one context's overlay.

    ``available`` is a connector-only annotation: True/False for
    ``kind="connector"`` entries (connected in this context or not), None
    for every other kind. The wrapped ``entry`` is the shared store's
    object — treat it as read-only.
    """

    entry: AtlasEntry
    available: bool | None = None


def _connector_name(entry: AtlasEntry) -> str:
    """The registry-facing name for a connector entry (id minus the kind)."""
    return entry.id.split(":", 1)[1] if ":" in entry.id else entry.id


def _connected_names(provider: EntitlementProvider) -> set[str] | None:
    """Resolve the connected set once per overlay pass, fail-closed.

    ``None`` means resolution failed — every connector is then annotated
    ``available=False`` (unavailable, NOT filtered: availability is a live
    fact, not an entitlement).
    """
    try:
        names = provider.connected_connector_names()
        return {n for n in names if isinstance(n, str)}
    except Exception as exc:  # noqa: BLE001 — fail closed, never break the tool
        logger.warning("atlas overlay: connected-connector resolution failed: %s", exc)
        return None


def _is_granted(provider: EntitlementProvider, entry: AtlasEntry) -> bool:
    """Grant check, fail-closed: only a literal ``True`` grants."""
    try:
        return provider.is_granted(entry) is True
    except Exception as exc:  # noqa: BLE001 — a raising provider must filter, not leak
        logger.warning("atlas overlay: grant check failed for %s: %s", entry.id, exc)
        return False


def _overlay_one(entry: AtlasEntry, connected: set[str] | None) -> OverlaidEntry:
    if entry.kind == "connector":
        available = connected is not None and _connector_name(entry) in connected
        return OverlaidEntry(entry=entry, available=available)
    return OverlaidEntry(entry=entry, available=None)


class AtlasOverlay:
    """Applies an :class:`EntitlementProvider` to atlas store results.

    Stateless — every method takes the provider explicitly, so one shared
    store singleton serves any number of concurrent contexts without
    cross-talk. Nothing here mutates ``AtlasStore`` or its entries.
    """

    @staticmethod
    def apply(entries: list[AtlasEntry], provider: EntitlementProvider) -> list[OverlaidEntry]:
        """Filter non-granted entries and annotate connector availability.

        Order-preserving; use :meth:`search` when availability re-ranking
        is wanted on scored results.
        """
        connected = _connected_names(provider)
        return [_overlay_one(entry, connected) for entry in entries if _is_granted(provider, entry)]

    @staticmethod
    def search(
        store: AtlasStore,
        query: str,
        provider: EntitlementProvider,
        limit: int = 5,
    ) -> list[OverlaidEntry]:
        """Context-aware search: score, filter, re-rank, then truncate.

        Base relevance scoring is the store's (unchanged); the overlay only
        applies a STABLE re-sort so an available connector ranks above an
        unavailable one at equal score. Filtering happens BEFORE the limit,
        so non-granted entries never eat result slots.
        """
        if limit <= 0:
            return []
        connected = _connected_names(provider)
        ranked: list[tuple[float, int, OverlaidEntry]] = []
        for score, entry in store.search_scored(query):
            if not _is_granted(provider, entry):
                continue
            overlaid = _overlay_one(entry, connected)
            demoted = 1 if overlaid.available is False else 0
            ranked.append((score, demoted, overlaid))
        # Stable sort: score desc, then available-before-unavailable; ties
        # keep the store's seed order (list.sort is stable).
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [overlaid for _, _, overlaid in ranked[:limit]]

    @staticmethod
    def describe(
        store: AtlasStore, entry_id: str, provider: EntitlementProvider
    ) -> OverlaidEntry | None:
        """Describe one entry through the overlay.

        A non-granted entry returns ``None`` — indistinguishable from an
        unknown id, so describe can never confirm a filtered entry exists.
        """
        entry = store.describe(entry_id)
        if entry is None or not _is_granted(provider, entry):
            return None
        return _overlay_one(entry, _connected_names(provider))

    @staticmethod
    def visible_ids(store: AtlasStore, provider: EntitlementProvider) -> list[str]:
        """Sorted ids this context may see — for known-ids error listings.

        Enumerations shown to the agent must come from here, never from
        ``store.entries``, or filtered ids leak through error messages.
        """
        return sorted(e.id for e in store.entries if _is_granted(provider, e))


class DefaultEntitlementProvider:
    """OSS default provider on the real connector seam.

    Availability comes from ``ConnectorRegistry.status(scope_key)`` — the
    same durable-state answer (definition present + config persisted)
    that the ``connector_list`` builtin reports, resolved lazily per call
    so mid-session connects/disconnects are reflected. ``scope_key`` is
    the connector state scope: the tools' ``"default"`` pocket scope in a
    single-user install, or a cloud run's ``ws:<workspace_id>``.

    Nothing is entitlement-gated in OSS: ``is_granted`` is always True,
    so primitives, surfaces, senses, and connectors all stay visible —
    connectors merely carry their live availability annotation.
    """

    def __init__(self, scope_key: str = DEFAULT_SCOPE_KEY, registry: Any | None = None) -> None:
        self._scope_key = scope_key
        self._registry = registry

    def _resolve_registry(self) -> Any:
        if self._registry is None:
            # The builtin connector tools' shared registry — the exact
            # instance connector_list answers from, so atlas availability
            # can never disagree with connector_list in the same process.
            from pocketpaw.tools.builtin.connector_tools import _get_registry

            self._registry = _get_registry()
        return self._registry

    def connected_connector_names(self) -> set[str]:
        from pocketpaw.connectors.protocol import ConnectorStatus

        rows = self._resolve_registry().status(self._scope_key)
        return {row["name"] for row in rows if row.get("status") == ConnectorStatus.CONNECTED}

    def is_granted(self, entry: AtlasEntry) -> bool:  # noqa: ARG002 — protocol shape
        return True


__all__ = [
    "DEFAULT_SCOPE_KEY",
    "AtlasOverlay",
    "DefaultEntitlementProvider",
    "EntitlementProvider",
    "OverlaidEntry",
]
