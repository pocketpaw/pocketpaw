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
# Updated: 2026-06-09 (feat/push-send-prune, pocketpaw#1392) — added the
# Web Push SEND path: ``send_to_user`` fans a ``PushPayload`` out to every
# stored subscription for a user, signing+encrypting each with the tenant's
# VAPID private key (obtained via the existing ``get_decrypted_private_pem``
# chokepoint — the private key never leaves the backend) and POSTing to the
# browser vendor endpoint via ``pywebpush``. A 404/410 response means the
# subscription is dead, so the row is deleted here (this service stays the
# sole Beanie writer). Any other error is logged and skipped so one bad
# endpoint can't abort the rest of the fan-out. Pruning a dead subscription
# is a local cleanup with no downstream consumers, so it carries a
# ``# no-event``; wiring sends to real product events + WS-vs-push dedupe is
# the follow-up (#1393), NOT here.
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
#     send path).
#   - ``send_to_user(workspace_id, user_id, payload)`` — fan a notification
#     out to the user's live subscriptions, pruning dead (404/410) ones.
#
# Tenancy: every read filters on ``workspace`` (ee/cloud rule §7), except the
# one deliberate global read flagged with ``# global-read:``. The VAPID
# private key is stored Fernet-encrypted (``_core.crypto``) and only ever
# decrypted by the send path — it never crosses the wire.
#
# No events are emitted here: subscribe/unsubscribe/prune are local
# persistence with no downstream consumers until event wiring lands (#1393).
# Each mutating function carries an explicit ``# no-event`` per ee/cloud rule §9.

from __future__ import annotations

import asyncio
import logging
import os

from py_vapid import Vapid01, b64urlencode  # type: ignore[import-untyped]
from pymongo.errors import DuplicateKeyError
from pywebpush import WebPushException, webpush  # type: ignore[import-untyped]

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound
from pocketpaw_ee.cloud.models.push_subscription import PushKeys as _PushKeysDoc
from pocketpaw_ee.cloud.models.push_subscription import PushSubscription as _PushSubscriptionDoc
from pocketpaw_ee.cloud.models.vapid_keypair import VapidKeypair as _VapidKeypairDoc
from pocketpaw_ee.cloud.push.domain import PushKeys, PushSubscription
from pocketpaw_ee.cloud.push.dto import (
    PushPayload,
    SendResult,
    SubscribeRequest,
    UnsubscribeRequest,
)

logger = logging.getLogger(__name__)

# The VAPID ``sub`` claim — a contact (mailto: or https: URL) the push service
# can reach if our sends misbehave. Operator-configurable via env; the default
# is a non-personal project address, never a real individual's inbox.
_PUSH_CONTACT_ENV = "CLOUD_PUSH_CONTACT"
_DEFAULT_PUSH_CONTACT = "mailto:push@pocketpaw.app"


def _vapid_contact() -> str:
    """Return the VAPID ``sub`` claim (contact mailto:/URL).

    Operator override via ``CLOUD_PUSH_CONTACT``; falls back to a non-personal
    project default so no individual's address is ever hardcoded.
    """
    return os.environ.get(_PUSH_CONTACT_ENV, "").strip() or _DEFAULT_PUSH_CONTACT

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


# ---------------------------------------------------------------------------
# Send — fan a notification out to a user's subscriptions, prune the dead
# ---------------------------------------------------------------------------


async def _delete_by_endpoint(workspace_id: str, endpoint: str) -> None:
    """Delete a dead subscription row by endpoint, workspace-scoped.

    Called from the send path when the push service reports the endpoint
    gone (404/410). Kept here so this service stays the sole Beanie writer
    for the push collection (ee/cloud rule §2 + the import-linter contract).
    """
    doc = await _PushSubscriptionDoc.find_one(
        {"endpoint": endpoint, "workspace": workspace_id}
    )
    if doc is not None:
        await doc.delete()  # no-event: dead-endpoint cleanup, no consumers until #1393


def _status_of(exc: WebPushException) -> int | None:
    """Best-effort HTTP status from a WebPushException's vendor response."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


async def send_to_user(
    workspace_id: str,
    user_id: str,
    payload: PushPayload | dict,
) -> SendResult:
    """Send a Web Push notification to every live subscription for a user.

    Fans ``payload`` out to each of the user's stored subscriptions, signing
    and encrypting per-endpoint with the tenant's VAPID private key (fetched
    through the ``get_decrypted_private_pem`` chokepoint — the key is read
    once per fan-out and never leaves this process). ``pywebpush.webpush`` is
    synchronous (it POSTs via ``requests``), so each call runs in a worker
    thread to keep the service async and the endpoints concurrent-friendly.

    Dead-endpoint pruning: a ``404`` or ``410`` from the push service means
    the browser dropped the subscription, so that row is deleted. Any other
    error is logged and skipped — one unreachable endpoint must not abort the
    rest of the fan-out. Returns a :class:`SendResult` summarizing the run.
    """
    payload = PushPayload.model_validate(payload)
    subs = await list_for_user(workspace_id, user_id)
    result = SendResult()
    if not subs:
        return result

    # Single chokepoint read of the tenant private key for the whole fan-out.
    private_pem = await get_decrypted_private_pem(workspace_id)
    vapid_claims = {"sub": _vapid_contact()}
    data = payload.model_dump_json(exclude_none=True)

    for sub in subs:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.keys.p256dh, "auth": sub.keys.auth},
        }
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=subscription_info,
                data=data,
                vapid_private_key=private_pem,
                # ``vapid_claims`` is mutated in place by py-vapid (it stamps
                # ``exp``/``aud``), so hand each call its own copy.
                vapid_claims=dict(vapid_claims),
            )
            result.sent += 1
        except WebPushException as exc:
            status = _status_of(exc)
            if status in (404, 410):
                await _delete_by_endpoint(workspace_id, sub.endpoint)
                result.pruned += 1
            else:
                result.failed += 1
                logger.warning(
                    "web push send failed (status=%s) for workspace=%s user=%s: %s",
                    status,
                    workspace_id,
                    user_id,
                    exc,
                )
        except Exception:  # noqa: BLE001 — never let one endpoint abort the fan-out
            result.failed += 1
            logger.exception(
                "unexpected error sending web push for workspace=%s user=%s",
                workspace_id,
                user_id,
            )

    return result


__all__ = [
    "get_decrypted_private_pem",
    "get_vapid_public_key",
    "list_for_user",
    "send_to_user",
    "subscribe",
    "unsubscribe",
]
