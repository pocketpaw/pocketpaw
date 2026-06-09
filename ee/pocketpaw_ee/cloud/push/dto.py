# Wire-format DTOs for the Web Push entity (pocketpaw#1391).
# Created: 2026-06-09 (feat/push-subscription-store) — request and response
# models are kept distinct (ee/cloud rule §4). The browser's
# ``PushSubscriptionJSON`` shape (endpoint + keys{p256dh,auth} +
# expirationTime) is mirrored on the way in; responses never carry any key
# material beyond the VAPID *public* key.
# Updated: 2026-06-09 (review nits) — ``SubscriptionResponse`` exposes
# ``created_at`` (the inherited ``createdAt``, ISO-UTC) instead of the removed
# dead model field.

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
    "SubscribeRequest",
    "SubscriptionResponse",
    "UnsubscribeRequest",
    "VapidPublicKeyResponse",
    "subscription_to_dto",
]
