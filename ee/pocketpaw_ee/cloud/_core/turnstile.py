# ee/pocketpaw_ee/cloud/_core/turnstile.py — Cloudflare Turnstile check for public routes.
#
# One verifier for every unauthenticated POST that would otherwise cost the
# platform something (an AI check spends engine money, a partner application
# lands in the operator queue). The token is sent to Cloudflare with the caller's
# IP and never stored. With ``POCKETPAW_TURNSTILE_SECRET`` unset the check is
# skipped with a warning in dev, but REFUSED in a production posture
# (``auth.core._is_production``: POCKETPAW_ENV=production or
# POCKETPAW_AUTH_COOKIE_SECURE=true), so a deploy that forgot the secret does not
# accept bot submissions. A network error or a bad answer fails closed. The
# caller names the error code so each route keeps its own ``<module>.turnstile_failed``.

from __future__ import annotations

import logging

import httpx

from pocketpaw_ee.cloud._core.errors import BadRequest
from pocketpaw_ee.cloud.auth.core import _is_production

logger = logging.getLogger(__name__)

TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def verify_turnstile(
    token: str,
    remote_ip: str | None,
    *,
    code: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Raise 400 ``code`` unless Cloudflare accepts ``token``."""
    from pocketpaw.config import get_settings

    secret = str(get_settings().turnstile_secret or "").strip()
    if not secret:
        if _is_production():
            logger.error("turnstile: POCKETPAW_TURNSTILE_SECRET unset in production, refusing")
            raise BadRequest(code, "This form is not available right now. Try again later.")
        logger.warning("turnstile: POCKETPAW_TURNSTILE_SECRET unset, skipping (dev only)")
        return
    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
            resp = await client.post(TURNSTILE_URL, data=data)
            ok = resp.status_code == 200 and resp.json().get("success") is True
    except Exception as exc:  # network / bad JSON: fail closed, never log the secret
        logger.warning("turnstile: verify failed: %s", type(exc).__name__)
        ok = False
    if not ok:
        raise BadRequest(
            code, "We couldn't confirm you're a person. Refresh the page and try again."
        )


__all__ = ["TURNSTILE_URL", "verify_turnstile"]
