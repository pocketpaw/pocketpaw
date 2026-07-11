# Fabric data models — Pydantic models for the ontology layer.
# Created: 2026-03-28
# Updated: 2026-06-13 (feat/fabric-multihop) — Added multi-hop / path traversal
#   to FabricQuery. A new PathHop model expresses ONE traversal step (a
#   link_type, an optional terminal object_type, an optional property-filter
#   bag, and a direction); FabricQuery gains an additive ``path: list[PathHop]``
#   field. When ``path`` is set, the query walks the link chain server-side and
#   returns the objects at the terminal hop — the 2-hop ontology join (e.g.
#   "open Deals whose Customer competes_with a Competitor") that previously had
#   to be hand-stitched as separate get_linked_objects calls in app code. The
#   existing single-hop ``linked_to``/``link_type`` fields are untouched and
#   keep working exactly as before (backward compatible). Each hop traverses
#   the named link in the FORWARD direction by default (from_object_id ->
#   to_object_id, the direction store.link() records), with an explicit
#   ``direction="in"`` for reverse traversal.
# Updated: 2026-06-13 (review fixes #1465) — bounded the path: FabricQuery.path
#   is capped at MAX_HOPS (5) by a field_validator that rejects a longer path
#   with a clear ValueError (an unbounded, cycle-blind iterative walk is a
#   latency / infinite-loop risk); PathHop.link_type carries Field(max_length=200)
#   as a sanity bound on an LLM-facing string (values are already bound params,
#   so this is hygiene, not injection defense).
# Updated: 2026-06-19 (SZD-2 — workspace-scope object TYPES) — ObjectType gains an
#   optional ``workspace_id`` so the discovered-type catalog is per-tenant. This
#   reverses the earlier W4a choice (object_types deliberately had NO workspace
#   column then); the "sovereign zero-setup discovery" feature requires each
#   tenant's discovered object TYPES to stay private, so a type defined in
#   workspace A must be invisible/unusable from workspace B. ``None`` = a
#   legacy/global type predating tenancy (or an OSS / single-tenant caller),
#   which a scoped read still sees — exactly the NULL-as-legacy boundary the
#   W4a object/link scoping already uses.
# Updated: 2026-07-10 (ontology-operator-ux) — two additions that make the Fabric
#   ontology operable by a non-engineer:
#     1. ObjectType gains a ``version`` int (starts at 1). ``FabricStore.update_type``
#        bumps it when the operator renames a property or adds one, so the schema
#        carries a monotonic version the UI can surface. Additive ALTER migration on
#        a pre-existing DB (see the store); a pre-version row reads back as 1.
#     2. ``FabricTypeError`` (a ``ValueError`` subclass) — raised by the store's
#        write-time property validation when a provided property value clashes with
#        its declared ``PropertyDef.type`` / ``enum_values``. Framework-agnostic on
#        purpose: the EE router maps it to a 422, agent-tool callers (which already
#        catch ValueError) degrade to a readable message, and the OSS store never
#        imports FastAPI.
# Updated: 2026-07-11 (self-serve-analysis S1) — transparent-analysis read engine,
#   additive and flag-gated (POCKETPAW_FABRIC_ANALYST):
#     1. FabricQuery gains optional aggregation fields: ``group_by`` (a property
#        key to group on), ``aggregate`` (count/sum/avg/min/max, defaults to
#        "count" when only group_by is set), ``aggregate_field`` (the numeric
#        property sum/avg/min/max read), ``ranges`` (RangeBucket numeric buckets
#        for the group key), and ``sort`` (order of the aggregate output). A
#        model validator normalizes/rejects inconsistent combinations (aggregate
#        without group_by, sum without aggregate_field, ranges/sort without
#        group_by, aggregation combined with ``path``).
#     2. ``QueryPlanStep {title, detail?, status?}`` — one human-readable
#        reasoning step of an aggregation run; the shape ripple's ReasoningTrace
#        consumes (never {label, count}).
#     3. FabricQueryResult gains ``aggregates`` (list of {key, value} group rows)
#        and ``steps`` (list[QueryPlanStep]); both default None so plain-query
#        responses are unchanged.
#     4. ``FabricAnalystDisabledError`` (a ``ValueError`` subclass) — raised by
#        the store when an aggregation query arrives while the
#        POCKETPAW_FABRIC_ANALYST flag is off; the EE router maps it to 422.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Cap on path depth (number of hops) for a multi-hop FabricQuery. A path is
# resolved iteratively, one DB round-trip per hop, with no cycle de-duplication
# across hops — so an unbounded path is both a latency risk and a way to walk a
# cyclic graph forever. Five hops covers every join the ontology layer needs in
# practice (the audit's hardest case is two); anything deeper is almost
# certainly a mistake and is rejected up front with a clear error.
MAX_HOPS = 5


class FabricTypeError(ValueError):
    """A write-time property value clashes with its declared type (ontology-operator-ux).

    Raised by :func:`pocketpaw.fabric.store.validate_object_properties` when a
    provided property value does not satisfy the ``PropertyDef`` declared for it
    on the object's ``ObjectType`` (wrong scalar kind, or a value outside a
    declared ``enum_values`` set). A ``ValueError`` subclass so the OSS store can
    stay framework-agnostic: the EE Fabric router catches it and returns HTTP 422,
    while agent-tool / connector callers that already guard ``ValueError`` fall
    back to a readable message instead of a 500.
    """


class FabricAnalystDisabledError(ValueError):
    """An aggregation query arrived while the analyst flag is off (self-serve-analysis S1).

    Raised by :meth:`pocketpaw.fabric.store.FabricStore.query` when a
    ``FabricQuery`` carries ``group_by`` / ``aggregate`` but the
    ``POCKETPAW_FABRIC_ANALYST`` settings flag is False (the default). The
    contract is FAIL-LOUD, not silent-degrade: the aggregation fields are
    accepted by the model (so the wire schema is stable and discoverable) and
    the STORE rejects the run with this error, which the EE Fabric router maps
    to HTTP 422 (code ``fabric.analyst_disabled``). A ``ValueError`` subclass so
    the OSS store stays framework-agnostic and agent-tool callers that already
    guard ``ValueError`` degrade to a readable message. Plain (non-aggregation)
    queries are never affected by the flag.
    """


def _gen_id(prefix: str) -> str:
    import random
    import string
    import time

    ts = hex(int(time.time() * 1000))[2:]
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{prefix}-{ts}-{rand}"


class PropertyDef(BaseModel):
    """Definition of a property on an object type."""

    name: str
    type: str = "string"  # string, number, boolean, date, enum
    required: bool = False
    description: str = ""
    enum_values: list[str] | None = None
    default: Any = None


class ObjectType(BaseModel):
    """Defines a category of business objects (Customer, Order, Product)."""

    id: str = Field(default_factory=lambda: _gen_id("ot"))
    name: str
    description: str = ""
    icon: str = "box"
    color: str = "#0A84FF"
    properties: list[PropertyDef] = Field(default_factory=list)
    # Tenancy (SZD-2): the owning workspace of this object TYPE. ``None`` =
    # legacy/global type written before per-type tenancy or by an OSS /
    # single-tenant caller; a scoped read still sees it (own rows + NULL).
    workspace_id: str | None = None
    # Schema version (ontology-operator-ux). Starts at 1 on define_type and is
    # bumped by FabricStore.update_type on a non-destructive schema change (a
    # property rename or an additive add). A pre-version DB row reads back as 1.
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class FabricObject(BaseModel):
    """An instance of an ObjectType."""

    id: str = Field(default_factory=lambda: _gen_id("obj"))
    type_id: str
    type_name: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    source_connector: str | None = None
    source_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class FabricLink(BaseModel):
    """A directional relationship between two objects."""

    id: str = Field(default_factory=lambda: _gen_id("lnk"))
    from_object_id: str
    to_object_id: str
    link_type: str  # "has_orders", "belongs_to", "purchased"
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class PathHop(BaseModel):
    """One step in a multi-hop traversal across the Fabric link graph.

    A hop says: from the objects reached so far, follow links of ``link_type``
    to the objects on the other end, optionally constraining those objects to a
    given ``object_type`` and/or matching a property ``filters`` bag.

    ``direction`` controls how the named link is read relative to the current
    frontier:

    - ``"out"`` (default) — follow ``from_object_id -> to_object_id``. This is
      the direction ``store.link(from_id, to_id, link_type)`` records, so it
      reads as "the current object HAS this link to the next object" (Deal
      --deal_for--> Customer).
    - ``"in"`` — follow ``to_object_id -> from_object_id`` (reverse).
    - ``"any"`` — match the link in either direction (the symmetric semantics
      the legacy single-hop ``linked_to`` uses).

    ``filters`` reuses the exact ``FabricQuery.filters`` shape (scalar =
    equality, operator-map = comparison) so the same parser applies at every
    hop.
    """

    link_type: str = Field(max_length=200)
    object_type: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    direction: Literal["out", "in", "any"] = "out"


class QueryPlanStep(BaseModel):
    """One human-readable reasoning step of an analysis run (self-serve-analysis S1).

    The transparent-analysis contract consumed by ripple's ReasoningTrace widget:
    exactly ``{title, detail?, status?}``. ``title`` is the short human sentence
    ("Grouped by category"), ``detail`` an optional elaboration ("4 groups from
    128 objects"), ``status`` an optional lifecycle marker — the read engine
    emits "done" for completed steps; "thinking" is reserved for streaming
    surfaces that render in-progress steps.
    """

    title: str
    detail: str | None = None
    status: Literal["thinking", "done"] | None = None


class RangeBucket(BaseModel):
    """One numeric bucket for a range-grouped aggregation (self-serve-analysis S1).

    Buckets the ``FabricQuery.group_by`` property by value: an object falls into
    the bucket when ``min <= value < max`` (min inclusive, max exclusive; either
    bound may be omitted for an open-ended bucket, but not both). ``label`` is
    the group key reported in the aggregate output; when omitted it is derived
    from the bounds ("100-500", "<100", ">=500"). Objects matching no bucket
    (including non-numeric / missing values) are dropped from the aggregation.
    """

    label: str | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _require_a_bound(self) -> RangeBucket:
        if self.min is None and self.max is None:
            raise ValueError("a range bucket needs at least one of min/max")
        return self

    def resolved_label(self) -> str:
        """The group key this bucket reports — explicit label or derived bounds."""
        if self.label:
            return self.label
        if self.min is None:
            return f"<{self.max:g}"
        if self.max is None:
            return f">={self.min:g}"
        return f"{self.min:g}-{self.max:g}"


# Aggregation functions the analyst read engine supports. ``count`` counts
# matching objects per group; the rest fold a numeric property
# (``aggregate_field``) per group.
AggregateFn = Literal["count", "sum", "avg", "min", "max"]

# Sort orders for the aggregate output rows.
AggregateSort = Literal["value_desc", "value_asc", "key_asc", "key_desc"]


class FabricQuery(BaseModel):
    """Query parameters for finding objects.

    Single-hop traversal (legacy, unchanged): set ``linked_to`` (+ optional
    ``link_type``) to find objects linked to a given object id.

    Multi-hop / path traversal (additive): set ``linked_to`` as the START
    object id and ``path`` to a list of :class:`PathHop` steps. The query walks
    the chain server-side and returns the objects reached at the FINAL hop (with
    that hop's ``object_type`` / ``filters`` applied). Top-level ``type_name`` /
    ``type_id`` / ``filters`` still constrain the terminal result set too, so
    "open Deals whose Customer competes_with a Competitor" is a single query:
    start at the Competitor, walk back, or start at the Deals and walk out — see
    ``FabricStore.query`` for the traversal contract. ``path`` and the legacy
    single-hop ``link_type`` are mutually exclusive (``path`` wins when both are
    present, and ``link_type`` is ignored — the per-hop ``link_type`` governs).

    Aggregation (self-serve-analysis S1, additive, flag-gated on
    POCKETPAW_FABRIC_ANALYST): set ``group_by`` (and optionally ``aggregate`` —
    it defaults to "count") to fold the workspace-scoped, filtered result set
    into per-group aggregate rows instead of object rows. ``aggregate_field``
    names the numeric property sum/avg/min/max read; ``ranges`` buckets a
    numeric group key; ``sort`` orders the aggregate rows (default: value
    descending). Aggregation composes with ``type_name``/``type_id``/``filters``
    /``linked_to`` but NOT with ``path`` (rejected — slice 1 aggregates the flat
    WHERE path only). Scope-then-aggregate: the workspace scope and filters are
    applied BEFORE grouping, so a cross-workspace object can never leak into a
    group total.
    """

    type_name: str | None = None
    type_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    linked_to: str | None = None
    link_type: str | None = None
    path: list[PathHop] = Field(default_factory=list)
    group_by: str | None = None
    aggregate: AggregateFn | None = None
    aggregate_field: str | None = None
    ranges: list[RangeBucket] | None = None
    sort: AggregateSort | None = None
    limit: int = 50
    offset: int = 0

    @field_validator("path")
    @classmethod
    def _cap_path_depth(cls, value: list[PathHop]) -> list[PathHop]:
        """Reject a path deeper than ``MAX_HOPS``.

        The walk in ``FabricStore.query`` is iterative and does NOT de-duplicate
        visited objects across hops, so a long (or cyclic-graph) path is a
        latency / runaway risk. Cap it at the model boundary with a clear error
        — the agent tool's outer ``except`` turns this ValueError into a readable
        message the LLM can act on, rather than letting it reach the DB.
        """
        if len(value) > MAX_HOPS:
            raise ValueError(
                f"path has {len(value)} hops; the maximum is {MAX_HOPS}. "
                "Express the join with fewer hops."
            )
        return value

    @model_validator(mode="after")
    def _normalize_aggregation(self) -> FabricQuery:
        """Normalize + validate the aggregation field combination (S1).

        - ``group_by`` alone implies ``aggregate="count"`` (the forgiving
          default a self-serve caller expects).
        - ``aggregate`` / ``aggregate_field`` / ``ranges`` / ``sort`` without
          ``group_by`` is inconsistent -> rejected with a readable error.
        - sum/avg/min/max fold a numeric property, so they REQUIRE
          ``aggregate_field``; ``count`` counts rows and must not carry one.
        - Aggregation over a ``path`` traversal is out of scope for slice 1 ->
          rejected (the flat WHERE path is the only aggregation surface).
        - ``group_by`` / ``aggregate_field`` must be plain identifier property
          names (alnum + underscore), mirroring the filter-key rule, so the
          error surfaces at the model boundary instead of deep in the store.
        """
        if self.group_by is not None and self.aggregate is None:
            self.aggregate = "count"
        if self.group_by is None:
            for name in ("aggregate", "aggregate_field", "ranges", "sort"):
                if getattr(self, name) is not None:
                    raise ValueError(f"{name} requires group_by to be set")
            return self
        if self.path:
            raise ValueError(
                "aggregation (group_by/aggregate) cannot be combined with a "
                "path traversal; aggregate the flat filtered set instead"
            )
        for label, name in (("group_by", self.group_by), ("aggregate_field", self.aggregate_field)):
            if name is not None and (not name or not all(c.isalnum() or c == "_" for c in name)):
                raise ValueError(f"Invalid {label} property name: {name!r}")
        if self.aggregate == "count":
            if self.aggregate_field is not None:
                raise ValueError('aggregate="count" does not take an aggregate_field')
        elif self.aggregate_field is None:
            raise ValueError(f'aggregate="{self.aggregate}" requires an aggregate_field')
        return self


class FabricQueryResult(BaseModel):
    """Result of a fabric query.

    Aggregation queries (self-serve-analysis S1) return ``objects=[]`` and
    populate ``aggregates`` — one ``{"key": <group>, "value": <aggregate>}``
    row per group — plus ``steps``, the human-readable reasoning trace
    (:class:`QueryPlanStep`). Both fields default to ``None`` so plain-query
    results are byte-compatible with the pre-S1 shape.
    """

    objects: list[FabricObject]
    total: int
    links: list[FabricLink] = Field(default_factory=list)
    aggregates: list[dict[str, Any]] | None = None
    steps: list[QueryPlanStep] | None = None
