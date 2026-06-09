# Web Push service (pocketpaw#1391) — sole owner of writes to the
# ``PushSubscription`` and ``VapidKeypair`` Beanie documents.
# Created: 2026-06-09 (feat/push-subscription-store).
# Updated: 2026-06-09 (review nits) — ``subscribe`` now (a) takes a
# server-captured ``user_agent`` and stores it on both branches, and (b)
# scopes the upsert precheck to the caller's workspace so a foreign-workspace
# endpoint collision can no longer silently reassign a row across tenants.
# The cross-workspace collision is caught as a ``DuplicateKeyError`` (the
# endpoint unique index) and surfaced as a ``ConflictError``, mirroring the
# keypair race handling.
#
# Responsibilities:
#   - ``get_vapid_public_key(workspace_id)`` — return the workspace's VAPID
#     public key, generating the keypair on first call (generate-once /
#     read-many). NEVER returns the private key.
#   - ``subscribe(workspace_id, user_id, body, user_agent)`` — upsert a
#     browser subscription, idempotent on ``endpoint`` within a workspace.
#   - ``unsubscribe(workspace_id, user_id, body)`` — remove a subscription
#     by endpoint (workspace-scoped).
#   - ``list_for_user(...)`` — read a user's subscriptions (used by the
#     send path in #1392).
#
# Tenancy: every read filters on ``workspace`` (ee/cloud rule §7), except the
# one deliberate global read flagged with ``# global-read:``. The VAPID
# private key is stored Fernet-encrypted (``_core.crypto``) and only ever
# decrypted by the send path — it never crosses the wire.
#
# No events are emitted here: subscribe/unsubscribe are local persistence
# with no downstream consumers until the send path lands (#1392). Each
# mutating function carries an explicit ``# no-event`` per ee/cloud rule §9.

from __future__ import annotations

from py_vapid import Vapid01, b64urlencode  # type: ignore[import-untyped]
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound
from pocketpaw_ee.cloud.models.push_subscription import PushKeys as _PushKeysDoc
from pocketpaw_ee.cloud.models.push_subscription import PushSubscription as _PushSubscriptionDoc
from pocketpaw_ee.cloud.models.vapid_keypair import VapidKeypair as _VapidKeypairDoc
from pocketpaw_ee.cloud.push.domain import PushKeys, PushSubscription
from pocketpaw_ee.cloud.push.dto import SubscribeRequest, UnsubscribeRequest

# ---------------------------------------------------------------------------
# Mapping helpers — Beanie doc ↔ domain
# ---------------------------------------------------------------------------


def _to_domain(doc: _PushSubscriptionDoc) -> PushSubscription:
    return PushSubscription(
        id=str(doc.id),
        workspace_id=doc.workspace,
        user_id=doc.user_id,
        endpoint=doc.endpoint,
        keys=PushKeys(p256dh=doc.keys.p256dh, auth=doc.keys.auth),
        expiration_time=doc.expiration_time,
        user_agent=doc.user_agent,
        # Inherited from TimestampedDocument; auto-set on insert. Naive on
        # read (Mongo strips tz) — the DTO re-anchors to UTC via iso_utc.
        created_at=getattr(doc, "createdAt", None),
    )


# ---------------------------------------------------------------------------
# VAPID keypair — per-workspace, generate-once / read-many
# ---------------------------------------------------------------------------


def _generate_vapid_keypair() -> tuple[str, str]:
    """Mint a fresh P-256 VAPID keypair.

    Returns ``(public_key_b64url, private_pem)`` where the public key is the
    uncompressed-point base64url form the browser consumes as
    ``applicationServerKey`` and the private key is a PKCS#8 PEM string.
    """
    from cryptography.hazmat.primitives import serialization

    vapid = Vapid01()
    vapid.generate_keys()
    raw_public = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = b64urlencode(raw_public)
    private_pem = vapid.private_pem()
    if isinstance(private_pem, bytes):
        private_pem = private_pem.decode()
    return public_b64, private_pem


async def _get_or_create_keypair(workspace_id: str) -> _VapidKeypairDoc:
    """Return the workspace's VAPID keypair doc, creating it on first use.

    The keypair is generated exactly once per workspace and reused on every
    subsequent call — never regenerated per request.
    """
    doc = await _VapidKeypairDoc.find_one({"workspace": workspace_id})
    if doc is not None:
        return doc

    public_b64, private_pem = _generate_vapid_keypair()
    doc = _VapidKeypairDoc(
        workspace=workspace_id,
        public_key=public_b64,
        private_pem_encrypted=crypto.encrypt(private_pem),
    )
    # ``workspace`` is unique-indexed; a concurrent first-call race would
    # surface as a DuplicateKeyError. For the read-mostly key path that
    # race is rare enough to re-read on conflict rather than lock.
    try:
        await doc.insert()
    except Exception:  # noqa: BLE001 — duplicate-key race: re-read the winner
        existing = await _VapidKeypairDoc.find_one({"workspace": workspace_id})
        if existing is None:
            raise
        return existing
    return doc


async def get_vapid_public_key(workspace_id: str) -> str:
    """Return the workspace's VAPID public key (base64url).

    Generates the keypair on first call. Only the PUBLIC key is returned —
    the private key never leaves the backend.
    """
    doc = await _get_or_create_keypair(workspace_id)
    return doc.public_key


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def subscribe(
    workspace_id: str,
    user_id: str,
    body: SubscribeRequest | dict,
    user_agent: str = "",
) -> PushSubscription:
    """Upsert a browser Web Push subscription. Idempotent on ``endpoint``.

    A second subscribe with the same endpoint *within the same workspace*
    updates the existing row (keys, user, expiration, user-agent) instead of
    inserting a duplicate. ``endpoint`` is globally unique, so an endpoint
    already owned by a DIFFERENT workspace is NOT silently reassigned: the
    precheck is workspace-scoped, so the foreign row isn't found, the insert
    hits the unique index, and the resulting ``DuplicateKeyError`` is
    surfaced as a :class:`ConflictError` rather than overwriting the other
    tenant's row. ``user_agent`` is captured server-side by the router from
    the request header.
    """
    body = SubscribeRequest.model_validate(body)
    keys_doc = _PushKeysDoc(p256dh=body.keys.p256dh, auth=body.keys.auth)

    # Workspace-scoped precheck: only an endpoint already owned by THIS
    # workspace counts as a re-subscribe. A foreign-workspace endpoint
    # falls through to the insert, where the unique index rejects it.
    existing = await _PushSubscriptionDoc.find_one(
        {"endpoint": body.endpoint, "workspace": workspace_id}
    )
    if existing is not None:
        existing.user_id = user_id
        existing.keys = keys_doc
        existing.expiration_time = body.expiration_time
        existing.user_agent = user_agent
        await existing.save()
        return _to_domain(existing)  # no-event: storage only until #1392 send path

    doc = _PushSubscriptionDoc(
        workspace=workspace_id,
        user_id=user_id,
        endpoint=body.endpoint,
        keys=keys_doc,
        expiration_time=body.expiration_time,
        user_agent=user_agent,
    )
    try:
        await doc.insert()
    except DuplicateKeyError as exc:
        # The endpoint is registered to another workspace — the unique
        # index caught it. Don't reassign another tenant's subscription;
        # report a conflict instead.
        raise ConflictError(
            "push.endpoint_taken",
            "This push endpoint is already registered elsewhere.",
        ) from exc
    return _to_domain(doc)  # no-event: storage only until #1392 send path


async def unsubscribe(
    workspace_id: str,
    user_id: str,  # noqa: ARG001 — viewer context; endpoint is the unique key
    body: UnsubscribeRequest | dict,
) -> bool:
    """Remove a subscription by endpoint, scoped to the workspace.

    Returns True when a row was deleted, False when no matching
    subscription existed.
    """
    body = UnsubscribeRequest.model_validate(body)
    doc = await _PushSubscriptionDoc.find_one(
        {"endpoint": body.endpoint, "workspace": workspace_id}
    )
    if doc is None:
        return False  # no-event: nothing removed
    await doc.delete()
    return True  # no-event: storage only until #1392 send path


async def list_for_user(workspace_id: str, user_id: str) -> list[PushSubscription]:
    """List a user's subscriptions within a workspace (for the send path)."""
    cursor = _PushSubscriptionDoc.find({"workspace": workspace_id, "user_id": user_id})
    return [_to_domain(doc) async for doc in cursor]


async def get_decrypted_private_pem(workspace_id: str) -> str:
    """Decrypt the workspace's VAPID private PEM for the send path (#1392).

    Server-side only — this is the single chokepoint that touches the
    private key, and it never returns through a router. Raises
    :class:`NotFound` if the workspace has no keypair yet.
    """
    doc = await _VapidKeypairDoc.find_one({"workspace": workspace_id})
    if doc is None:
        raise NotFound("push.vapid_keypair_missing", "No VAPID keypair for workspace")
    return crypto.decrypt(doc.private_pem_encrypted)


__all__ = [
    "get_decrypted_private_pem",
    "get_vapid_public_key",
    "list_for_user",
    "subscribe",
    "unsubscribe",
]
