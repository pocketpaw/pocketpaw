# ee/pocketpaw_ee/cloud/ship/enqueue.py — the web-process side of provisioning.
#
# ``enqueue_provision`` mints a fresh box keypair, inserts a ``provisioning``
# ShipBox (the private key Fernet-encrypted by the store), and enqueues
# ``provision_box_job`` on the shared arq pool. It mirrors the chat-run /
# workspace-job enqueue contract: the arq queue selector kwarg pitfall is
# avoided by enqueuing positionally, exactly like ``jobs/service.py``.
#
# Returns the created ShipBox so the caller (the SHIP-3 router) can 202 it with a
# pollable box id.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.
# Changed 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): added
# ``enqueue_deploy`` — the same web-side contract for the deploy pipeline. It
# inserts a ``queued`` ShipDeploy and dispatches ``deploy_app_job`` positionally,
# so ``POST /ship/apps/{id}/deploy`` returns immediately with a pollable id.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.ship import store
from pocketpaw_ee.cloud.ship.deploy_job import deploy_app_job  # noqa: F401 — registration ref
from pocketpaw_ee.cloud.ship.job import provision_box_job  # noqa: F401 — registration ref
from pocketpaw_ee.ship_engine.keygen import generate_box_keypair

logger = logging.getLogger(__name__)


async def enqueue_provision(
    *,
    workspace_id: str,
    provider: str = "hcloud",
    server_type: str,
    region: str,
    pool_factory=None,
) -> object:
    """Create a box row + keypair and enqueue its provisioning job.

    ``pool_factory`` is an injection seam for tests (an async callable returning
    an arq pool). In production it defaults to the chat-run executor's shared
    pool getter, so ship jobs ride the same worker + Redis as everything else.
    Returns the inserted ShipBox.
    """
    keypair = generate_box_keypair(comment=f"paw-ship-{workspace_id[:8]}")
    box = await store.create_provisioning_box(
        workspace_id=workspace_id,
        provider=provider,
        server_type=server_type,
        region=region,
        ssh_private_key=keypair.private_key_openssh,
        ssh_public_key=keypair.public_key_openssh,
    )

    pool = await _resolve_pool(pool_factory)
    try:
        # Positional enqueue — no ``queue=`` kwarg (arq forwards non-control
        # kwargs to the job function; see jobs/service.py's note). Ship jobs ride
        # the shared default queue on the one worker process.
        await pool.enqueue_job("provision_box_job", str(box.id), workspace_id)
    except Exception:
        logger.exception("ship: enqueue failed for box %s", box.id)
        raise
    return box


async def enqueue_deploy(
    *,
    workspace_id: str,
    app_id: str,
    image: str,
    pool_factory=None,
) -> object:
    """Insert a ``queued`` deploy attempt and enqueue its deploy job.

    ``image`` is pinned onto the attempt at enqueue so a later app edit never
    rewrites what this attempt shipped. Returns the inserted ShipDeploy — the
    router answers with it immediately; the arq job advances the status.
    """
    deploy = await store.create_deploy(workspace_id=workspace_id, app_id=app_id, image=image)

    pool = await _resolve_pool(pool_factory)
    try:
        # Positional enqueue — see the note in ``enqueue_provision``.
        await pool.enqueue_job("deploy_app_job", str(deploy.id), workspace_id)
    except Exception:
        logger.exception("ship: deploy enqueue failed for deploy %s", deploy.id)
        raise
    return deploy


async def _resolve_pool(pool_factory):
    if pool_factory is not None:
        return await pool_factory()
    from pocketpaw_ee.cloud.chat.runs.arq_executor import _get_pool

    return await _get_pool()
