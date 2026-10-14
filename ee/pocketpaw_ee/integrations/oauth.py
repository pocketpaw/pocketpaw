# ee/pocketpaw_ee/integrations/oauth.py: hosted connect + callback for connect kinds.
#
#   start_connect   POST /integrations/{provider}/connect -> {authorize_url}
#                   authorize_url is OUR /start?t=<ticket>, not the provider's page
#   start_resume    POST /integrations/connections/{id}/resume -> {authorize_url}
#                   the same /start URL, to the provider's ``resume_url`` (an
#                   unfinished setup) or, for needs_reconnect, its authorize_url
#   redeem_ticket   GET  /integrations/{provider}/start?t   -> cookie + 302 to provider
#   finish_callback GET  /integrations/{provider}/callback?code&state -> HTML page
#
# ``state`` lives in the single-use server-side store ``cloud.auth._oauth_state``
# (namespace ``integrations``) with workspace, user, provider, optional pocket_id,
# for a resume the connection id, and, for providers that support it, the PKCE
# S256 verifier. Its TTL is the provider's ``state_ttl_seconds`` when it sets one
# (Stripe onboarding can take an hour), else the store default; the browser
# cookie lives as long. The callback trusts only that payload, never its own query
# string beyond ``code`` and ``state``. A resume updates its own connection row in place
# (a different provider account is refused) and re-runs ``site_setup`` for every
# site bound to it. A provider's authorize_url may be sync or async.
#
# BROWSER BINDING. ``/start`` redeems a one-time ticket (TTL ``TICKET_TTL_SECONDS``)
# and sets an HttpOnly, Secure, SameSite=Lax cookie on the integrations path holding
# sha256(state). The callback refuses unless that cookie matches the state, and
# clears it. So a callback URL finished with someone else's provider account and
# replayed in a victim's browser (login CSRF) is refused there. Residual: a ticket
# link forwarded unopened inside its short TTL still starts a flow in whatever
# browser opens it, unless that browser carries a valid API session cookie for a
# different user than the one who started the flow: then /start refuses
# (``session_mismatch``). With no session cookie there is nothing to compare (Tauri
# opens it in the system browser).
#
# The redirect URI base is ``POCKETPAW_INTEGRATIONS_CALLBACK_BASE`` (one platform
# callback domain), falling back to the API's public base URL. The callback page
# posts {type: "paw:integration-connected", provider, connection_id, status, error?}
# to ``window.opener`` at the builder origin (``PAW_SITES_BUILDER_ORIGIN``) and the
# desktop shell's fixed origins, never "*". Not built on ``pocketpaw.clients.oauth``
# / ``TokenStore`` (local, plaintext, weak state). No token leaves this module.

from __future__ import annotations

import hashlib
import hmac
import html
import inspect
import json
import logging
import os
import secrets
from typing import Any
from urllib.parse import urlencode

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, Forbidden, ValidationError
from pocketpaw_ee.cloud._core.public_url import public_base_url
from pocketpaw_ee.integrations import bindings, vault

logger = logging.getLogger(__name__)

STATE_NAMESPACE = "integrations"
TICKET_NAMESPACE = "integrations_ticket"
TICKET_TTL_SECONDS = 120
COOKIE_PATH = "/api/v1/integrations/"
COOKIE_MAX_AGE = 600
CALLBACK_BASE_ENV = "POCKETPAW_INTEGRATIONS_CALLBACK_BASE"
MESSAGE_TYPE = "paw:integration-connected"
# The API session cookie (``cloud.auth.core.cookie_transport``).
SESSION_COOKIE = "paw_auth"


def callback_base() -> str:
    return os.environ.get(CALLBACK_BASE_ENV, "").strip().rstrip("/") or public_base_url()


def redirect_uri(provider_id: str) -> str:
    return f"{callback_base()}/api/v1/integrations/{provider_id}/callback"


def state_ttl(provider: Any) -> int:
    """How long a started flow stays redeemable for ``provider``."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    ttl = getattr(provider, "state_ttl_seconds", None)
    return int(ttl) if isinstance(ttl, int) and ttl > 0 else _oauth_state.STATE_TTL_SECONDS


def cookie_max_age(provider_id: str) -> int:
    from pocketpaw_ee.integrations.providers import get_provider

    provider = get_provider(provider_id)
    return max(COOKIE_MAX_AGE, state_ttl(provider) if provider else 0)


def cookie_name(provider_id: str) -> str:
    return f"paw_int_{provider_id}"


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class StartRefused(Exception):
    """``/start`` refused; ``code`` goes to the opener as the error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def message_origins() -> list[str]:
    """Where the callback page may post its result. Each is an origin we serve."""
    from pocketpaw.api.cors import BUILTIN_ORIGINS
    from pocketpaw_ee.sites.service import _builder_origin

    origins = [_builder_origin().rstrip("/"), *BUILTIN_ORIGINS]
    return [o for o in dict.fromkeys(origins) if o and o != "*"]


def _connectable(provider: Any) -> None:
    if provider.kind != "connect":
        raise ValidationError(
            "integrations.not_connectable", f"{provider.name} does not need connecting."
        )
    if provider.availability() != "available":
        raise ConflictError(
            "integrations.provider_unavailable", f"{provider.name} isn't available yet."
        )


async def _ticket_url(provider_id: str, state: str, user_id: str, provider_url: str) -> str:
    from pocketpaw_ee.cloud.auth import _oauth_state

    ticket = await _oauth_state.issue(
        TICKET_NAMESPACE,
        {"provider": provider_id, "state": state, "url": provider_url, "user_id": user_id},
        ttl_seconds=TICKET_TTL_SECONDS,
    )
    return f"{callback_base()}/api/v1/integrations/{provider_id}/start?" + urlencode({"t": ticket})


async def _issue_state(provider: Any, payload: dict[str, Any]) -> tuple[str, str | None]:
    """(state, PKCE challenge or None) for one connect or resume flow."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    challenge = None
    if getattr(provider, "supports_pkce", False):
        verifier, challenge = _oauth_state.pkce_pair()
        payload["code_verifier"] = verifier
    state = await _oauth_state.issue(STATE_NAMESPACE, payload, ttl_seconds=state_ttl(provider))
    return state, challenge


async def start_connect(
    *, workspace_id: str, user_id: str, provider_id: str, pocket_id: str | None
) -> str:
    """Issue single-use state + a one-time start ticket; return our /start URL."""
    provider = bindings.provider_or_404(provider_id)
    _connectable(provider)
    if pocket_id:
        await bindings.require_edit(workspace_id, user_id, pocket_id)
    state, challenge = await _issue_state(
        provider,
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "provider": provider.id,
            "pocket_id": pocket_id or "",
        },
    )
    provider_url = await _resolve(
        provider.authorize_url(state, redirect_uri(provider.id), code_challenge=challenge)
    )
    return await _ticket_url(provider.id, state, user_id, provider_url)


async def start_resume(
    *, workspace_id: str, user_id: str, connection_id: str, is_admin: bool
) -> str:
    """``POST /integrations/connections/{id}/resume``: the provider's ``resume_url``
    when it has one for this connection, else a fresh connect for needs_reconnect.
    Same authz as linking (creator or admin) and the same state + cookie binding."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    conn = await vault.get_connection(workspace_id, connection_id)
    if conn.created_by != user_id and not is_admin:
        raise Forbidden(
            "integrations.not_connection_owner",
            "Only the person who connected this account or a workspace admin can do that.",
        )
    vault.ensure_not_revoked(conn)
    provider = bindings.provider_or_404(conn.provider)
    _connectable(provider)
    state, challenge = await _issue_state(
        provider,
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "provider": provider.id,
            "pocket_id": "",
            "connection_id": str(conn.id),
        },
    )
    hook = getattr(provider, "resume_url", None)
    provider_url = None
    if hook is not None:
        provider_url = await _resolve(hook(conn, state, redirect_uri(provider.id)))
    if not provider_url and conn.status == "needs_reconnect":
        provider_url = await _resolve(
            provider.authorize_url(state, redirect_uri(provider.id), code_challenge=challenge)
        )
    if not provider_url:
        try:
            await _oauth_state.consume(STATE_NAMESPACE, state)
        except CloudError:
            pass
        raise ConflictError(
            "integrations.nothing_to_resume", "This connection has nothing left to finish."
        )
    return await _ticket_url(provider.id, state, user_id, str(provider_url))


def session_user_id(token: str | None) -> str | None:
    """The user of a VALID API session cookie, else None (expired, forged, absent)."""
    if not token:
        return None
    try:
        import jwt as pyjwt

        from pocketpaw_ee.cloud.auth.core import get_jwt_strategy

        strategy = get_jwt_strategy()
        key = strategy.decode_key
        payload = pyjwt.decode(
            token,
            key if isinstance(key, str) else key.get_secret_value(),
            audience=strategy.token_audience,
            algorithms=[strategy.algorithm],
        )
    except Exception:  # noqa: BLE001 - an unreadable cookie is no session
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


async def redeem_ticket(
    provider_id: str, ticket: str, session_token: str | None = None
) -> tuple[str, str]:
    """(provider authorize URL, state hash for the cookie). Raises ``StartRefused``
    for a bad ticket, or when the browser's session belongs to someone else."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    try:
        payload = await _oauth_state.consume(TICKET_NAMESPACE, ticket or "")
    except CloudError:
        raise StartRefused("invalid_ticket") from None
    if payload.get("provider") != provider_id or not payload.get("state"):
        raise StartRefused("invalid_ticket")
    session_user = session_user_id(session_token)
    if session_user and payload.get("user_id") and session_user != payload["user_id"]:
        raise StartRefused("session_mismatch")
    return str(payload["url"]), state_hash(str(payload["state"]))


async def finish_callback(
    provider_id: str, code: str, state: str, cookie: str | None = None
) -> dict[str, Any]:
    """Check the browser cookie, redeem state, finish the grant, store it, link the
    site. Returns the message for the opener."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    message: dict[str, Any] = {
        "type": MESSAGE_TYPE,
        "provider": provider_id,
        "connection_id": None,
        "status": "error",
    }
    if not state or not cookie or not hmac.compare_digest(cookie, state_hash(state)):
        # Not the browser that started this flow. The state is left alone so a
        # replayed link cannot burn the real owner's flow either.
        message["error"] = "invalid_state"
        return message
    try:
        payload = await _oauth_state.consume(STATE_NAMESPACE, state)
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
        grant = await provider.complete(
            code, redirect_uri(provider_id), code_verifier=payload.get("code_verifier")
        )
        conn = await vault.store_grant(
            workspace_id=payload["workspace_id"],
            provider=provider_id,
            grant=grant,
            user_id=payload["user_id"],
            connection_id=payload.get("connection_id") or "",
        )
    except Exception as exc:  # noqa: BLE001 - every failure becomes one safe message
        logger.warning("integrations: %s callback failed: %s", provider_id, type(exc).__name__)
        mismatch = isinstance(exc, CloudError) and exc.code == "integrations.resume_mismatch"
        message["error"] = "account_mismatch" if mismatch else "provider_error"
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
    # A reconnect / resume also revives every other site already bound to this grant.
    await bindings.setup_bound_sites(conn, payload["user_id"])
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
