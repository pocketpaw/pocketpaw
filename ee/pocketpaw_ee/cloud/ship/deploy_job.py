# ee/pocketpaw_ee/cloud/ship/deploy_job.py — the arq entry point that deploys an
# app, plus the pure orchestrator behind it (SHIP-3).
#
# Same two-layer shape as the SHIP-2 provisioning pair (``provisioning.py`` +
# ``job.py``), kept in one module because the deploy wiring is thin:
#
#   * ``run_deploy`` — the orchestrator. Walks one ShipDeploy through
#     ``queued -> building -> releasing -> live`` (or ``failed``), emitting a
#     realtime event on every transition and persisting the app's new URL on
#     success. The engine arrives through an injected ``BoxSessionFactory``, so
#     the whole pipeline is testable against SHIP-1's zero-network fake
#     transport.
#   * ``deploy_app_job`` — the arq function. Loads the deploy, its app and its
#     box WORKSPACE-SCOPED, then hands off. It never raises for an operational
#     failure (the attempt is recorded ``failed`` and the job returns), so arq
#     does not retry-storm a genuinely broken box.
#
# The status vocabulary maps to real pipeline stages: ``building`` is set as the
# engine call goes out (Dokku creates the app, applies config, pulls the image),
# ``releasing`` once the engine has answered and the new container is being
# recorded, ``live`` when the app row carries the deployed image + URL.
#
# SECURITY: ``AppSpec`` is built with NO env — env VALUES are never stored by
# this entity (``env_refs`` holds names only), so a deploy can never echo a
# secret back through the engine. A failure summary comes from SHIP-1's
# ``CommandFailed``, whose command + stderr tail are redacted before the
# exception is constructed.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import ShipAppUpdated, ShipDeployStatusChanged
from pocketpaw_ee.cloud.ship import engine as ship_engine
from pocketpaw_ee.cloud.ship import store
from pocketpaw_ee.ship_engine.port import AppSpec, DeployRequest, ShipEngineError

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.ship import (
        ShipApp,
        ShipAppStatus,
        ShipBox,
        ShipDeploy,
        ShipDeployStatus,
    )

logger = logging.getLogger(__name__)

# A failure summary is a short operator hint, not a transcript — the full output
# stays on the box and is read through ``GET /ship/apps/{id}/logs``.
_SUMMARY_MAX_CHARS = 500


async def run_deploy(
    deploy: ShipDeploy,
    *,
    app: ShipApp,
    box: ShipBox,
    session_factory: ship_engine.BoxSessionFactory | None = None,
) -> ShipDeploy:
    """Take ``deploy`` to ``live`` (or ``failed``). Returns the updated doc.

    Never raises for an engine failure — a ``ShipEngineError`` is recorded on the
    attempt and the app is flipped ``failed``. A programming/infra error (a
    missing encryption key, a broken bus) still propagates, matching the
    provisioning orchestrator's posture.
    """
    factory = session_factory or ship_engine.box_session

    deploy = await _advance(deploy, "building")
    try:
        async with factory(box) as session:
            result = await session.engine.deploy_app(
                DeployRequest(app=AppSpec(name=app.name), image=deploy.image)
            )
    except ShipEngineError as exc:
        logger.warning("ship deploy failed for app=%s deploy=%s", app.id, deploy.id)
        await _mark_app(app, "failed")
        return await _advance(deploy, "failed", log_summary=_summary(exc))

    deploy = await _advance(deploy, "releasing")
    app = await store.record_app_deployed(app, image=result.image, app_url=result.app_url)
    await emit(ShipAppUpdated(data=_app_payload(app)))
    return await _advance(deploy, "live")


async def deploy_app_job(ctx: dict, deploy_id: str, workspace_id: str) -> dict:
    """arq function: run the deploy attempt ``deploy_id`` for ``workspace_id``.

    ``ctx`` is arq's job context (unused here). Returns a small status dict for
    observability; the durable outcome is the ShipDeploy ``status``.
    """
    deploy = await store.get_deploy(workspace_id, deploy_id)
    if deploy is None:
        logger.warning("ship deploy: attempt %s not found for workspace", deploy_id)
        return {"ok": False, "reason": "deploy_not_found"}

    app = await store.get_app(workspace_id, deploy.app_id)
    if app is None:
        updated = await _advance(deploy, "failed", log_summary="app no longer exists")
        return {"ok": False, "reason": "app_not_found", "status": updated.status}

    box = await store.get_box(workspace_id, app.box_id)
    if box is None:
        await _mark_app(app, "failed")
        updated = await _advance(deploy, "failed", log_summary="box no longer exists")
        return {"ok": False, "reason": "box_not_found", "status": updated.status}
    if box.status != "ready":
        await _mark_app(app, "failed")
        updated = await _advance(
            deploy, "failed", log_summary=f"box is {box.status}, not ready to accept a deploy"
        )
        return {"ok": False, "reason": "box_not_ready", "status": updated.status}

    updated = await run_deploy(deploy, app=app, box=box)
    return {"ok": updated.status == "live", "status": updated.status}


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


async def _advance(
    deploy: ShipDeploy, status: ShipDeployStatus, *, log_summary: str = ""
) -> ShipDeploy:
    """Persist a status transition and announce it."""
    deploy = await store.set_deploy_status(deploy, status, log_summary=log_summary)
    await emit(
        ShipDeployStatusChanged(
            data={
                "id": str(deploy.id),
                "workspace_id": deploy.workspace,
                "app_id": deploy.app_id,
                "status": deploy.status,
            }
        )
    )
    return deploy


async def _mark_app(app: ShipApp, status: ShipAppStatus) -> None:
    """Flip the app's own lifecycle status and announce it."""
    app = await store.set_app_status(app, status)
    await emit(ShipAppUpdated(data=_app_payload(app)))


def _app_payload(app: ShipApp) -> dict:
    """The event payload for an app write — ids + status + URLs, never secrets."""
    return {
        "id": str(app.id),
        "workspace_id": app.workspace,
        "box_id": app.box_id,
        "status": app.status,
        "urls": list(app.urls),
    }


def _summary(exc: ShipEngineError) -> str:
    """A short, already-redacted failure line for the attempt record."""
    return str(exc)[:_SUMMARY_MAX_CHARS]
