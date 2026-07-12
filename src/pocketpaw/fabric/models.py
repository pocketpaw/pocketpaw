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
# Updated: 2026-07-10 (FST-1 — Fabric source-truth schema) — added ``Statement``
#   and ``SourceRef``: the provenance layer for the source-truth chain. A
#   Statement records ONE observed (object, property, value) claim with full
#   provenance (who wrote it — ``writer_class``; where it came from —
#   ``source_ref_id``; bitemporal fields — observed/recorded/valid_from/
#   valid_to) plus a curation rank. A SourceRef identifies WHERE a statement
#   came from (a connector run, a document, a human actor, an agent session)
#   and is deduplicated on that identity by ``FabricStore.upsert_source``.
#   Both models carry ``workspace_id`` (W4a tenancy, same type/default as
#   ObjectType.workspace_id: ``str | None = None``); on SourceRef it is PART
#   of the dedup identity — the same source identity in two workspaces is two
#   rows, so tenant data never rendezvouses on a shared SourceRef.
#   ADDITIVE ONLY: nothing in the existing read path (FabricObject.properties,
#   fabric_query, widgets, projections) consumes these yet — statements are
#   inert until the ``fabric_source_truth_mode`` flag (default "off") gains
#   shadow/enforce semantics in later slices. The flat properties dict remains
#   the primary read path.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Cap on path depth (number of hops) for a multi-hop FabricQuery. A path is
# resolved iteratively, one DB round-trip per hop, with no cycle de-duplication
# across hops — so an unbounded path is both a latency risk and a way to walk a
# cyclic graph forever. Five hops covers every join the ontology layer needs in
# practice (the audit's hardest case is two); anything deeper is almost
# certainly a mistake and is rejected up front with a clear error.
MAX_HOPS = 5


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


class SourceRef(BaseModel):
    """The identity of WHERE a :class:`Statement` came from (FST-1).

    One row per distinct source identity: a connector run, a document, a
    human actor, or an agent session. ``FabricStore.upsert_source`` dedups on
    the identity tuple ``(kind, connector, run_id, document_uri, actor_id,
    session_id, workspace_id)`` — calling it twice with the same identity
    returns the same row, so statements from the same source share one
    SourceRef. ``workspace_id`` IS part of the identity: the same source seen
    from two workspaces is two rows (tenancy isolation beats dedup).

    The identity fields are a union across the four kinds; only the ones
    relevant to a given ``kind`` are expected to be set (e.g. a
    ``connector_run`` carries ``connector`` + ``run_id``; a ``document``
    carries ``document_uri``; a ``human_actor`` carries ``actor_id``; an
    ``agent_session`` carries ``session_id``). Unset fields are ``None`` and
    still participate in the identity (as "absent").
    """

    id: str = Field(default_factory=lambda: _gen_id("src"))
    kind: Literal["connector_run", "document", "human_actor", "agent_session"]
    connector: str | None = None
    run_id: str | None = None
    document_uri: str | None = None
    actor_id: str | None = None
    session_id: str | None = None
    retrieved_at: datetime | None = None
    # Tenancy (W4a semantics, carried on the model like ObjectType.workspace_id):
    # the owning workspace. ``None`` = OSS / single-tenant caller. Unlike the
    # other tables, this is PART OF THE DEDUP IDENTITY (see the docstring).
    workspace_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class Statement(BaseModel):
    """One observed (object, property, value) claim with provenance (FST-1).

    Statements are APPEND-ONLY: the store exposes no update/delete verbs for
    them (rank changes land in a later slice as new curation writes). Nothing
    reads statements in production paths yet — the flat
    ``FabricObject.properties`` dict remains the primary read path until the
    ``fabric_source_truth_mode`` flag gains shadow/enforce semantics.

    Bitemporal fields:

    - ``observed_at`` — when the source says the value was true / was seen.
    - ``recorded_at`` — when THIS store recorded the claim (stamped on write).
    - ``valid_from`` / ``valid_to`` — the validity interval of the claim;
      ``valid_to=None`` means "still valid as far as this statement knows".

    ``rank`` is the curation knob resolvers will consume later:
    ``preferred`` outranks ``normal`` outranks ``deprecated``; ``pinned``
    marks a statement a human explicitly fixed in place.
    """

    id: str = Field(default_factory=lambda: _gen_id("stm"))
    object_id: str
    property: str
    value: Any = None
    source_ref_id: str
    writer_class: Literal["human", "connector", "mirror", "agent", "inferred"]
    observed_at: datetime = Field(default_factory=datetime.now)
    recorded_at: datetime = Field(default_factory=datetime.now)
    valid_from: datetime = Field(default_factory=datetime.now)
    valid_to: datetime | None = None
    rank: Literal["preferred", "normal", "deprecated"] = "normal"
    rank_reason: str | None = None
    pinned: bool = False
    # Tenancy (W4a semantics): the owning workspace, stamped on append.
    # ``None`` = OSS / single-tenant caller; a scoped read still sees NULL
    # rows (own rows + legacy NULL — the standard W4a read scope).
    workspace_id: str | None = None


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
    """

    type_name: str | None = None
    type_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    linked_to: str | None = None
    link_type: str | None = None
    path: list[PathHop] = Field(default_factory=list)
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


class FabricQueryResult(BaseModel):
    """Result of a fabric query."""

    objects: list[FabricObject]
    total: int
    links: list[FabricLink] = Field(default_factory=list)
