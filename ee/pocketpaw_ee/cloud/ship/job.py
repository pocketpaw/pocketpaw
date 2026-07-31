# ee/pocketpaw_ee/cloud/ship/job.py — the arq entry point that provisions a box.
#
# ``provision_box_job(ctx, box_id, workspace_id)`` is the durable job the web
# process enqueues after inserting a ``provisioning`` ShipBox. It loads the box
# (workspace-scoped), builds the real hcloud client + SSH readiness probe, and
# hands off to the pure ``provisioning.run_provision`` orchestrator (which owns
# the create/probe/idempotency logic and is unit-tested with fakes).
#
# The job itself is the THIN wiring layer: token resolution, real-seam
# construction, and the inter-attempt sleep. It never raises for an operational
# failure — ``run_provision`` records ``degraded`` on the box and returns — so
# arq marks the job done (no retry storm on a genuinely dead provider).
#
# The readiness probe reuses SHIP-1's ``AsyncSSHTransport`` + ``DokkuDriver``:
# it opens an SSH connection with the box's decrypted key and runs the driver's
# ``metrics``/version path; a successful ``dokku version`` means ready.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from pocketpaw_ee.cloud.ship import provisioning, store
from pocketpaw_ee.ship_engine.hcloud import (
    HcloudProvisioner,
    ProvisionError,
    build_hcloud_client,
)
from pocketpaw_ee.ship_engine.port import BoxHandle

logger = logging.getLogger(__name__)

_HCLOUD_TOKEN_ENV = "POCKETPAW_HCLOUD_TOKEN"


async def _ssh_dokku_ready(handle: BoxHandle, ssh_private_key: str) -> tuple[bool, str]:
    """Real readiness probe: SSH in with the box key, confirm ``dokku version``.

    Returns ``(ready, host_key)``. ``host_key`` is the box's SSH HOST key,
    captured trust-on-first-use during this probe so the caller can pin it on the
    ShipBox — every connect after provisioning verifies against it. It is the
    box's server identity, not a secret.

    Writes the client key to a private temp file (asyncssh reads a key path),
    connects via SHIP-1's ``AsyncSSHTransport``, and runs ``dokku version``. Any
    failure (still booting, connection refused) returns ``(False, "")``; the
    orchestrator retries.
    """
    from pocketpaw_ee.ship_engine.dokku import AsyncSSHTransport

    key_path = None
    try:
        fd, key_path = tempfile.mkstemp(prefix="paw-ship-", suffix=".key")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(ssh_private_key)
        # TRUST ON FIRST USE. The box was created seconds ago, so its host key
        # cannot be known in advance and asyncssh's default verification would
        # refuse every connection — which is exactly why no box could ever reach
        # ``ready``. Accept the key on this one probe, hand it back so the caller
        # pins it on the ShipBox, and every later connect verifies against it.
        transport = AsyncSSHTransport(
            handle.host,
            port=handle.ssh_port,
            username=handle.ssh_user,
            client_key_path=key_path,
            trust_on_first_use=True,
        )
        try:
            result = await transport.run("dokku version")
        finally:
            # In the finally, not on the success line — a probe that raised
            # mid-command used to leak an open asyncssh connection, and the
            # orchestrator retries up to 30 times per box.
            await _safe_close(transport)
        return (result.exit_code == 0, transport.captured_host_key)
    finally:
        if key_path and os.path.exists(key_path):
            os.unlink(key_path)


async def _safe_close(transport: Any) -> None:
    close = getattr(transport, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            logger.debug("ship probe transport aclose failed", exc_info=True)


async def provision_box_job(ctx: dict, box_id: str, workspace_id: str) -> dict:
    """arq function: provision the box ``box_id`` for ``workspace_id``.

    ``ctx`` is arq's job context (unused here). Returns a small status dict for
    observability; the durable outcome is the ShipBox ``status`` the orchestrator
    writes.
    """
    box = await store.get_box(workspace_id, box_id)
    if box is None:
        # Nothing to do — the box was deleted or never existed for this tenant.
        logger.warning("ship provision: box %s not found for workspace", box_id)
        return {"ok": False, "reason": "box_not_found"}

    # Build the provider client BEFORE handing off to the orchestrator. A
    # missing token (or any client-construction failure) raises ProvisionError
    # here, OUTSIDE run_provision — so it must be caught and turned into a
    # ``degraded`` box, or the box hangs in ``provisioning`` forever (the exact
    # "never hang" contract run_provision guarantees for failures it sees).
    token = os.environ.get(_HCLOUD_TOKEN_ENV, "").strip()
    try:
        provisioner = HcloudProvisioner(build_hcloud_client(token))
    except ProvisionError as exc:
        await store.mark_degraded(box, reason=str(exc))
        logger.warning("ship provision: client construction failed for box %s", box_id)
        return {"ok": False, "status": "degraded"}
    ssh_private_key = store.decrypt_ssh_key(box)

    updated = await provisioning.run_provision(
        box,
        provisioner=provisioner,
        ssh_public_key=box.ssh_public_key,
        ssh_private_key=ssh_private_key,
        probe=_ssh_dokku_ready,
        sleep=asyncio.sleep,
    )
    return {"ok": updated.status == "ready", "status": updated.status}
