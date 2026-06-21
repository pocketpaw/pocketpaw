# Discovery — service (cloud 4-file rule §5).
# Created: 2026-06-21 (SZD finish slice F1 / feat/szd-finish-core) — the
#   workspace-discovery TRIGGER. ``run(workspace_id, user_id, body)`` is the
#   front door that today only a script could reach: it enumerates the
#   workspace's ENABLED connectors (``connectors.service.list_connectors``),
#   builds ``DiscoveryRunOptions``, and FIRES
#   ``orchestrate.run_discovery_and_propose`` as a background task
#   (``asyncio.create_task``, mirroring chat/router's fire-and-forget) so the
#   HTTP request returns 202 immediately instead of blocking on connector
#   sampling + digest. The orchestrator stages its proposals durably as pending
#   Instinct Actions, so the trigger's response only needs the optimistic
#   ``run_id`` — the action ids surface separately in the pending-actions list
#   the ApprovalsPanel already polls.
#
#   Sovereignty / tenancy: validate the body at entry (rule §6); pass the
#   resolved ``workspace_id`` / ``user_id`` straight into the orchestrator,
#   which re-asserts tenancy at its own entry. No Beanie writes happen here
#   (the orchestrator + proposal services own all persistence), so this entity
#   needs no import-linter ``models.*`` contract.

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from pocketpaw_ee.cloud.discovery.dto import DiscoveryRunRequest, DiscoveryRunResponse
from pocketpaw_ee.discovery.orchestrate import (
    DiscoveryProposalResult,
    run_discovery_and_propose,
)
from pocketpaw_ee.discovery.run import DiscoveryRunOptions

logger = logging.getLogger(__name__)


def _log_task_result(task: asyncio.Task[DiscoveryProposalResult]) -> None:
    """Done-callback for the fire-and-forget discovery task.

    ``asyncio.create_task`` swallows exceptions unless the task is awaited or
    its result is inspected. Without this, a crash mid-digest would yield no
    proposals AND no log line (risk R5 in the build plan). Here we surface the
    failure (or the staged-proposal summary) without ever re-raising into the
    event loop.
    """
    try:
        result = task.result()
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        logger.warning("discovery.run task cancelled")
    except Exception:  # noqa: BLE001 - background task must not crash the loop
        logger.exception("discovery.run task failed")
    else:
        logger.info(
            "discovery.run staged proposals run_id=%s materialised=%d skipped=%d rules=%d",
            result.run_id,
            len(result.materialised_types),
            len(result.skipped_types),
            len(result.instinct_action_ids),
        )


async def run(
    workspace_id: str,
    user_id: str,
    body: DiscoveryRunRequest,
) -> dict:
    """Trigger a workspace-discovery run and return immediately (202-shaped).

    Steps:
      1. Validate the body at entry (rule §6 — internal callers re-parse).
      2. Enumerate the workspace's ENABLED connectors (the request may override
         with an explicit ``connector_ids`` list; the common UI path leaves it
         ``None`` and lets the service resolve it server-side). Disabled
         connectors are excluded.
      3. Build ``DiscoveryRunOptions`` (only ``sample_cap`` is exposed in v1 —
         ``DiscoveryRunOptions`` has no ``digester_kind``; structured-vs-
         unstructured is chosen inside ``DiscoveryRun``).
      4. FIRE the orchestrator as a background task and return the optimistic
         ``run_id`` now. The proposals land as pending Instinct Actions.

    Returns a wire dict (rule §5) shaped like ``DiscoveryRunResponse``. Because
    the run is fire-and-forget, the action-id fields are EMPTY in the response;
    ``run_id`` is a dispatch token for optimistic UI confirmation (distinct from
    the orchestrator's internal proposal run_id, which tags the staged Actions).
    """
    # Lazy import to avoid a router→service→connectors import cycle at module load.
    from pocketpaw_ee.cloud.connectors import service as connectors_service

    body = DiscoveryRunRequest.model_validate(body)

    if body.connector_ids is not None:
        connector_ids = list(body.connector_ids)
    else:
        rows = await connectors_service.list_connectors(workspace_id, user_id=user_id)
        connector_ids = [r.name for r in rows if r.enabled]

    opts = DiscoveryRunOptions(sample_cap=body.sample_cap or DiscoveryRunOptions().sample_cap)

    run_id = uuid4().hex

    task = asyncio.create_task(
        run_discovery_and_propose(workspace_id, user_id, connector_ids, opts)
    )
    task.add_done_callback(_log_task_result)

    logger.info(
        "discovery.run dispatched run_id=%s workspace=%s connectors=%d",
        run_id,
        workspace_id,
        len(connector_ids),
    )

    # no-event: the orchestrator stages durable Instinct Actions (each emits its
    # own proposal event); a duplicate trigger-level event would desync nothing.
    return DiscoveryRunResponse(run_id=run_id).model_dump()
