# ee/cloud/fabric_ingest/router.py — the connector→Fabric transform surface.
# Created: 2026-07-11 (feat/real-pipeline-s1) — author/list/delete the
#   workspace's FabricIngestConfig mappings + a run-now endpoint that fires
#   one mapping's ingest immediately (firestore OR connector source; the
#   gcalendar connector is the first real connector adopter).
#
# RBAC copies ee/fabric/router.py exactly: router-level ``require_license`` +
# ``require_plan_feature("fabric")`` (the whole surface is business-tier+),
# then per-route ``require_action_any_workspace`` — reads need ``fabric.read``
# (MEMBER), every mutation (author, delete, run-now) needs ``fabric.write``
# (MEMBER; a caller outside the workspace 403s). Tenancy: every handler takes
# the caller's active workspace via ``current_workspace_id`` and threads it
# into the service — the workspace NEVER travels in a request body, so a
# caller can only ever author/run its own tenant's mappings (cloud rule §7).
#
# The router is thin (cloud chokepoint rule): all FabricIngestConfig reads and
# writes live in service.py (``list_mappings`` / ``upsert_mapping`` /
# ``delete_mapping`` / ``ingest_collection``); DTOs live in dto.py. No prefix
# on the router — ee/cloud/__init__.py mounts it under ``/api/v1`` like the
# fabric router.
#
# Run-now semantics: ``ingest_collection`` never raises for a misconfigured
# mapping — a missing mapping, an unconnected/disabled connector, or an
# unregistered ingestor all come back as ``status="error"`` in the result
# body (HTTP 200), matching the sweep's per-collection isolation contract.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_plan_feature
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.fabric_ingest import service
from pocketpaw_ee.cloud.fabric_ingest.dto import (
    MappingResponse,
    MappingsListResponse,
    MappingUpsertRequest,
    RunNowRequest,
    RunNowResponse,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Fabric Ingest"],
    dependencies=[Depends(require_license), Depends(require_plan_feature("fabric"))],
)


@router.get(
    "/fabric/ingest/mappings",
    response_model=MappingsListResponse,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def list_mappings(
    workspace_id: str = Depends(current_workspace_id),
) -> MappingsListResponse:
    """List the caller's workspace's authored transform mappings."""
    mappings = await service.list_mappings(workspace_id)
    return MappingsListResponse(
        mappings=[MappingResponse.model_validate(m) for m in mappings]
    )


@router.post(
    "/fabric/ingest/mappings",
    response_model=MappingResponse,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def upsert_mapping(
    req: MappingUpsertRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> MappingResponse:
    """Author one mapping (create-or-replace, keyed on ``collection``).

    For a connector-source mapping set ``source_kind="connector"`` and keep
    ``collection`` = the connector name (``connector_id`` defaults to it).
    """
    stored = await service.upsert_mapping(workspace_id, req.model_dump())
    return MappingResponse.model_validate(stored)


@router.delete(
    "/fabric/ingest/mappings",
    status_code=204,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def delete_mapping(
    collection: str = Query(..., min_length=1, description="The mapping's routing key"),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    """Remove the mapping keyed on ``collection`` (404 when it doesn't exist).

    ``collection`` rides a query param, not a path segment — Firestore
    collection paths can contain ``/``.
    """
    removed = await service.delete_mapping(workspace_id, collection)
    if not removed:
        raise NotFound("fabric_ingest.mapping_not_found", "No such mapping")


@router.post(
    "/fabric/ingest/run",
    response_model=RunNowResponse,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def run_now(
    req: RunNowRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> RunNowResponse:
    """Run one mapping's ingest immediately for the caller's workspace.

    Returns the ingest result envelope; a misconfigured mapping (no mapping,
    connector not connected, ingestor unregistered) reports ``status="error"``
    in the body rather than an HTTP 5xx — the same never-raise contract the
    background sweep relies on.
    """
    result = await service.ingest_collection(workspace_id, req.collection)
    return RunNowResponse.model_validate(result)
