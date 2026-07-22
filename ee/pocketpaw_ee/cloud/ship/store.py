# ee/pocketpaw_ee/cloud/ship/store.py — the /ship persistence seam.
#
# The ONLY module that reads/writes the ``ShipBox`` / ``ShipApp`` / ``ShipDeploy``
# Beanie documents (the same one-module-owns-one-doc isolation the credit /
# litellm-key docs use; the import-linter "Ship" contract keeps the docs off the
# router / dto / domain layers). It hides the Fernet envelope on the SSH private
# key: callers pass/receive PLAINTEXT keys, the doc stores CIPHERTEXT. Every read
# is workspace-scoped — an id alone is never trusted without its owning
# workspace.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.
# Changed 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): added the app + deploy
# halves — ``list_boxes``, the ``ShipApp`` CRUD used by the HTTP surface, the
# ``ShipDeploy`` attempt log the arq deploy job advances, and the two
# ``park_*_destroy`` writers that record a parked teardown without executing it.

from __future__ import annotations

from datetime import UTC, datetime

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud.models.ship import (
    ShipApp,
    ShipAppStatus,
    ShipBox,
    ShipBoxStatus,
    ShipBuildPath,
    ShipDeploy,
    ShipDeployStatus,
)


async def create_provisioning_box(
    *,
    workspace_id: str,
    provider: str,
    server_type: str,
    region: str,
    ssh_private_key: str,
    ssh_public_key: str,
) -> ShipBox:
    """Insert a fresh box in ``provisioning`` and return it.

    ``ssh_private_key`` is PLAINTEXT in; it is Fernet-encrypted before the doc is
    written (encryption requires ``CLOUD_ENCRYPTION_KEY`` — a missing key raises
    a clear setup error rather than persisting a secret in the clear). The public
    key is stored as-is (not secret).
    """
    box = ShipBox(
        workspace=workspace_id,
        provider=provider,
        server_type=server_type,
        region=region,
        status="provisioning",
        ssh_private_key_enc=crypto.encrypt(ssh_private_key),
        ssh_public_key=ssh_public_key,
    )
    await box.insert()
    return box


def _as_object_id(value: str) -> PydanticObjectId | None:
    """Coerce a path-segment id to an ObjectId, or None when it is malformed.

    A caller-supplied id reaches ``get`` straight off the URL, so a garbage
    segment must read as "not found", never as a 500 out of bson.
    """
    try:
        return PydanticObjectId(value)
    except (InvalidId, TypeError, ValueError):
        return None


async def get_box(workspace_id: str, box_id: str) -> ShipBox | None:
    """Load one box, workspace-scoped. A cross-tenant id yields None."""
    oid = _as_object_id(box_id)
    if oid is None:
        return None
    box = await ShipBox.get(oid)
    if box is None or box.workspace != workspace_id:
        return None
    return box


async def list_boxes(workspace_id: str) -> list[ShipBox]:
    """Every box the workspace owns, newest first. Tenant-filtered read."""
    return await ShipBox.find(ShipBox.workspace == workspace_id).sort("-createdAt").to_list()


async def mark_ready(
    box: ShipBox, *, server_id: str, ip: str, price_monthly: float | None
) -> ShipBox:
    """Record the provider facts and flip the box to ``ready``."""
    box.server_id = server_id
    box.ip = ip
    box.price_monthly = price_monthly
    box.status = "ready"
    box.status_reason = None
    await box.save()
    return box


async def mark_server_created(box: ShipBox, *, server_id: str, ip: str) -> ShipBox:
    """Persist the provider server id + IP the moment the create returns, BEFORE
    readiness. This is the idempotency anchor: a retry sees ``server_id`` set and
    never creates a second server."""
    box.server_id = server_id
    box.ip = ip
    await box.save()
    return box


async def mark_degraded(box: ShipBox, *, reason: str) -> ShipBox:
    """Flip the box to ``degraded`` with a fixed, safe reason string."""
    box.status = "degraded"
    box.status_reason = reason
    await box.save()
    return box


async def set_status(box: ShipBox, status: ShipBoxStatus) -> ShipBox:
    """Set an arbitrary lifecycle status (used by teardown → ``destroyed``)."""
    box.status = status
    await box.save()
    return box


def decrypt_ssh_key(box: ShipBox) -> str:
    """Decrypt the box's SSH private key for driver use. Never logged/serialized."""
    return crypto.decrypt(box.ssh_private_key_enc)


async def park_box_destroy(box: ShipBox, *, proposal_id: str) -> ShipBox:
    """Record a PARKED teardown on the box. Destroys nothing.

    The box keeps its lifecycle ``status`` — the frozen ``BoxOut`` status
    vocabulary has no ``pending`` member, and a parked teardown has not changed
    what the box actually is. ``pending_destroy_proposal_id`` is the pending
    marker.
    """
    box.pending_destroy_proposal_id = proposal_id
    await box.save()
    return box


# --------------------------------------------------------------------------- #
# Apps
# --------------------------------------------------------------------------- #


async def create_app(
    *,
    workspace_id: str,
    box_id: str,
    name: str,
    build_path: ShipBuildPath,
    git_ref: str,
    image: str,
    env_refs: list[str],
    prod: bool,
) -> ShipApp:
    """Insert a fresh app in ``created``. ``env_refs`` are NAMES only."""
    app = ShipApp(
        workspace=workspace_id,
        box_id=box_id,
        name=name,
        build_path=build_path,
        git_ref=git_ref,
        image=image,
        env_refs=list(env_refs),
        prod=prod,
    )
    await app.insert()
    return app


async def get_app(workspace_id: str, app_id: str) -> ShipApp | None:
    """Load one app, workspace-scoped. A cross-tenant id yields None."""
    oid = _as_object_id(app_id)
    if oid is None:
        return None
    app = await ShipApp.get(oid)
    if app is None or app.workspace != workspace_id:
        return None
    return app


async def find_app_by_name(workspace_id: str, box_id: str, name: str) -> ShipApp | None:
    """Look an app up by its engine-side name within one box. Tenant-filtered."""
    return await ShipApp.find_one(
        ShipApp.workspace == workspace_id,
        ShipApp.box_id == box_id,
        ShipApp.name == name,
    )


async def list_apps(workspace_id: str, *, box_id: str | None = None) -> list[ShipApp]:
    """Every app the workspace owns (optionally one box's), newest first."""
    criteria = [ShipApp.workspace == workspace_id]
    if box_id:
        criteria.append(ShipApp.box_id == box_id)
    return await ShipApp.find(*criteria).sort("-createdAt").to_list()


async def set_app_status(app: ShipApp, status: ShipAppStatus) -> ShipApp:
    """Set the app's lifecycle status."""
    app.status = status
    await app.save()
    return app


async def record_app_deployed(app: ShipApp, *, image: str, app_url: str) -> ShipApp:
    """Flip the app ``live`` and merge the engine-reported URL into ``urls``."""
    app.status = "live"
    app.image = image
    if app_url and app_url not in app.urls:
        app.urls.append(app_url)
    await app.save()
    return app


async def record_app_domain(app: ShipApp, *, domain: str, url: str) -> ShipApp:
    """Record a routed domain (and the URL it serves on) against the app."""
    if domain not in app.domains:
        app.domains.append(domain)
    if url and url not in app.urls:
        app.urls.append(url)
    await app.save()
    return app


async def record_app_db(app: ShipApp, *, service: str, env_var: str) -> ShipApp:
    """Record the linked database service + the env var NAME the link injected.

    The connection string is a secret and is never stored — SHIP-1's ``DbResult``
    deliberately exposes only the variable's name.
    """
    app.db_service = service
    app.db_env_var = env_var
    await app.save()
    return app


async def park_app_destroy(app: ShipApp, *, proposal_id: str) -> ShipApp:
    """Record a PARKED teardown on the app. Destroys nothing."""
    app.pending_destroy_proposal_id = proposal_id
    await app.save()
    return app


# --------------------------------------------------------------------------- #
# Deploys
# --------------------------------------------------------------------------- #


async def create_deploy(*, workspace_id: str, app_id: str, image: str) -> ShipDeploy:
    """Insert a ``queued`` deploy attempt, ``started_at`` stamped at acceptance."""
    deploy = ShipDeploy(
        workspace=workspace_id,
        app_id=app_id,
        status="queued",
        started_at=datetime.now(UTC),
        image=image,
    )
    await deploy.insert()
    return deploy


async def get_deploy(workspace_id: str, deploy_id: str) -> ShipDeploy | None:
    """Load one deploy attempt, workspace-scoped. A cross-tenant id yields None."""
    oid = _as_object_id(deploy_id)
    if oid is None:
        return None
    deploy = await ShipDeploy.get(oid)
    if deploy is None or deploy.workspace != workspace_id:
        return None
    return deploy


async def list_deploys(workspace_id: str, app_id: str, *, limit: int = 50) -> list[ShipDeploy]:
    """One app's deploy attempts, newest first. Tenant-filtered read."""
    return (
        await ShipDeploy.find(
            ShipDeploy.workspace == workspace_id,
            ShipDeploy.app_id == app_id,
        )
        .sort("-createdAt")
        .limit(limit)
        .to_list()
    )


async def set_deploy_status(
    deploy: ShipDeploy,
    status: ShipDeployStatus,
    *,
    log_summary: str = "",
) -> ShipDeploy:
    """Advance a deploy attempt. Terminal states stamp ``finished_at``."""
    deploy.status = status
    if log_summary:
        deploy.log_summary = log_summary
    if status in ("live", "failed"):
        deploy.finished_at = datetime.now(UTC)
    await deploy.save()
    return deploy
