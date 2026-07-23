# ee/pocketpaw_ee/cloud/models/ship.py — the /ship managed-deploy documents.
#
# Three Beanie documents back a workspace's managed deploys:
#
#   * ``ShipBox`` — one row per provisioned box. Records the provider, the
#     provider-side server id, the reachable IP, the lifecycle status, the
#     monthly price captured at provision time, and an ENCRYPTED SSH private
#     key (Fernet, via ``cloud._core.crypto``) the engine driver uses to reach
#     the box over SSH. The key is the box's blast radius, so it never lives in
#     plaintext at rest and never leaves this layer in a DTO — the ship service
#     decrypts it only to hand it to the SHIP-1 driver.
#   * ``ShipApp`` — one row per app deployed onto a box (SHIP-3). Carries the
#     build inputs (``build_path`` / ``git_ref`` / ``image``), the routed
#     domains + URLs, the linked database service, and the app lifecycle
#     status. ``env_refs`` holds env var NAMES only — values are the app's own
#     secret material and are never persisted here (the SHIP-1 ``AppSpec``
#     security invariant, carried through to storage).
#   * ``ShipDeploy`` — one row per deploy attempt (SHIP-3). The pollable state
#     the HTTP surface returns immediately while the arq deploy job advances it
#     ``queued -> building -> releasing -> live`` (or ``failed``).
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
# ``ee.cloud.ship.store`` reads/writes this document — the same
# one-module-owns-one-doc isolation the credit / litellm-key / foresight docs
# use.
#
# Changed 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): added ``ShipApp`` +
# ``ShipDeploy`` (registered alongside ``ShipBox`` in ``cloud.models.__init__``)
# so the workspace-scoped ``/api/v1/ship`` surface has app + deploy state to
# read. Both are workspace-indexed and read exclusively through
# ``ee.cloud.ship.store``.
# Changed 2026-07-23 (feat/ship-9-env-store, SHIP-9): added the encrypted env
# store to ``ShipApp`` — ``env_vars`` maps an env var NAME to a ``ShipEnvVar``
# sub-doc holding the value Fernet-encrypted at rest (same envelope + key,
# ``CLOUD_ENCRYPTION_KEY``, as the box SSH key) plus a non-secret display mask
# and a ``both|prod|preview`` scope. Plaintext env VALUES never live at rest;
# ``env_refs`` (names only) is unchanged and still carried alongside it. Only
# ``ee.cloud.ship.store`` encrypts/decrypts the ``enc_value`` (mirrors
# ``decrypt_ssh_key``); no other layer ever handles the plaintext.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from beanie import Indexed
from pydantic import BaseModel, Field
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
    # Set when a DELETE parked a teardown for human approval (SHIP-3). The box
    # keeps its current ``status`` — the frozen ``BoxOut`` status vocabulary has
    # no ``pending`` member, and a parked teardown has not changed the box's
    # actual lifecycle state. SHIP-4 replaces the placeholder id with a real
    # Instinct proposal id.
    pending_destroy_proposal_id: str | None = None

    class Settings:
        name = "ship_boxes"
        indexes = [  # noqa: RUF012 — Beanie collection config, not shared mutable
            IndexModel([("workspace", 1), ("status", 1)], name="ix_workspace_status"),
        ]


# The app lifecycle. ``created`` is the birth state; a deploy job drives
# ``deploying`` -> ``live`` / ``failed``.
ShipAppStatus = Literal["created", "deploying", "live", "failed"]

# How the app's image is produced. v1 deploys a pre-built image reference; the
# build strategy is recorded now so the SHIP-5 build path has somewhere to read
# it from without a migration.
ShipBuildPath = Literal["dockerfile", "nixpacks"]


class ShipAppDomain(BaseModel):
    """One domain routed to an app, with the TLS outcome the engine reported.

    Stored as a sub-document (not a bare string) because the /ship console shows
    the certificate state per domain — persisting only the name would force the
    read path to invent one.
    """

    domain: str
    tls_enabled: bool = False


# An env var's deploy scope: applied to both deploy kinds, or only to production
# / only to preview deploys. The deploy-time merge filters on it (SHIP-9).
ShipEnvScope = Literal["both", "prod", "preview"]


class ShipEnvVar(BaseModel):
    """One env var stored for an app (SHIP-9).

    ``enc_value`` is a Fernet token — the plaintext value NEVER lives at rest,
    the same envelope the box SSH key uses (``cloud._core.crypto`` +
    ``CLOUD_ENCRYPTION_KEY``). ``masked`` is a NON-secret display hint (a short
    suffix, or bullets for short values) the read surface returns so the console
    can render the var without ever decrypting it — decryption happens only at
    deploy, inside ``ship.store``. ``scope`` decides which deploy kinds the var
    is merged into.

    Only ``ee.cloud.ship.store`` reads/writes ``enc_value`` (encrypt on upsert,
    decrypt at deploy); no router, DTO, domain, or service ever touches it.
    """

    enc_value: str
    masked: str = ""
    scope: ShipEnvScope = "both"


class ShipApp(TimestampedDocument):
    """One app deployed onto a workspace's managed box.

    Tenancy: ``workspace`` is REQUIRED and every read filters on it (the ship
    store asserts it). ``box_id`` is the owning ``ShipBox`` id — an app never
    outlives its box.

    SECURITY: ``env_refs`` carries env var NAMES only. ``env_vars`` (SHIP-9) may
    carry VALUES, but only as Fernet ciphertext (``ShipEnvVar.enc_value``) — the
    plaintext never lives at rest, is never serialized into a DTO/event/log, and
    is decrypted solely inside ``ship.store`` at deploy time (the same envelope
    the box SSH key uses). SHIP-1's ``AppSpec``/``DbResult`` hold the matching
    invariant at the engine boundary.
    """

    # Tenancy filter — every ship read scopes on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The owning box's id (a ``ShipBox`` document id as a string).
    box_id: str
    # The engine-side app name (also the Dokku app name).
    name: str
    # How the image is built. v1 records it; the build itself is image-based.
    build_path: ShipBuildPath = "dockerfile"
    # The git ref the image was (or will be) built from — provenance only.
    git_ref: str = ""
    # The container image reference (tag included) the engine deploys.
    image: str = ""
    # Env var NAMES the app expects. NEVER values (see the class docstring).
    env_refs: list[str] = Field(default_factory=list)
    # Encrypted env store (SHIP-9): NAME -> ``ShipEnvVar`` (value Fernet-encrypted
    # at rest, plus a non-secret display mask + scope). The one place an app's
    # env VALUES are persisted — encrypted, and only ever written/read through
    # ``ship.store``. Distinct from ``env_refs`` (names an app merely expects).
    env_vars: dict[str, ShipEnvVar] = Field(default_factory=dict)
    # Production flag — the console badges it; no behaviour hangs off it yet.
    prod: bool = False
    # Engine-reported URLs the app answers on (deploy URL + added domains).
    urls: list[str] = Field(default_factory=list)
    # Domains routed to the app via ``add_domain`` (name + TLS outcome).
    domains: list[ShipAppDomain] = Field(default_factory=list)
    # The linked database service name + the env var name the link injected.
    # The connection string itself is a secret and is NEVER stored.
    db_service: str = ""
    db_env_var: str = ""
    # Lifecycle state (see ``ShipAppStatus``).
    status: ShipAppStatus = "created"
    # Set when a DELETE parked a teardown for human approval (SHIP-3); SHIP-4
    # swaps the placeholder for a real Instinct proposal id.
    pending_destroy_proposal_id: str | None = None

    class Settings:
        name = "ship_apps"
        indexes = [  # noqa: RUF012 — Beanie collection config, not shared mutable
            IndexModel([("workspace", 1), ("box_id", 1)], name="ix_workspace_box"),
            IndexModel(
                [("workspace", 1), ("box_id", 1), ("name", 1)],
                name="ux_workspace_box_name",
                unique=True,
            ),
        ]


# The deploy lifecycle the arq deploy job walks. ``queued`` is written by the
# web process at enqueue; the job advances it and ends on ``live`` or ``failed``.
ShipDeployStatus = Literal["queued", "building", "releasing", "live", "failed"]


class ShipDeploy(TimestampedDocument):
    """One deploy attempt for a ``ShipApp``.

    Tenancy: ``workspace`` is REQUIRED and every read filters on it. This is the
    pollable record the HTTP surface hands back immediately — the long work runs
    in the arq deploy job, which advances ``status`` and stamps ``finished_at``.

    ``log_summary`` is a SHORT, already-redacted failure tail (SHIP-1's
    ``CommandFailed`` redacts its command + stderr before construction), never a
    raw engine transcript.
    """

    # Tenancy filter — every ship read scopes on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The deployed app's id (a ``ShipApp`` document id as a string).
    app_id: str
    # Lifecycle state (see ``ShipDeployStatus``).
    status: ShipDeployStatus = "queued"
    # When the attempt was accepted (stamped at enqueue, so it is always set).
    started_at: datetime | None = None
    # When the attempt reached a terminal state (``live`` / ``failed``).
    finished_at: datetime | None = None
    # The image reference this attempt deployed — pinned at enqueue so a later
    # app edit never rewrites history.
    image: str = ""
    # A short, redacted outcome/failure summary. The log POINTER for the full
    # transcript is the app's engine-side log stream (``GET .../logs``).
    log_summary: str = ""

    class Settings:
        name = "ship_deploys"
        indexes = [  # noqa: RUF012 — Beanie collection config, not shared mutable
            IndexModel(
                [("workspace", 1), ("app_id", 1), ("createdAt", -1)],
                name="ix_workspace_app_created",
            ),
        ]
