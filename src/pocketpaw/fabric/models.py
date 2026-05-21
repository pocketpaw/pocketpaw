# Fabric data models — Pydantic models for the ontology layer.
# Created: 2026-03-28

from __future__ import annotations

from datetime import datetime
from typing import Any

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


class FabricQuery(BaseModel):
    """Query parameters for finding objects."""

    type_name: str | None = None
    type_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    linked_to: str | None = None
    link_type: str | None = None
    limit: int = 50
    offset: int = 0


class FabricQueryResult(BaseModel):
    """Result of a fabric query."""

    objects: list[FabricObject]
    total: int
    links: list[FabricLink] = Field(default_factory=list)
