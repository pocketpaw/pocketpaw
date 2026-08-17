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
#
# Updated: 2026-08-17 (feat/ast-2-atlas-trust-aggregate, AST-2) — the describe
#   view learns source-truth. The registry knows Customer HAS ``arr``; the OSS
#   ``FabricStore`` (a different DB) knows whether ``arr`` is disputed / stale
#   and who won. One optional, duck-typed, ASYNC read bridges them at the
#   TYPE level:
#   * ``RegistryFabricIntrospector.entity_type_source_truth(name)`` — NOT a
#     Protocol member (same reasoning as ``list_entity_properties``). Resolves
#     the type in the store, runs ONE indexed type-scoped
#     ``list_statement_keys`` (LIMIT cap+1) — an untracked type stops there
#     with zero further statement reads — then walks tracked keys through
#     ``get_statements`` + the pure ``resolve()`` (mirroring
#     ``get_object_provenance``), capped at ``SOURCE_TRUTH_SAMPLE_CAP`` keys
#     (``sampled=True``). Rolls up per property: objects / disputed / stale /
#     aging / winner_writer_mix, plus ``mode``, ``tracked``, ``object_count``
#     and a pointer to ``fabric_query include_provenance=true``.
#   * ``describe_fabric_id_async`` — the awaitable sibling the async
#     ``atlas_describe`` handler now calls: sync describe + the aggregate folded
#     in as the additive ``source_truth`` key. Any raise → key absent, registry
#     payload intact. ``search_entity_types`` never touches it.
#   * ``build_workspace_fabric_introspector`` also binds
#     ``get_fabric_store(workspace_id=...)``; that binding degrades alone (the
#     registry introspector still returns, only ``source_truth`` goes absent).

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

# AST-2: hard cap on tracked (object_id, property) keys the type-level
# source-truth aggregate walks per describe. Above it the roll-up reports
# ``sampled=True`` and stops — a describe answer is a glance, not an audit;
# the per-object truth is one fabric_query away (SOURCE_TRUTH_POINTER).
SOURCE_TRUTH_SAMPLE_CAP = 500
SOURCE_TRUTH_POINTER = "fabric_query include_provenance=true for the per-object answer"

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
    #
    # OPTIONAL — a type-level source-truth aggregate for the DETAIL view (AST-2).
    #
    # An implementation MAY expose ``async entity_type_source_truth(name) ->
    # dict | None`` — a live roll-up of the OSS FabricStore's statement
    # provenance for objects of that type (per property: how many tracked
    # objects, how many disputed / stale / aging, the winner writer-class mix;
    # see ``RegistryFabricIntrospector.entity_type_source_truth`` for the exact
    # shape). ONLY ``describe_fabric_id_async`` consumes it (via ``getattr`` +
    # ``callable`` + ``await``), folded into the describe payload as the
    # additive ``source_truth`` key; ``search_entity_types`` NEVER calls it —
    # search stays properties-only (FINDING B). Any raise → the key is simply
    # absent and the registry payload still answers. Same non-Protocol
    # reasoning as ``list_entity_properties`` above.


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


async def describe_fabric_id_async(
    introspector: FabricIntrospector,
    entry_id: str,
) -> dict[str, Any] | None:
    """:func:`describe_fabric_id` plus the optional live ``source_truth`` roll-up.

    The async sibling the (already async) ``atlas_describe`` handler awaits.
    It calls the sync registry describe unchanged, then — ONLY when the
    introspector exposes the optional, duck-typed ``entity_type_source_truth``
    (AST-2) — awaits it and folds a dict result in as ``source_truth``.
    Fail-closed: a missing method, a ``None`` result, or ANY raise leaves the
    key ABSENT and the registry payload untouched, so a broken FabricStore
    can never take the schema answer down with it. Sync callers keep using
    ``describe_fabric_id``.
    """
    payload = describe_fabric_id(introspector, entry_id)
    if payload is None:
        return None
    aggregate = getattr(introspector, "entity_type_source_truth", None)
    if not callable(aggregate):
        return payload
    try:
        source_truth = await aggregate(payload["name"])
        if isinstance(source_truth, dict):
            payload["source_truth"] = source_truth
    except Exception as exc:  # noqa: BLE001 — additive field, never break the tool
        logger.debug("atlas fabric source-truth aggregate unavailable (%s): %s", entry_id, exc)
    return payload


class RegistryFabricIntrospector:
    """Adapter from the EE ``WorkspaceFabricRegistry`` read surface to
    :class:`FabricIntrospector`.

    Takes any registry-shaped object (``list_entity_types`` /
    ``entity_type_exists`` / ``get_entity_properties`` /
    ``list_entity_links``) so tests can wrap a tmp-path store without
    importing ``pocketpaw_ee`` at module scope. The registry is already
    bound to one workspace — this adapter adds no scoping of its own.

    ``source_truth`` (AST-2) is an OPTIONAL OSS ``FabricStore`` (the
    per-property statement/provenance store — a different DB from the
    registry) bound to the same workspace; with it,
    :meth:`entity_type_source_truth` serves the live type-level trust
    aggregate. ``None`` (the default, and the builder's fallback when the
    store can't be bound) makes that method answer ``None`` — the describe
    payload then simply lacks ``source_truth``.
    """

    __slots__ = ("_registry", "_source_truth", "_workspace_id")

    def __init__(
        self,
        registry: Any,
        *,
        source_truth: Any | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self._registry = registry
        self._source_truth = source_truth
        self._workspace_id = workspace_id

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

    async def entity_type_source_truth(self, name: str) -> dict | None:
        """Live type-level source-truth roll-up for the describe view (AST-2).

        Optional, duck-typed — NOT a ``FabricIntrospector`` Protocol member
        (see the OPTIONAL note on the Protocol). ``None`` when no store is
        bound. Otherwise::

            {mode, tracked, sampled, object_count,
             properties: {<prop>: {objects, disputed, stale, aging,
                                   winner_writer_mix: {<writer_class>: n}}},
             pointer}

        Cost is bounded by the TRACKED set, never the whole fabric: the type
        name resolves to the store's type row (``get_type_by_name``, a
        ``fabric_object_types`` read); ONE indexed ``list_statement_keys``
        query (type-scoped join, ``LIMIT cap+1``) is the only statement read
        for an untracked type — no keys → ``tracked=False`` and it stops.
        Tracked keys are walked with ``get_statements`` + the pure
        ``resolve()`` (mirroring ``FabricStore.get_object_provenance`` —
        dispute/freshness/winner metadata are never persisted, so they are
        recomputed here), capped at ``SOURCE_TRUTH_SAMPLE_CAP`` keys
        (``sampled=True`` beyond it). ``winner_freshness`` is None on the
        pinned path, so a pinned winner counts as neither stale nor aging.
        """
        store = self._source_truth
        if store is None:
            return None
        from datetime import UTC, datetime

        from pocketpaw.config import get_settings
        from pocketpaw.fabric.resolver import resolve
        from pocketpaw.fabric.trust import default_trust_rules

        ws = self._workspace_id
        out: dict[str, Any] = {
            "mode": get_settings().fabric_source_truth_mode,
            "tracked": False,
            "sampled": False,
            "object_count": 0,
            "properties": {},
            "pointer": SOURCE_TRUTH_POINTER,
        }
        obj_type = await store.get_type_by_name(name, workspace_id=ws)
        if obj_type is None:
            return out
        keys = await store.list_statement_keys(
            workspace_id=ws, type_id=obj_type.id, limit=SOURCE_TRUTH_SAMPLE_CAP + 1
        )
        if not keys:
            return out
        if len(keys) > SOURCE_TRUTH_SAMPLE_CAP:
            out["sampled"] = True
            keys = keys[:SOURCE_TRUTH_SAMPLE_CAP]
        out["tracked"] = True
        out["object_count"] = len({oid for oid, _prop in keys})
        now = datetime.now(UTC)
        rules = default_trust_rules()
        properties: dict[str, dict[str, Any]] = {}
        for object_id, prop in keys:
            stmts = await store.get_statements(object_id, prop, workspace_id=ws)
            if not stmts:
                continue
            resolution = resolve(stmts, rules, object_type=obj_type.name, now=now)
            entry = properties.setdefault(
                prop,
                {"objects": 0, "disputed": 0, "stale": 0, "aging": 0, "winner_writer_mix": {}},
            )
            entry["objects"] += 1
            if resolution.is_disputed:
                entry["disputed"] += 1
            if resolution.winner_freshness in ("stale", "aging"):
                entry[resolution.winner_freshness] += 1
            winner = resolution.winner_statement
            if winner is not None:
                mix = entry["winner_writer_mix"]
                mix[winner.writer_class] = mix.get(winner.writer_class, 0) + 1
        out["properties"] = properties
        return out


def build_workspace_fabric_introspector(workspace_id: str) -> FabricIntrospector | None:
    """EE wiring hook: a live introspector bound to *workspace_id*, or None.

    ``None`` on: blank workspace id, ``pocketpaw_ee.fabric`` not importable
    (OSS install), or registry/store construction failure (e.g. unwritable
    state dir) — all DEBUG-logged, all degrade to "no introspector"
    (fail-closed). The returned introspector is constructed PER RUN with the
    run's workspace id — never cached process-globally.

    AST-2: the same call also binds the workspace's OSS ``FabricStore``
    (``get_fabric_store(workspace_id=...)``) as the introspector's optional
    ``source_truth`` store. That binding degrades INDEPENDENTLY: if it fails
    the registry introspector is still returned (schema answers keep
    working) and only the ``source_truth`` describe field goes absent.
    """
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        return None
    try:
        from pocketpaw_ee.fabric import WorkspaceFabricRegistry, WorkspaceFabricStore
    except ImportError:
        logger.debug("pocketpaw_ee.fabric not importable; atlas fabric introspection off")
        return None
    try:
        ws = workspace_id.strip()
        store = WorkspaceFabricStore()
        registry = WorkspaceFabricRegistry(store=store, workspace_id=ws)
        source_truth = None
        try:
            from pocketpaw.stores import get_fabric_store

            source_truth = get_fabric_store(workspace_id=ws)
        except Exception as exc:  # noqa: BLE001 — additive field degrades alone
            logger.debug("atlas fabric source-truth store unavailable: %s", exc)
        return RegistryFabricIntrospector(registry, source_truth=source_truth, workspace_id=ws)
    except Exception as exc:  # noqa: BLE001 — infra failure degrades, never crashes
        logger.debug("atlas fabric introspector construction failed: %s", exc)
        return None


__all__ = [
    "FABRIC_ID_PREFIX",
    "FABRIC_KIND",
    "MAX_FABRIC_RESULTS",
    "SOURCE_TRUTH_POINTER",
    "SOURCE_TRUTH_SAMPLE_CAP",
    "FabricIntrospector",
    "RegistryFabricIntrospector",
    "build_workspace_fabric_introspector",
    "describe_fabric_id",
    "describe_fabric_id_async",
    "search_entity_types",
]
