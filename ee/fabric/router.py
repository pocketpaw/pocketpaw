# ee/fabric/router.py — FastAPI router for the Fabric ontology API.
# Created: 2026-03-28 — CRUD endpoints for object types, objects, links, queries, stats.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ee.fabric.models import FabricObject, FabricQuery, FabricQueryResult, ObjectType, PropertyDef
from ee.fabric.store import FabricStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Fabric"])

_DB_PATH = Path.home() / ".pocketpaw" / "fabric.db"


def _store() -> FabricStore:
    return FabricStore(_DB_PATH)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class DefineTypeRequest(BaseModel):
    name: str
    properties: list[PropertyDef]
    description: str = ""
    icon: str = "box"
    color: str = "#0A84FF"


class CreateObjectRequest(BaseModel):
    type_id: str
    properties: dict[str, Any] = {}
    source_connector: str | None = None
    source_id: str | None = None


class LinkRequest(BaseModel):
    from_id: str
    to_id: str
    link_type: str
    properties: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/fabric/types", response_model=list[ObjectType])
async def list_types():
    return await _store().list_types()


@router.post("/fabric/types", response_model=ObjectType, status_code=201)
async def define_type(req: DefineTypeRequest):
    return await _store().define_type(
        name=req.name,
        properties=req.properties,
        description=req.description,
        icon=req.icon,
        color=req.color,
    )


@router.post("/fabric/objects", response_model=FabricObject, status_code=201)
async def create_object(req: CreateObjectRequest):
    return await _store().create_object(
        type_id=req.type_id,
        properties=req.properties,
        source_connector=req.source_connector,
        source_id=req.source_id,
    )


@router.get("/fabric/objects/{obj_id}", response_model=FabricObject)
async def get_object(obj_id: str):
    obj = await _store().get_object(obj_id)
    if not obj:
        raise HTTPException(404, "Object not found")
    return obj


@router.post("/fabric/query", response_model=FabricQueryResult)
async def query_fabric(q: FabricQuery):
    return await _store().query(q)


@router.post("/fabric/links", status_code=201)
async def create_link(req: LinkRequest):
    return await _store().link(
        from_id=req.from_id,
        to_id=req.to_id,
        link_type=req.link_type,
        properties=req.properties,
    )


@router.get("/fabric/stats")
async def fabric_stats():
    return await _store().stats()
