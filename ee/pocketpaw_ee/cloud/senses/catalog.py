# Catalog search + listing — keyword/BM25 discovery AND full browse over the
#   FULL connector catalog.
# Created: 2026-07-16 (SR-1 catalog-wide discovery) — the search half of the
#   new ``sense_search`` MCP tool. Where ``list_pocket_connectors`` /
#   ``list_senses`` only enumerate what a pocket ALREADY bound, this searches
#   every connector the registry knows (all 35 YAML defs), so the agent can
#   surface an action nobody wired up yet and tell the user to enable it. Pure,
#   dependency-free BM25 over the parsed connector defs (NO ML, NO new deps):
#   each connector action is one document; the searchable text is the connector
#   name + display name + action name + description + the connector's declared
#   senses (enriched with each core sense's display name + description). Trust
#   level and execution mode come from the connector's adapter schema (the same
#   authoritative source ``get_action_trust`` / ``list_pocket_connectors`` use),
#   so native LOCAL connectors (gcp, firebase) report ``execution_mode=local``
#   correctly and are marked UNAVAILABLE — the cloud run has no local-runtime
#   listener, so ``connectors.service.execute`` returns 503 for them and the
#   agent must not select them. BOUND-vs-unbound is overlaid from a caller-
#   supplied set of the pocket's reachable connector names (resolved from the EE
#   store by the MCP handler), keeping this module pure + fully unit-testable.
#   ``cost_estimate`` is a placeholder (None) — real per-action pricing lands in
#   a later task; this module builds NO metering.
# Updated: 2026-07-16 (SR-2 catalog listing API) — added ``list_catalog``: the
#   BROWSE half (no query) behind ``GET /api/v1/cloud/senses/catalog``, the data
#   behind a tools-style front door. It reuses the SAME ``_build_index`` path
#   (trust / execution-mode / senses come from the same adapter-schema source as
#   search — no duplicated index logic), then GROUPS every connector by CATEGORY
#   (the connector def's ``type`` field, e.g. ``developer`` / ``communication``),
#   deterministically sorted. Each connector carries its actions (with per-action
#   trust + execution mode + availability + the ``cost_estimate=None``
#   placeholder), its declared senses, and a BOUND flag overlaid from the same
#   caller-supplied reachable set search uses. Availability follows the identical
#   rule as search: ``local`` / ``sandbox`` actions the shared cloud can't
#   dispatch are marked UNAVAILABLE. Pure + dependency-free — the tenant-filtered
#   bound read still happens in the EE connectors service, not here.

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from pocketpaw.senses import CORE_SENSES

# ---------------------------------------------------------------------------
# Availability — which execution modes the cloud run can actually dispatch.
# ---------------------------------------------------------------------------

# The cloud FastAPI process only runs ``cloud`` actions in-process. ``local``
# actions need the user's pocketpaw runtime listener (absent in the shared
# cloud), so ``connectors.service.execute`` returns a 503
# (``connector.local_agent_unavailable``); ``sandbox`` is reserved and raises
# 501. Marking non-cloud actions UNAVAILABLE keeps the agent from proposing a
# tool it cannot actually fire.
_UNAVAILABLE_REASONS: dict[str, str] = {
    "local": "local_runtime_unavailable",
    "sandbox": "sandbox_not_implemented",
}

# ---------------------------------------------------------------------------
# Tokenization — lowercase alphanumeric tokens, tiny stoplist.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "to", "of", "for", "in", "on", "and", "or",
        "my", "me", "is", "it", "with", "this", "that", "from", "at", "by",
    }
)

# Field weights baked into each document's term counts so a connector-name or
# action-name match outranks a description-only match. BM25's tf saturation
# (k1) keeps the boost bounded.
_W_CONNECTOR = 3
_W_ACTION = 3
_W_DISPLAY = 2
_W_SENSE = 2
_W_DESCRIPTION = 1

_BM25_K1 = 1.5
_BM25_B = 0.75


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, length >= 2, minus a tiny stoplist.

    ``create_issue`` -> ``["create", "issue"]`` (underscore splits);
    ``paw.code.v1`` -> ``["paw", "code", "v1"]``.
    """
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2 and t not in _STOPWORDS]


@dataclass(frozen=True)
class CatalogHit:
    """One connector action matched by a catalog search.

    ``bound`` is True when the connector is enabled + reachable from the current
    pocket (overlaid from the caller's reachable-set). ``available`` is False for
    non-cloud actions the shared cloud can't dispatch (local / sandbox), with
    ``unavailable_reason`` naming why. ``cost_estimate`` is a placeholder (None)
    until per-action pricing ships in a later task.
    """

    connector: str
    display_name: str
    action: str
    description: str
    trust_level: str  # "auto" | "confirm" | "restricted"
    execution_mode: str  # "cloud" | "local" | "sandbox"
    senses: tuple[str, ...]
    bound: bool
    available: bool
    unavailable_reason: str | None
    cost_estimate: float | None
    score: float


@dataclass
class _ActionDoc:
    """One indexed action: its scored token bag + the metadata for its hit."""

    connector: str
    display_name: str
    action: str
    description: str
    trust_level: str
    execution_mode: str
    senses: tuple[str, ...]
    # The connector's category — its ``type`` field (e.g. "developer",
    # "communication"). Carried on every action doc so ``list_catalog`` can group
    # without a second registry pass; search ignores it.
    category: str = "generic"
    tokens: Counter[str] = field(default_factory=Counter)

    @property
    def length(self) -> int:
        return sum(self.tokens.values())


# Sense-id -> curated CoreSense, so a connector's declared senses contribute
# their human display name + description to the searchable text (a query like
# "manage calendar availability" then matches a calendar connector even when the
# action wording differs).
_SENSE_BY_ID = {s.id: s for s in CORE_SENSES}


def _add(tokens: Counter[str], text: str, weight: int) -> None:
    for tok in _tokenize(text):
        tokens[tok] += weight


async def _build_index(registry) -> list[_ActionDoc]:
    """Index every action of every connector the registry knows.

    Trust level + execution mode come from the connector's adapter schema (the
    authoritative source — native LOCAL adapters stamp ``execution_mode=local``
    even though their YAML omits the key), matching how ``get_action_trust`` and
    ``list_pocket_connectors`` classify actions. A connector whose adapter fails
    to enumerate is skipped rather than breaking the whole search.
    """
    # Reuse the EE service's registry singleton + adapter factory so the catalog
    # reads the SAME parsed defs the rest of the connector stack does (no file
    # re-reading, no duplicated native/REST selection logic).
    from pocketpaw_ee.cloud.connectors import service as connectors_service

    docs: list[_ActionDoc] = []
    for defn in registry.definitions:
        senses = tuple(getattr(defn, "senses", None) or ())
        # Category = the connector def's ``type`` (ConnectorDef has no separate
        # ``category`` field; ``type`` is the deterministic grouping key).
        category = str(getattr(defn, "type", None) or "generic")
        try:
            adapter = connectors_service._adapter_for_definition(defn, defn.name)  # noqa: SLF001
            schemas = await adapter.actions()
        except Exception:  # noqa: BLE001 — a bad adapter must not drop the catalog
            continue
        for schema in schemas:
            tokens: Counter[str] = Counter()
            _add(tokens, defn.name, _W_CONNECTOR)
            _add(tokens, defn.display_name, _W_DISPLAY)
            _add(tokens, schema.name, _W_ACTION)
            _add(tokens, schema.description, _W_DESCRIPTION)
            for sense_id in senses:
                _add(tokens, sense_id, _W_SENSE)
                core = _SENSE_BY_ID.get(sense_id)
                if core is not None:
                    _add(tokens, core.display_name, _W_SENSE)
                    _add(tokens, core.description, _W_SENSE)
            docs.append(
                _ActionDoc(
                    connector=defn.name,
                    display_name=defn.display_name,
                    action=schema.name,
                    description=schema.description,
                    trust_level=str(schema.trust_level),
                    execution_mode=str(schema.execution_mode),
                    senses=senses,
                    category=category,
                    tokens=tokens,
                )
            )
    return docs


def _bm25_scores(query_tokens: list[str], docs: list[_ActionDoc]) -> list[float]:
    """Classic BM25 (k1=1.5, b=0.75) over the weighted token bags.

    IDF is computed across the action corpus; term frequency is the field-
    weighted count. Pure arithmetic — no ML, no vectors, no external index.
    """
    n = len(docs)
    if n == 0:
        return []
    df: Counter[str] = Counter()
    for doc in docs:
        for tok in doc.tokens:
            df[tok] += 1
    avgdl = sum(doc.length for doc in docs) / n
    if avgdl == 0:
        return [0.0] * n

    unique_q = set(query_tokens)
    idf = {
        t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
        for t in unique_q
        if df.get(t, 0) > 0
    }

    scores: list[float] = []
    for doc in docs:
        dl = doc.length
        s = 0.0
        for t in unique_q:
            f = doc.tokens.get(t, 0)
            if f == 0 or t not in idf:
                continue
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
            s += idf[t] * (f * (_BM25_K1 + 1)) / denom
        scores.append(s)
    return scores


async def search_catalog(
    query: str,
    *,
    bound_connectors: set[str] | None = None,
    limit: int = 10,
    registry=None,
) -> list[CatalogHit]:
    """Search the FULL connector catalog for actions matching ``query``.

    Catalog-wide: every connector the registry knows is searched, including
    connectors NOT bound to the current pocket — that is the whole point (the
    agent can surface a tool nobody wired up yet). Ranking is BM25 over the
    parsed connector defs; only positive-scoring actions are returned, best
    first, capped at ``limit``.

    ``bound_connectors`` is the set of connector names reachable from the current
    pocket (resolved from the EE store by the caller); each hit's ``bound`` flag
    is set from it. Passing ``None`` treats everything as unbound. ``available``
    is intrinsic to the action (False for ``local`` / ``sandbox`` execution
    modes the shared cloud can't dispatch). ``registry`` defaults to the EE
    connector-service singleton; tests inject a registry built from a fixed
    connectors dir.
    """
    if not query or not query.strip():
        return []

    if registry is None:
        from pocketpaw_ee.cloud.connectors import service as connectors_service

        registry = connectors_service._get_registry()  # noqa: SLF001 — reuse the EE singleton

    bound = bound_connectors or set()
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    docs = await _build_index(registry)
    scores = _bm25_scores(query_tokens, docs)

    ranked = sorted(
        (
            (doc, score)
            for doc, score in zip(docs, scores, strict=True)
            if score > 0
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )[: max(0, limit)]

    hits: list[CatalogHit] = []
    for doc, score in ranked:
        reason = _UNAVAILABLE_REASONS.get(doc.execution_mode)
        hits.append(
            CatalogHit(
                connector=doc.connector,
                display_name=doc.display_name,
                action=doc.action,
                description=doc.description,
                trust_level=doc.trust_level,
                execution_mode=doc.execution_mode,
                senses=doc.senses,
                bound=doc.connector in bound,
                available=reason is None,
                unavailable_reason=reason,
                # TODO(SR-pricing): real per-action cost lands in a later task;
                # placeholder until then. Do NOT build metering here.
                cost_estimate=None,
                score=round(score, 4),
            )
        )
    return hits


# ---------------------------------------------------------------------------
# Listing (browse, no query) — the grouped catalog behind GET /senses/catalog.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogActionEntry:
    """One action of a connector, as it appears in the browse catalog.

    ``available`` is False for non-cloud execution modes the shared cloud can't
    dispatch (``local`` / ``sandbox``) — the SAME rule ``search_catalog`` applies
    — with ``unavailable_reason`` naming why. ``cost_estimate`` is a placeholder
    (None) until per-action pricing ships.
    """

    action: str
    description: str
    trust_level: str  # "auto" | "confirm" | "restricted"
    execution_mode: str  # "cloud" | "local" | "sandbox"
    available: bool
    unavailable_reason: str | None
    cost_estimate: float | None


@dataclass(frozen=True)
class CatalogConnectorEntry:
    """One connector in the browse catalog, with its actions + per-tenant state.

    ``bound`` is True when the connector is enabled + reachable from the current
    pocket (overlaid from the caller's reachable-set). ``senses`` are the
    provider-agnostic capabilities the connector declares.
    """

    connector: str
    display_name: str
    category: str
    senses: tuple[str, ...]
    bound: bool
    actions: tuple[CatalogActionEntry, ...]


@dataclass(frozen=True)
class CatalogCategoryGroup:
    """Every connector sharing one category (the connector def's ``type``)."""

    category: str
    connectors: tuple[CatalogConnectorEntry, ...]


async def list_catalog(
    *,
    bound_connectors: set[str] | None = None,
    registry=None,
) -> list[CatalogCategoryGroup]:
    """Browse the WHOLE connector catalog, grouped by category.

    The listing half of the catalog (search's sibling): no query, every connector
    the registry knows, each with its full action list. Reuses ``_build_index``
    verbatim so trust level, execution mode, and senses come from the SAME
    adapter-schema source ``search_catalog`` reads — no duplicated index logic.

    Grouping is deterministic: categories sorted alphabetically, connectors sorted
    by name within a category, actions kept in the adapter's natural (YAML) order.
    The category is the connector def's ``type`` field.

    ``bound_connectors`` is the set of connector names reachable from the current
    pocket (resolved from the EE store by the caller); each connector's ``bound``
    flag is set from it. Passing ``None`` treats everything as unbound.
    ``available`` is intrinsic to each action (False for ``local`` / ``sandbox``
    the shared cloud can't dispatch). ``registry`` defaults to the EE connector-
    service singleton; tests inject a registry built from a fixed connectors dir.
    """
    if registry is None:
        from pocketpaw_ee.cloud.connectors import service as connectors_service

        registry = connectors_service._get_registry()  # noqa: SLF001 — reuse the EE singleton

    bound = bound_connectors or set()
    docs = await _build_index(registry)

    # Fold the flat per-action index into per-connector accumulators, preserving
    # first-seen action order (the adapter's YAML order).
    by_connector: dict[str, dict] = {}
    for doc in docs:
        entry = by_connector.setdefault(
            doc.connector,
            {
                "display_name": doc.display_name,
                "category": doc.category,
                "senses": doc.senses,
                "actions": [],
            },
        )
        reason = _UNAVAILABLE_REASONS.get(doc.execution_mode)
        entry["actions"].append(
            CatalogActionEntry(
                action=doc.action,
                description=doc.description,
                trust_level=doc.trust_level,
                execution_mode=doc.execution_mode,
                available=reason is None,
                unavailable_reason=reason,
                # TODO(SR-pricing): real per-action cost lands in a later task;
                # placeholder until then. Do NOT build metering here.
                cost_estimate=None,
            )
        )

    connectors = [
        CatalogConnectorEntry(
            connector=name,
            display_name=data["display_name"],
            category=data["category"],
            senses=data["senses"],
            bound=name in bound,
            actions=tuple(data["actions"]),
        )
        for name, data in by_connector.items()
    ]

    # Group by category, sorting both levels for a stable, deterministic response.
    by_category: dict[str, list[CatalogConnectorEntry]] = {}
    for conn in connectors:
        by_category.setdefault(conn.category, []).append(conn)

    return [
        CatalogCategoryGroup(
            category=category,
            connectors=tuple(sorted(members, key=lambda c: c.connector)),
        )
        for category, members in sorted(by_category.items())
    ]


__all__ = [
    "CatalogActionEntry",
    "CatalogCategoryGroup",
    "CatalogConnectorEntry",
    "CatalogHit",
    "list_catalog",
    "search_catalog",
]
