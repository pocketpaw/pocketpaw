# ee/pocketpaw_ee/cloud/billing/webhooks.py — the PUBLIC inbound gateway webhook
# (BC-2, the Gateway primitive).
#
# ``POST /billing/webhooks/dodo`` carries NO auth dependency — Dodo is the
# caller. Trust is established by the Standard-Webhooks signature, verified
# inside ``billing.service.handle_webhook`` (which delegates to the provider)
# against the RAW request bytes BEFORE any field is trusted. This router is
# mounted SEPARATELY from the licensed top-up router in ee/cloud/__init__.py so
# no auth/license dependency ever attaches to it.
#
# On a verified ``payment.succeeded`` the service grants credits EXACTLY ONCE
# (BC-1's idempotent grant, keyed on the webhook event id). A bad signature
# raises ``ValidationError`` → 400 via the cloud error handler — no grant.
#
# SECURITY: read RAW bytes (the signature is over the exact bytes — never
# re-serialize). Never log the secret or customer PII.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new module.

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from pocketpaw_ee.cloud.billing import service as billing_service
from pocketpaw_ee.cloud.billing.dto import WebhookAck

logger = logging.getLogger(__name__)

# NO license / auth dependency — the Standard-Webhooks signature is the trust
# boundary. Mounted on its own in mount_cloud().
router = APIRouter(prefix="/billing/webhooks", tags=["Billing"])


@router.post("/dodo", response_model=WebhookAck)
async def dodo_webhook(request: Request) -> WebhookAck:
    """Ingest a Dodo Payments webhook.

    Reads the RAW body + headers, verifies the Standard-Webhooks signature, and
    on a ``payment.succeeded`` event grants the purchased credits to the
    workspace named in the payment metadata — EXACTLY ONCE (a replay is a no-op).
    Returns 200 with ``{ok, granted}``; a signature that does not verify raises
    ``ValidationError`` → 400 (no grant).
    """
    raw = await request.body()
    # Starlette headers are case-insensitive; hand the service a plain dict (the
    # Standard-Webhooks verifier lowercases keys itself).
    headers = dict(request.headers)
    result = await billing_service.handle_webhook(payload=raw, headers=headers)
    return WebhookAck(ok=bool(result.get("ok", True)), granted=bool(result.get("granted", False)))
