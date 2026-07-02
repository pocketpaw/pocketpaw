# Wire-format DTOs for the Web Push entity (pocketpaw#1391).
# Created: 2026-06-09 (feat/push-subscription-store) — request and response
# models are kept distinct (ee/cloud rule §4). The browser's
# ``PushSubscriptionJSON`` shape (endpoint + keys{p256dh,auth} +
# expirationTime) is mirrored on the way in; responses never carry any key
# material beyond the VAPID *public* key.
# Updated: 2026-06-09 (review nits) — ``SubscriptionResponse`` exposes
# ``created_at`` (the inherited ``createdAt``, ISO-UTC) instead of the removed
# dead model field.
# Updated: 2026-06-09 (feat/push-send-prune, pocketpaw#1392) — added the
# ``PushPayload`` notification body (title/body + optional url/icon/tag) the
# send path serializes to the browser, and ``SendResult`` (sent + pruned
# counts) the fan-out returns to its caller. Both are pure wire models with
# no key material.

from __future__ import annotations

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.push.domain import PushSubscription


class PushKeysIn(BaseModel):
    """The ``keys`` object from a browser PushSubscription."""

    p256dh: str
    auth: str


class SubscribeRequest(BaseModel):
    """Body of ``POST /push/subscribe`` — mirrors PushSubscriptionJSON.

    The browser produces ``expirationTime`` (camelCase, epoch millis or
    null); we accept both spellings so callers can post the raw
    ``subscription.toJSON()`` without remapping.
    """

    endpoint: str
    keys: PushKeysIn
    expiration_time: int | None = Field(default=None, alias="expirationTime")

    model_config = {"populate_by_name": True}


class UnsubscribeRequest(BaseModel):
    """Body of ``POST /push/unsubscribe`` — identifies the row by endpoint."""

    endpoint: str


class SubscriptionResponse(BaseModel):
    """Wire response for a stored subscription. No private key material."""

    id: str
    endpoint: str
    expiration_time: int | None = None
    created_at: str | None = None


class VapidPublicKeyResponse(BaseModel):
    """Wire response for the VAPID public key endpoint.

    Exactly ``{"key": "<base64url public key>"}`` — the private key is
    never a field on any response model in this module.
    """

    key: str


class PushPayload(BaseModel):
    """The notification body the send path serializes and ships to a browser.

    Maps onto the fields the service worker's ``push`` handler reads when it
    calls ``registration.showNotification(title, options)``. ``title`` and
    ``body`` are the only required fields; ``url`` (deep-link opened on click),
    ``icon``, and ``tag`` (collapse key — a later notification with the same
    tag replaces an earlier one) are optional. The whole model is JSON-encoded
    as the encrypted Web Push payload, so it carries no server secrets.
    """

    title: str
    body: str
    url: str | None = None
    icon: str | None = None
    tag: str | None = None


class SendResult(BaseModel):
    """Outcome of a fan-out send to one user's subscriptions.

    ``sent`` counts endpoints the push service accepted; ``pruned`` counts
    dead endpoints (404/410) deleted during the fan-out; ``failed`` counts
    endpoints that errored for some other reason (logged, left in place).
    """

    sent: int = 0
    pruned: int = 0
    failed: int = 0


def subscription_to_dto(sub: PushSubscription) -> SubscriptionResponse:
    """Map a domain ``PushSubscription`` to its wire DTO."""
    return SubscriptionResponse(
        id=sub.id,
        endpoint=sub.endpoint,
        expiration_time=sub.expiration_time,
        created_at=iso_utc(sub.created_at),
    )


__all__ = [
    "PushKeysIn",
    "PushPayload",
    "SendResult",
    "SubscribeRequest",
    "SubscriptionResponse",
    "UnsubscribeRequest",
    "VapidPublicKeyResponse",
    "subscription_to_dto",
]
