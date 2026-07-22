# ee/pocketpaw_ee/cloud/ship/store.py — the ShipBox persistence seam.
#
# The ONLY module that reads/writes the ``ShipBox`` Beanie document (the same
# one-service-owns-one-doc isolation the credit / litellm-key docs use; an
# import-linter contract will keep the doc off every other layer). It hides the
# Fernet envelope on the SSH private key: callers pass/receive PLAINTEXT keys,
# the doc stores CIPHERTEXT. Every read is workspace-scoped — a box id alone is
# never trusted without its owning workspace.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.

from __future__ import annotations

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud.models.ship import ShipBox, ShipBoxStatus


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


async def get_box(workspace_id: str, box_id: str) -> ShipBox | None:
    """Load one box, workspace-scoped. A cross-tenant id yields None."""
    box = await ShipBox.get(box_id)
    if box is None or box.workspace != workspace_id:
        return None
    return box


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
