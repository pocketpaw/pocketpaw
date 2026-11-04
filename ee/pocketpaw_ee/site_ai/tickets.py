# ee/pocketpaw_ee/site_ai/tickets.py — short-lived visitor tickets for browser AI calls.
#
# ``POST /sites/{site_id}/ai/ticket`` (site key + origin gate) turns a Turnstile
# answer into a ticket the browser sends as ``X-Paw-Ai-Ticket`` on ``/ai/chat`` and
# ``/ai/embed``. A ticket is ``v1.<kind>.<exp>.<nonce>.<sig>`` where ``sig`` is
# HMAC-SHA256 over (kind, site id, the caller's IP hash, exp, nonce) with a server
# secret. It is bound to the site, the address it was minted from, the caller kind
# (``browser`` or ``preview``, so a preview ticket can't skip Turnstile on the live
# site) and an expiry ~20 minutes out. The IP hash is never in the ticket: it is
# recomputed from the request, so a ticket used from another address fails.
#
# Turnstile (browser tickets), fail closed: the site's own widget when it has one,
# else the platform secret; either way the answer's hostname must be one of the
# site's own hosts. With neither configured the ticket is refused, unless the
# dev-only ``PAW_SITE_AI_ALLOW_NO_TURNSTILE=1`` is set on a non-production box
# (the flag is ignored, and logged, in production).
# Preview tickets (the site's own unguessable draft origin) skip Turnstile.
#
# Secret: ``POCKETPAW_SITE_AI_TICKET_SECRET``, else derived from ``AUTH_SECRET``.
# Neither set refuses tickets (503), never signs with a default.

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.site_ai.errors import AiError

logger = logging.getLogger(__name__)

TICKET_TTL_S = 20 * 60
_SKEW_S = 60
_KINDS = ("browser", "preview")


def _secret() -> bytes:
    own = os.environ.get("POCKETPAW_SITE_AI_TICKET_SECRET", "").strip()
    if own:
        return own.encode("utf-8")
    auth = os.environ.get("AUTH_SECRET", "").strip()
    if auth:
        return hmac.new(auth.encode("utf-8"), b"site-ai-ticket-v1", hashlib.sha256).digest()
    raise AiError("provider_unavailable", "Browser AI isn't configured on this server.")


def _sign(kind: str, site_id: str, ip_hash: str, exp: int, nonce: str) -> str:
    msg = f"v1|{kind}|{site_id}|{ip_hash}|{exp}|{nonce}".encode()
    digest = hmac.new(_secret(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def mint(kind: str, site_id: str, ip_hash: str, now: float | None = None) -> tuple[str, str]:
    """(ticket, expires_at ISO) for ``kind`` on ``site_id`` from ``ip_hash``."""
    if kind not in _KINDS:
        raise ValueError(kind)
    exp = int(now if now is not None else time.time()) + TICKET_TTL_S
    nonce = secrets.token_urlsafe(12)
    ticket = f"v1.{kind}.{exp}.{nonce}.{_sign(kind, site_id, ip_hash, exp, nonce)}"
    return ticket, datetime.fromtimestamp(exp, UTC).isoformat()


def verify(ticket: Any, kind: str, site_id: str, ip_hash: str, now: float | None = None) -> None:
    """Raise 401 ``unauthorized`` unless ``ticket`` is a live ticket of ``kind`` for
    ``site_id`` minted from this address."""
    if not isinstance(ticket, str) or not 0 < len(ticket) <= 256:
        raise AiError("unauthorized", "This page needs a fresh AI ticket.")
    parts = ticket.split(".")
    if len(parts) != 5 or parts[0] != "v1" or parts[1] != kind or not parts[2].isdigit():
        raise AiError("unauthorized", "This page needs a fresh AI ticket.")
    exp = int(parts[2])
    clock = int(now if now is not None else time.time())
    if exp < clock or exp > clock + TICKET_TTL_S + _SKEW_S:
        raise AiError("unauthorized", "This page needs a fresh AI ticket.")
    expected = _sign(kind, site_id, ip_hash, exp, parts[3])
    if not hmac.compare_digest(expected.encode(), parts[4].encode()):
        raise AiError("unauthorized", "This page needs a fresh AI ticket.")


async def check_turnstile(site: Any, token: Any, ip: str) -> None:
    """Fail-closed Turnstile for a browser ticket; see the header."""
    from pocketpaw_ee.cloud.auth.core import _is_production
    from pocketpaw_ee.integrations import site_gate

    token = token if isinstance(token, str) else ""
    try:
        widget = await site_gate.site_widget(site)
        if widget is not None:
            await site_gate.verify_turnstile(token, ip, widget)
            return
        if site_gate.turnstile_configured():
            from pocketpaw_ee.cloud.leads.intake import derived_origins

            secret = str(_platform_secret() or "")
            await site_gate.verify_turnstile(token, ip, (secret, derived_origins(site)))
            return
    except site_gate.SiteGateError:
        raise AiError("turnstile_required") from None
    if os.environ.get("PAW_SITE_AI_ALLOW_NO_TURNSTILE", "").strip() == "1":
        if not _is_production():
            return
        logger.error("site_ai: PAW_SITE_AI_ALLOW_NO_TURNSTILE is set in production; ignored")
    raise AiError("turnstile_required", "Browser AI needs Turnstile set up for this site.")


def _platform_secret() -> str | None:
    from pocketpaw.config import get_settings

    return get_settings().turnstile_secret


__all__ = ["TICKET_TTL_S", "check_turnstile", "mint", "verify"]
