# ee/pocketpaw_ee/integrations/oauth.py: hosted connect + callback for connect kinds.
#
#   start_connect   POST /integrations/{provider}/connect -> {authorize_url}
#   finish_callback GET  /integrations/{provider}/callback?code&state -> HTML page
#
# ``state`` is the single-use server-side store in ``cloud.auth._oauth_state``
# (namespace ``integrations``), carrying workspace, user, provider and an optional
# pocket_id. The callback trusts only that payload, never its own query string
# beyond ``code`` and ``state``. Not built on ``pocketpaw.clients.oauth`` /
# ``TokenStore`` (local, plaintext, weak state).
#
# The redirect URI base is ``POCKETPAW_INTEGRATIONS_CALLBACK_BASE`` (one platform
# callback domain), falling back to the API's public base URL.
#
# The callback answers with a tiny page that posts
# {type: "paw:integration-connected", provider, connection_id, status, error?} to
# ``window.opener`` and closes. Target origins are the builder origin
# (``PAW_SITES_BUILDER_ORIGIN``) and the desktop shell's fixed origins; never "*".
# The message carries no token.

from __future__ import annotations

import html
import json
import logging
import os
import secrets
from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, ValidationError
from pocketpaw_ee.cloud._core.public_url import public_base_url
from pocketpaw_ee.integrations import bindings, vault

logger = logging.getLogger(__name__)

STATE_NAMESPACE = "integrations"
CALLBACK_BASE_ENV = "POCKETPAW_INTEGRATIONS_CALLBACK_BASE"
MESSAGE_TYPE = "paw:integration-connected"


def callback_base() -> str:
    return os.environ.get(CALLBACK_BASE_ENV, "").strip().rstrip("/") or public_base_url()


def redirect_uri(provider_id: str) -> str:
    return f"{callback_base()}/api/v1/integrations/{provider_id}/callback"


def message_origins() -> list[str]:
    """Where the callback page may post its result. Each is an origin we serve."""
    from pocketpaw.api.cors import BUILTIN_ORIGINS
    from pocketpaw_ee.sites.service import _builder_origin

    origins = [_builder_origin().rstrip("/"), *BUILTIN_ORIGINS]
    return [o for o in dict.fromkeys(origins) if o and o != "*"]


async def start_connect(
    *, workspace_id: str, user_id: str, provider_id: str, pocket_id: str | None
) -> str:
    """Issue single-use state and return the provider's authorize URL."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    provider = bindings.provider_or_404(provider_id)
    if provider.kind != "connect":
        raise ValidationError(
            "integrations.not_connectable", f"{provider.name} does not need connecting."
        )
    if provider.availability() != "available":
        raise ConflictError(
            "integrations.provider_unavailable", f"{provider.name} isn't available yet."
        )
    if pocket_id:
        await bindings.require_edit(workspace_id, user_id, pocket_id)
    state = await _oauth_state.issue(
        STATE_NAMESPACE,
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "provider": provider.id,
            "pocket_id": pocket_id or "",
        },
    )
    return provider.authorize_url(state, redirect_uri(provider.id))


async def finish_callback(provider_id: str, code: str, state: str) -> dict[str, Any]:
    """Redeem state, finish the grant, store it, link the site. Returns the message."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    message: dict[str, Any] = {
        "type": MESSAGE_TYPE,
        "provider": provider_id,
        "connection_id": None,
        "status": "error",
    }
    try:
        payload = await _oauth_state.consume(STATE_NAMESPACE, state or "")
    except CloudError:
        message["error"] = "invalid_state"
        return message
    if payload.get("provider") != provider_id:
        message["error"] = "invalid_state"
        return message
    if not code:
        message["error"] = "access_denied"
        return message
    try:
        provider = bindings.provider_or_404(provider_id)
        grant = await provider.complete(code, redirect_uri(provider_id))
        conn = await vault.store_grant(
            workspace_id=payload["workspace_id"],
            provider=provider_id,
            grant=grant,
            user_id=payload["user_id"],
        )
    except Exception as exc:  # noqa: BLE001 - every failure becomes one safe message
        logger.warning("integrations: %s callback failed: %s", provider_id, type(exc).__name__)
        message["error"] = "provider_error"
        return message

    message["connection_id"] = str(conn.id)
    message["status"] = "active"
    if payload.get("pocket_id"):
        try:
            await bindings.link_connection(
                workspace_id=payload["workspace_id"],
                user_id=payload["user_id"],
                pocket_id=payload["pocket_id"],
                conn=conn,
            )
        except CloudError as exc:
            # The grant is saved; only the site link failed (pocket gone, access lost).
            logger.info("integrations: callback could not link the site: %s", exc.code)
            message["error"] = "link_failed"
    # A reconnect also revives every other site already bound to this grant.
    await bindings.notify_connection_status(conn, "connected")
    return message


def callback_html(message: dict[str, Any]) -> tuple[str, str]:
    """The callback page and its script nonce (for the CSP header)."""
    nonce = secrets.token_urlsafe(16)
    data = json.dumps(message).replace("<", "\\u003c")
    origins = json.dumps(message_origins()).replace("<", "\\u003c")
    ok = message.get("status") == "active" and not message.get("error")
    text = (
        "Connected. You can close this window."
        if ok
        else "That didn't work. Close this window and try again."
    )
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Paw Sites</title></head>
<body><p>{html.escape(text)}</p>
<script nonce="{nonce}">
(function () {{
  var msg = {data};
  var targets = {origins};
  try {{
    if (window.opener) {{
      for (var i = 0; i < targets.length; i++) {{
        try {{ window.opener.postMessage(msg, targets[i]); }} catch (e) {{}}
      }}
    }}
  }} finally {{
    setTimeout(function () {{ window.close(); }}, 300);
  }}
}})();
</script></body></html>"""
    return page, nonce
