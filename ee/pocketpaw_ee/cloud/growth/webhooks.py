# ee/pocketpaw_ee/cloud/growth/webhooks.py — the inbound MSG91 WhatsApp webhook
# for the /growth outbound engine (G-6).
#
# ``POST /api/v1/growth/webhooks/msg91``. Like the Recall webhook, this router
# carries NO auth dependency — the provider is the caller — so trust rests
# entirely on the signature. Unlike the Recall webhook it FAILS CLOSED: a
# missing header, a malformed header, a bad digest, AND an unconfigured secret
# all return 403. An inbound reply is the event that flips ``opted_in`` to True,
# i.e. the event that unlocks future business-initiated sends to that number, so
# an unauthenticated caller must never be able to forge one. There is no
# "accept it while you finish wiring up the secret" mode.
#
# Signature scheme: HMAC-SHA256 over the RAW request body, keyed by
# ``GROWTH_MSG91_WEBHOOK_SECRET``, hex-encoded, in ``X-Msg91-Signature``. An
# optional ``sha256=`` prefix is tolerated because several providers emit it.
# MSG91 does not publish a fixed signing scheme for WhatsApp inbound events
# (you configure the callback URL and any custom headers on the account), so
# this is the repo-standard shared-secret HMAC — the same primitive the Svix
# verification in ``meetings/providers/recall/webhooks.py`` uses, minus the
# Svix-specific id/timestamp envelope.
#
# WHAT AN INBOUND REPLY MEANS: under Meta's rules a user-initiated message both
# opens a 24-hour service window and is the opt-in signal for that number. So
# the handler sets ``prospect.opted_in = True``, moves the prospect to
# ``replied``, and walks any ``sent`` WhatsApp draft for that prospect to
# ``replied`` through the service's gate seam.
#
# The response body is a CONSTANT ``{"ok": true}`` for every accepted request —
# processed, ignored, or unknown number. A caller with a valid signature still
# must not be able to use this endpoint as a membership oracle over phone
# numbers.
#
# Created 2026-07-27 (feat/growth-g6): new module.

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from starlette.datastructures import Headers

from pocketpaw_ee.cloud._core.errors import Forbidden

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/growth/webhooks", tags=["Growth"])

_SECRET_ENV = "GROWTH_MSG91_WEBHOOK_SECRET"
_SIGNATURE_HEADERS = ("x-msg91-signature", "x-webhook-signature")

# Event/type discriminators that mean "this is a delivery receipt, not a human
# replying". Status callbacks must never be read as an opt-in.
_STATUS_TYPES = frozenset(
    {"status", "statuses", "delivery", "delivered", "read", "sent", "failed", "deleted"}
)

# Keys the sender's number has appeared under across MSG91's inbound shapes.
_NUMBER_KEYS = (
    "customer_number",
    "from",
    "sender",
    "mobile",
    "recipient_number",
    "number",
)


@router.post("/msg91")
async def msg91_webhook(request: Request) -> dict:
    """Ingest an MSG91 WhatsApp inbound event.

    A verified inbound reply opts the prospect in, marks them ``replied``, and
    walks their sent WhatsApp drafts to ``replied``. Delivery-status callbacks
    and numbers we don't hold are accepted and ignored. A bad or missing
    signature is a 403 — nothing is read from an unverified body.
    """
    raw = await request.body()
    _verify_signature(request.headers, raw)

    try:
        event = json.loads(raw or b"{}")
    except ValueError:
        logger.warning("growth/msg91 webhook: body was not valid JSON")
        return {"ok": True}
    if not isinstance(event, dict):
        return {"ok": True}

    if _is_status_callback(event):
        return {"ok": True}

    number = _extract_number(event)
    if not number:
        logger.info("growth/msg91 webhook: inbound event carried no sender number — ignored")
        return {"ok": True}

    from pocketpaw_ee.cloud.growth import service as growth_service

    matched = await growth_service.record_whatsapp_inbound_reply(number)
    # Logged (operators need it), never returned — the response shape is
    # identical for a known and an unknown number.
    logger.info("growth/msg91 webhook: inbound reply applied to %d prospect row(s)", matched)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Signature verification — fail closed
# ---------------------------------------------------------------------------


def _verify_signature(headers: Headers, body: bytes) -> None:
    """Verify the shared-secret HMAC. Raises ``Forbidden`` on ANY doubt.

    Deliberately stricter than the Recall webhook's verifier: an unset secret
    rejects rather than warning-and-accepting, because accepting an unsigned
    body here would let anyone flip ``opted_in`` on a prospect and thereby
    unlock business-initiated sends to a number that never consented.
    """
    secret = os.environ.get(_SECRET_ENV, "").strip()
    if not secret:
        logger.error(
            "%s is not set — rejecting the MSG91 webhook. This endpoint fails closed; "
            "set the secret to enable inbound replies.",
            _SECRET_ENV,
        )
        raise Forbidden(
            "growth.webhook_unverifiable",
            "The MSG91 webhook secret is not configured on this deployment.",
        )

    provided = ""
    for name in _SIGNATURE_HEADERS:
        value = headers.get(name)
        if value:
            provided = value.strip()
            break
    if not provided:
        raise Forbidden(
            "growth.webhook_unsigned", "The MSG91 webhook is missing its signature header."
        )
    # Tolerate the common ``sha256=<hex>`` prefix form.
    if "=" in provided:
        prefix, _, rest = provided.partition("=")
        if prefix.strip().lower() in ("sha256", "v1"):
            provided = rest.strip()

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided.lower(), expected):
        raise Forbidden(
            "growth.webhook_signature_invalid", "The MSG91 webhook signature did not verify."
        )


# ---------------------------------------------------------------------------
# Payload shape helpers
# ---------------------------------------------------------------------------


def _is_status_callback(event: dict[str, Any]) -> bool:
    """True when the payload is a delivery receipt rather than a human reply."""
    for key in ("type", "event", "event_type"):
        value = event.get(key)
        if isinstance(value, str) and value.strip().lower() in _STATUS_TYPES:
            return True
    # Meta's envelope nests receipts under ``statuses`` with no inbound message.
    for container in (event, event.get("data"), event.get("payload")):
        if (
            isinstance(container, dict)
            and container.get("statuses")
            and not container.get("messages")
        ):
            return True
    return False


def _extract_number(event: dict[str, Any]) -> str:
    """Pull the sender's number out of an inbound payload, or "".

    MSG91 has used several envelopes (flat, ``data``-wrapped, and a Meta-style
    ``messages[]`` passthrough), so this checks the shapes we've seen rather
    than assuming one. Returns the raw string; normalisation into the spellings
    a prospect row might carry happens in the service.
    """
    containers: list[dict[str, Any]] = [event]
    for key in ("data", "payload", "content", "message"):
        nested = event.get(key)
        if isinstance(nested, dict):
            containers.append(nested)

    for container in containers:
        for key in _NUMBER_KEYS:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            # Meta nests ``from`` inside a contact/profile object in some shapes.
            if isinstance(value, dict):
                inner = value.get("number") or value.get("wa_id") or value.get("id")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
        messages = container.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict):
                sender = first.get("from") or first.get("customer_number")
                if isinstance(sender, str) and sender.strip():
                    return sender.strip()
    return ""


__all__ = ["router"]
