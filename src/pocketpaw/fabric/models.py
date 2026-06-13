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

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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

    link_type: str
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


class FabricQueryResult(BaseModel):
    """Result of a fabric query."""

    objects: list[FabricObject]
    total: int
    links: list[FabricLink] = Field(default_factory=list)
