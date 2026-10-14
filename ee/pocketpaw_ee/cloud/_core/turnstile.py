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
#
# A caller with its own widget (a Paw Site's per-site widget, see
# ``integrations.providers.turnstile``) passes ``secret`` and the hostnames the
# challenge may have been solved on; the answer's ``hostname`` must be one of them.

from __future__ import annotations

import logging
from collections.abc import Collection

import httpx

from pocketpaw_ee.cloud._core.errors import BadRequest
from pocketpaw_ee.cloud.auth.core import _is_production

logger = logging.getLogger(__name__)

TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _bare_host(value: str) -> str:
    """Lowercased host without a port or trailing dot (``WWW.a.com:443`` -> ``www.a.com``)."""
    host = value.strip().lower()
    if not host.startswith("["):
        host = host.split(":", 1)[0]
    return host.rstrip(".")


async def verify_turnstile(
    token: str,
    remote_ip: str | None,
    *,
    code: str,
    transport: httpx.AsyncBaseTransport | None = None,
    secret: str | None = None,
    expected_hostnames: Collection[str] | None = None,
) -> None:
    """Raise 400 ``code`` unless Cloudflare accepts ``token``.

    ``secret`` overrides the platform secret (never falls back to it);
    ``expected_hostnames`` also requires the answer's ``hostname`` to be listed."""
    from pocketpaw.config import get_settings

    if secret is not None:
        secret = secret.strip()
        if not secret:
            raise BadRequest(code, "This form is not available right now. Try again later.")
    else:
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
            body = resp.json() if resp.status_code == 200 else {}
            ok = body.get("success") is True
            if ok and expected_hostnames is not None:
                allowed = {_bare_host(h) for h in expected_hostnames if h}
                ok = _bare_host(str(body.get("hostname") or "")) in allowed - {""}
    except Exception as exc:  # network / bad JSON: fail closed, never log the secret
        logger.warning("turnstile: verify failed: %s", type(exc).__name__)
        ok = False
    if not ok:
        raise BadRequest(
            code, "We couldn't confirm you're a person. Refresh the page and try again."
        )


__all__ = ["TURNSTILE_URL", "verify_turnstile"]
