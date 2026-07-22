# ee/pocketpaw_ee/cloud/models/ship.py — the provisioned-box document for /ship.
#
# One Beanie document backs a workspace's managed deploy boxes:
#
#   * ``ShipBox`` — one row per provisioned box. Records the provider, the
#     provider-side server id, the reachable IP, the lifecycle status, the
#     monthly price captured at provision time, and an ENCRYPTED SSH private
#     key (Fernet, via ``cloud._core.crypto``) the engine driver uses to reach
#     the box over SSH. The key is the box's blast radius, so it never lives in
#     plaintext at rest and never leaves this layer in a DTO — the ship service
#     decrypts it only to hand it to the SHIP-1 driver.
#
# WHY store the key on the box doc (encrypted) rather than the connector state
# store: the connector state store is keyed on connector NAME + workspace and
# is owned by ``connectors/service.py`` for the external-integration lifecycle;
# a provisioned box is not a connector. The box's SSH credential is 1:1 with the
# box row and shares its lifecycle (minted at provision, destroyed with the
# box), so it belongs on the box doc — encrypted at rest with the same
# deployment Fernet key (``CLOUD_ENCRYPTION_KEY``) the connector/meetings
# secrets already use.
#
# Status lifecycle: ``provisioning`` → ``ready`` (server up, SSH reachable,
# ``dokku version`` answered) or ``degraded`` (a terminal provision failure,
# ``status_reason`` set). ``destroyed`` is the post-teardown terminal state.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new entity. Registered
# in ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``ship_boxes`` collection. Only
# ``ee.cloud.ship.service`` (SHIP-3) and the ``provision_box`` builtin job
# (SHIP-2) read/write this document — the same one-service-owns-one-doc
# isolation the credit / litellm-key / foresight docs use.

from __future__ import annotations

from typing import Literal

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument

# The box lifecycle states. ``provisioning`` is the birth state the enqueue
# writes; the job advances it to exactly one terminal-ish state.
ShipBoxStatus = Literal["provisioning", "ready", "degraded", "destroyed"]


class ShipBox(TimestampedDocument):
    """One managed-deploy box provisioned for a workspace.

    Tenancy: ``workspace`` is REQUIRED and every read filters on it (the ship
    service asserts it). ``server_id`` is the provider's server identifier
    (empty until the provider create call returns) — it is also the
    idempotency anchor: the provision job never creates a second server when a
    row already carries one.

    ``ssh_private_key_enc`` is a Fernet token (never plaintext); ``ssh_user`` /
    ``ssh_port`` are the coordinates the SHIP-1 ``BoxHandle`` needs. The public
    key is not stored — it is derived from config for the cloud-init authorize
    step and is not secret.

    ``price_monthly`` is the provider's quoted monthly price for the chosen
    server type, captured at provision time (provenance, not a live bill).
    ``status_reason`` carries the failure detail when ``status == 'degraded'``
    (a fixed, PII-free string — never raw provider payloads).
    """

    # Tenancy filter — every ship read scopes on this. Indexed (non-unique: a
    # workspace owns many boxes).
    workspace: Indexed(str)  # type: ignore[valid-type]
    # Provider discriminator; "hcloud" is the only v1 driver.
    provider: str = "hcloud"
    # The provider-side server id. Empty string until the create call returns;
    # once set it is the idempotency guard against a double-create.
    server_id: str = ""
    # The box's reachable IPv4 (empty until the server has one).
    ip: str = ""
    # Lifecycle state (see module comment).
    status: ShipBoxStatus = "provisioning"
    # Failure detail when status == "degraded"; a fixed safe string, never a
    # raw provider error payload.
    status_reason: str | None = None
    # SSH coordinates for the SHIP-1 BoxHandle.
    ssh_user: str = "root"
    ssh_port: int = 22
    # The box's SSH PRIVATE key, Fernet-encrypted at rest. Decrypted only inside
    # the ship service to build a driver; never serialized into a DTO or log.
    ssh_private_key_enc: str = ""
    # The box's SSH PUBLIC key (not secret) — authorized on the box via
    # cloud-init and re-supplied to the provisioner on a retry. Stored so the
    # public half never has to be re-derived from the encrypted private key.
    ssh_public_key: str = ""
    # The provider's quoted monthly price for the server type, captured at
    # provision (provenance; the cost recorder in SHIP-7 reads it).
    price_monthly: float | None = None
    # The provider-native server type + region the box was provisioned with.
    server_type: str = ""
    region: str = ""

    class Settings:
        name = "ship_boxes"
        indexes = [  # noqa: RUF012 — Beanie collection config, not shared mutable
            IndexModel([("workspace", 1), ("status", 1)], name="ix_workspace_status"),
        ]
