# atlas/fabric.py — live Fabric (workspace ontology) introspection for the
# atlas MCP tools (AT-7). Created: 2026-07-02 (feat/atlas-fabric).
#
# Fabric — the typed workspace ontology (typed objects like Customer /
# Competitor, typed links like competes_with) — is EE cloud and PER-TENANT:
# its entries are dynamic, so they must NEVER be baked into the compiled
# artifact (atlas.json stays global and byte-deterministic). This module is
# the seam that lets the atlas tools answer "what entity types exist in THIS
# workspace?" live:
#
# * ``FabricIntrospector`` — a small structural protocol (like
#   ``EntitlementProvider`` in overlay.py): ``list_entity_types()`` and
#   ``describe_entity_type(name)``. The atlas MCP server builder accepts an
#   optional introspector; absent (the OSS default) nothing about live
#   Fabric exists — ``fabric:*`` ids are unknown ids and search surfaces no
#   fabric cards. Only the compiled ``primitive:fabric`` narrative remains.
# * ``search_entity_types`` / ``describe_fabric_id`` — the tool-layer
#   helpers. Search is a simple exact/stemmed token match (the store's own
#   ``_stem_set`` normalizer, plus camel-case splitting) over live
#   entity-type names and their property names; results are synthetic cards
#   (id ``fabric:<type>``, tool-layer-only kind ``fabric``) that the search
#   handler APPENDS after compiled-entry results — never displacing them.
# * FAIL-CLOSED: any introspector error (listing, describing, bad shapes)
#   is logged at DEBUG and treated as "no introspector" — no crash, no
#   partial leak.
# * ``build_workspace_fabric_introspector`` — the EE wiring hook: imports
#   ``pocketpaw_ee.fabric`` inside try/except (optional, like the runtime's
#   other ee seams) and binds a ``WorkspaceFabricRegistry`` to ONE
#   workspace id (never a process-global). Import or construction failure
#   degrades to ``None``.
#
# Updated: 2026-07-05 (fix/atlas-compiler-robustness) — two search fixes:
#   * FINDING B: ``search_entity_types`` no longer calls the full
#     ``describe_entity_type`` per type. Search scores on names + property
#     names only and never uses links, but describe on the live EE adapter
#     opens 3 sqlite connections per type (exists + properties + links) → 3N
#     per query, two-thirds wasted. It now reads properties only via
#     ``_search_properties`` (prefers a new optional, duck-typed
#     ``list_entity_properties`` — one connection on the EE adapter — and falls
#     back to describe for older introspectors). The thin search card reports a
#     property count only (``_search_summary``); the link count stays on the
#     detail view (``describe_fabric_id``), so search never triggers the link
#     fetch just to print a number.
#   * FINDING D: ``_CAMEL_RE`` gains an acronym-boundary alternative
#     ``(?<=[A-Z])(?=[A-Z][a-z])`` so consecutive-capital names split
#     ("HTTPServer" → "HTTP Server", "APIKey" → "API Key"); "http server" /
#     "api key" queries now match acronym-named entity types.

from __future__ import annotations

import logging
import re
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Synthetic-card id prefix and tool-layer kind. ``fabric`` is deliberately
# NOT added to ``AtlasKind`` — live fabric cards are plain dicts built at
# answer time, never ``AtlasEntry`` rows in the compiled artifact.
FABRIC_ID_PREFIX = "fabric:"
FABRIC_KIND = "fabric"

# Cap on synthetic fabric cards appended to a search answer. Compiled-entry
# results keep their own limit; fabric cards are extra, never displacing.
MAX_FABRIC_RESULTS = 3

# Entity-type names are commonly CamelCase ("CustomerAccount"); split on
# case boundaries before stemming so "customer account" queries match. Two
# boundaries: the lower/digit->upper transition ("CustomerAccount" ->
# "Customer Account") AND the acronym boundary where a run of capitals is
# followed by a capitalized word ("HTTPServer" -> "HTTP Server", "APIKey" ->
# "API Key", "IOError" -> "IO Error"). Without the acronym alternative,
# consecutive-capital names never split and "http server" / "api key" queries
# miss them.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


@runtime_checkable
class FabricIntrospector(Protocol):
    """Live view of ONE workspace's Fabric ontology (structural protocol).

    Implementations are bound to a single workspace at construction —
    per-run wiring, never a process-global. Both methods may raise; every
    caller in this module treats a raise as "introspector absent"
    (fail-closed).
    """

    def list_entity_types(self) -> list[str]:
        """Names of every entity type registered in this workspace."""
        ...

    def describe_entity_type(self, name: str) -> dict | None:
        """Schema for one entity type, or ``None`` if unknown.

        Expected shape: ``{"name": str, "properties": list[str],
        "links": list[dict]}`` (each link dict carries ``name`` /
        ``from_type`` / ``to_type``).
        """
        ...

    # OPTIONAL — a cheap properties-only read path for the search scorer.
    #
    # ``search_entity_types`` scores on names + property names and never uses
    # links, so calling the full ``describe_entity_type`` per type is wasteful
    # (on the live EE adapter it opens 3 sqlite connections per type). An
    # implementation MAY expose ``list_entity_properties(name) -> list[str]``
    # to serve just the property names; ``search`` uses it via ``getattr`` when
    # present and falls back to ``describe_entity_type`` otherwise. It is NOT
    # part of the required structural contract — adding it here would break the
    # ``isinstance`` check for introspectors (including test fakes) that only
    # implement the two required methods — so it stays a documented, duck-typed
    # extension rather than a Protocol member.


def _fabric_tokens(text: str) -> set[str]:
    """Stemmed token set for a fabric name — camel-split then the store's
    own suffix normalizer, so query and index normalize identically."""
    from pocketpaw.atlas.store import _stem_set

    return _stem_set(_CAMEL_RE.sub(" ", text))


def _clean_properties(described: dict) -> list[str]:
    props = described.get("properties", [])
    if not isinstance(props, (list, tuple, set, frozenset)):
        return []
    return sorted(p for p in props if isinstance(p, str) and p)


def _clean_links(described: dict) -> list[dict]:
    links = described.get("links", [])
    if not isinstance(links, (list, tuple)):
        return []
    return [link for link in links if isinstance(link, dict)]


def _summary(name: str, properties: list[str], links: list[dict]) -> str:
    """Detail-view summary (describe): reports both property and link counts."""
    return (
        f"Workspace entity type '{name}' — {len(properties)} properties, "
        f"{len(links)} links. Live Fabric ontology (this workspace only)."
    )


def _search_summary(name: str, properties: list[str]) -> str:
    """Thin search-card summary: property count only.

    The search path deliberately never fetches a type's links (see
    ``search_entity_types``), so the card can't report a link count without
    paying for a read it doesn't need. The link count stays on the detail
    view (``describe_fabric_id`` via ``_summary``).
    """
    return (
        f"Workspace entity type '{name}' — {len(properties)} properties. "
        "Live Fabric ontology (this workspace only)."
    )


def _search_properties(introspector: FabricIntrospector, name: str) -> list[str]:
    """Property names for one type on the SEARCH path — properties only.

    Search scores on entity-type NAMES and PROPERTY names; it never uses a
    type's links. So it must NOT call ``describe_entity_type``, which on the
    live EE adapter opens three sqlite connections per type (exists +
    properties + links) — 3N connections/queries per query, two-thirds of it
    wasted (the existence check the search already knows passed, plus the link
    fetch it discards). Prefer the properties-only ``list_entity_properties``
    read path (one connection); fall back to ``describe_entity_type`` only for
    older introspectors that don't expose it, so no caller regresses.
    """
    getter = getattr(introspector, "list_entity_properties", None)
    if callable(getter):
        props = getter(name)
        if isinstance(props, (list, tuple, set, frozenset)):
            return sorted(p for p in props if isinstance(p, str) and p)
        return []
    described = introspector.describe_entity_type(name) or {}
    if not isinstance(described, dict):
        return []
    return _clean_properties(described)


def search_entity_types(
    introspector: FabricIntrospector,
    intent: str,
    limit: int = MAX_FABRIC_RESULTS,
) -> list[dict[str, Any]]:
    """Match *intent* against the live entity-type list; return fabric cards.

    Simple exact/stemmed token match: a query token hitting an entity-type
    NAME token scores 2.0, a PROPERTY-name token 1.0 (best field only, like
    the store's scoring spirit). Zero-overlap types are dropped; ties sort
    by name for determinism. FAIL-CLOSED: any introspector error returns
    ``[]`` (logged at DEBUG) — the compiled-entry answer is never harmed.

    Reads properties only (never links) via ``_search_properties`` — the thin
    search card reports a property count, not a link count, so the per-type
    link fetch is never triggered on the search path (see FINDING B).
    """
    try:
        query = _fabric_tokens(intent)
        if not query or limit <= 0:
            return []
        scored: list[tuple[float, str, list[str]]] = []
        for name in introspector.list_entity_types():
            if not isinstance(name, str) or not name:
                continue
            properties = _search_properties(introspector, name)
            name_tokens = _fabric_tokens(name)
            prop_tokens: set[str] = set()
            for prop in properties:
                prop_tokens |= _fabric_tokens(prop)
            score = 2.0 * len(query & name_tokens)
            score += 1.0 * len(query & (prop_tokens - name_tokens))
            if score > 0:
                scored.append((score, name, properties))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {
                "id": f"{FABRIC_ID_PREFIX}{name}",
                "kind": FABRIC_KIND,
                "name": name,
                "summary": _search_summary(name, properties),
            }
            for _score, name, properties in scored[:limit]
        ]
    except Exception as exc:  # noqa: BLE001 — fail closed, never break the tool
        logger.debug("atlas fabric introspection unavailable (search): %s", exc)
        return []


def describe_fabric_id(
    introspector: FabricIntrospector,
    entry_id: str,
) -> dict[str, Any] | None:
    """Live schema payload for a ``fabric:<type>`` id, or ``None``.

    ``None`` covers every miss uniformly: a non-fabric id, an unknown
    entity type, or a raising introspector (fail-closed, DEBUG-logged) —
    the caller then falls through to the normal unknown-id path, so a
    broken introspector is indistinguishable from an absent one.
    """
    if not entry_id.startswith(FABRIC_ID_PREFIX):
        return None
    name = entry_id[len(FABRIC_ID_PREFIX) :]
    if not name:
        return None
    try:
        described = introspector.describe_entity_type(name)
        if not isinstance(described, dict):
            return None
        properties = _clean_properties(described)
        links = _clean_links(described)
        return {
            "id": entry_id,
            "kind": FABRIC_KIND,
            "name": name,
            "summary": _summary(name, properties, links),
            "properties": properties,
            "links": links,
            "workspace_scoped": True,
            "narrative": (
                f"'{name}' is a typed Fabric entity registered in THIS workspace's "
                "ontology (not a global OS primitive). Its schema is live and "
                "tenant-specific: the properties and links listed here are what "
                "this workspace declared. See primitive:fabric for what Fabric is."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — fail closed, never break the tool
        logger.debug("atlas fabric introspection unavailable (describe %s): %s", entry_id, exc)
        return None


class RegistryFabricIntrospector:
    """Adapter from the EE ``WorkspaceFabricRegistry`` read surface to
    :class:`FabricIntrospector`.

    Takes any registry-shaped object (``list_entity_types`` /
    ``entity_type_exists`` / ``get_entity_properties`` /
    ``list_entity_links``) so tests can wrap a tmp-path store without
    importing ``pocketpaw_ee`` at module scope. The registry is already
    bound to one workspace — this adapter adds no scoping of its own.
    """

    __slots__ = ("_registry",)

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def list_entity_types(self) -> list[str]:
        return list(self._registry.list_entity_types())

    def describe_entity_type(self, name: str) -> dict | None:
        if not self._registry.entity_type_exists(name):
            return None
        return {
            "name": name,
            "properties": sorted(self._registry.get_entity_properties(name)),
            "links": list(self._registry.list_entity_links(name)),
        }

    def list_entity_properties(self, name: str) -> list[str]:
        """Property names for one type — the cheap search read path.

        One registry call (``get_entity_properties`` → one sqlite connection)
        instead of the three ``describe_entity_type`` opens (exists +
        properties + links). Search scores on property names only, so it never
        needs the existence check (a name that came from ``list_entity_types``
        already exists) or the link list. An unknown type returns ``[]`` — the
        store's ``get_properties`` already returns an empty set for it, matching
        the ``NullFabricRegistry`` contract.
        """
        return sorted(self._registry.get_entity_properties(name))


def build_workspace_fabric_introspector(workspace_id: str) -> FabricIntrospector | None:
    """EE wiring hook: a live introspector bound to *workspace_id*, or None.

    ``None`` on: blank workspace id, ``pocketpaw_ee.fabric`` not importable
    (OSS install), or registry/store construction failure (e.g. unwritable
    state dir) — all DEBUG-logged, all degrade to "no introspector"
    (fail-closed). The returned introspector is constructed PER RUN with the
    run's workspace id — never cached process-globally.
    """
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        return None
    try:
        from pocketpaw_ee.fabric import WorkspaceFabricRegistry, WorkspaceFabricStore
    except ImportError:
        logger.debug("pocketpaw_ee.fabric not importable; atlas fabric introspection off")
        return None
    try:
        store = WorkspaceFabricStore()
        registry = WorkspaceFabricRegistry(store=store, workspace_id=workspace_id.strip())
        return RegistryFabricIntrospector(registry)
    except Exception as exc:  # noqa: BLE001 — infra failure degrades, never crashes
        logger.debug("atlas fabric introspector construction failed: %s", exc)
        return None


__all__ = [
    "FABRIC_ID_PREFIX",
    "FABRIC_KIND",
    "MAX_FABRIC_RESULTS",
    "FabricIntrospector",
    "RegistryFabricIntrospector",
    "build_workspace_fabric_introspector",
    "describe_fabric_id",
    "search_entity_types",
]
